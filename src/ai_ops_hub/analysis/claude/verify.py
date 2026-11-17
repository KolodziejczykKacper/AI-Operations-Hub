"""Turn the model's raw answer into a verified ``AnalysisResult``.

The model's output is a CLAIM, never a fact. Every value is checked locally against the
original text before it can enter the result:

* the quote must occur verbatim in the subject/body; offsets are computed HERE (never trusted);
* the normalized value must be supported by that quote (same digits, same currency, same
  date parts, or - for free text - contained in the quote). A value the quote does not support
  is REJECTED (a hallucination or an unsupported rewrite) and recorded as a warning;
* values inside instruction-like text (prompt injection) are rejected like the rule-based
  analyzer masks them;
* the model's status can only be downgraded, never upgraded; a date without a stated year
  becomes ``ambiguous`` with the partial value ``--MM-DD``, exactly like the rule-based analyzer;
* review reasons are computed locally with the same rules as for every analyzer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from ai_ops_hub.analysis.claude.schema import RawAnalysis, RawField
from ai_ops_hub.analysis.injection import find_injections, sentences_to_mask
from ai_ops_hub.analysis.schemas import (
    MATERIAL_FIELDS,
    AnalysisInput,
    AnalysisResult,
    AnalysisWarning,
    AnalyzedField,
    Category,
    FieldName,
    FieldStatus,
    WarningCode,
)
from ai_ops_hub.analysis.text import fold
from ai_ops_hub.analysis.validation import build_review_reasons
from ai_ops_hub.cases.rules import check_value_shape
from ai_ops_hub.schemas import AnalysisMethod, ExtractedValue, SourceSpan, SpanSource, ValueKind

_KIND = {
    FieldName.CONTACT_NAME: ValueKind.PARTY,
    FieldName.ORGANIZATION: ValueKind.PARTY,
    FieldName.REFERENCE: ValueKind.REFERENCE,
    FieldName.PRODUCT: ValueKind.PRODUCT,
    FieldName.QUANTITY: ValueKind.QUANTITY,
    FieldName.AMOUNT: ValueKind.AMOUNT,
    FieldName.DEADLINE: ValueKind.DEADLINE,
    FieldName.REQUESTED_DATE: ValueKind.DATE,
    FieldName.EMAIL: ValueKind.EMAIL,
    FieldName.PHONE: ValueKind.PHONE,
}
_ORDER = {n: i for i, n in enumerate(FieldName)}
_DATE_FIELDS = (FieldName.DEADLINE, FieldName.REQUESTED_DATE)
_TEXT_FIELDS = (FieldName.CONTACT_NAME, FieldName.ORGANIZATION, FieldName.REFERENCE, FieldName.PRODUCT)
_CONTROL = re.compile("[\x00-\x08\x0b-\x1f\x7f]")

# currency evidence in the quote -> the only code the normalized value may carry
_CURRENCY_EVIDENCE: list[tuple[re.Pattern, str]] = [
    (re.compile(r"(?i)(?:\bzł\b|\bzl\b|\bpln\b|złot)"), "PLN"),
    (re.compile(r"(?i)(?:€|\beur\b|\beuro)"), "EUR"),
    (re.compile(r"(?i)(?:\$|\busd\b|dolar|\bdollar)"), "USD"),
    (re.compile(r"(?i)(?:£|\bgbp\b|\bfunt)"), "GBP"),
    (re.compile(r"(?i)\bchf\b"), "CHF"),
]


@dataclass
class VerificationStats:
    proposed: int = 0
    accepted: int = 0
    rejected: int = 0  # hallucinated / unsupported values that were dropped
    downgraded: int = 0  # kept, but the status was lowered by local checks
    reasons: list[str] = field(default_factory=list)  # short codes only, no email content


@dataclass(frozen=True)
class _Checked:
    normalized: str
    status: FieldStatus
    warnings: tuple[tuple[WarningCode, str], ...] = ()


class _Reject(Exception):
    pass


def _digits(text: str) -> str:
    return re.sub(r"\D", "", text)


def _numbers(text: str) -> set[Decimal]:
    out: set[Decimal] = set()
    for raw in re.findall(r"\d[\d  .,]*\d|\d", text):
        compact = re.sub(r"[  ]", "", raw)
        for cand in {compact, compact.replace(",", "."), compact.replace(".", "").replace(",", "."), compact.replace(",", "")}:
            try:
                out.add(Decimal(cand))
            except InvalidOperation:
                pass
    return out


def _squash(text: str) -> str:
    return " ".join(fold(text).split())


def _check_amount(normalized: str, quote: str) -> _Checked:
    m = re.match(r"^(\d{1,15}(?:\.\d{1,4})?)(?: ([A-Za-z]{3}))?$", normalized.strip())
    if not m:
        raise _Reject("amount_format")
    number, code = Decimal(m.group(1)), (m.group(2) or "").upper()
    if number not in _numbers(quote) and _digits(quote) not in {_digits(m.group(1)), _digits(m.group(1).split(".")[0])}:
        raise _Reject("amount_not_in_quote")
    evidence = {c for rx, c in _CURRENCY_EVIDENCE if rx.search(quote)}
    if code and evidence and code not in evidence:
        raise _Reject("currency_mismatch")
    decimals = len(m.group(1).split(".")[1]) if "." in m.group(1) else 0
    shown = f"{number:.2f}" if decimals <= 2 else m.group(1)
    if code and code in evidence:
        return _Checked(f"{shown} {code}", FieldStatus.VALID)
    # no currency evidence in the quote: do not accept a currency the model "inferred"
    return _Checked(shown, FieldStatus.AMBIGUOUS,
                    ((WarningCode.CURRENCY_NOT_STATED, "no currency is stated next to the amount"),))


def _check_quantity(normalized: str, quote: str) -> _Checked:
    m = re.match(r"^(\d{1,15}(?:\.\d{1,4})?)(?: (\S.{0,40}))?$", normalized.strip())
    if not m:
        raise _Reject("quantity_format")
    if Decimal(m.group(1)) not in _numbers(quote) and _digits(quote) != _digits(m.group(1)):
        raise _Reject("quantity_not_in_quote")
    return _Checked(normalized.strip(), FieldStatus.VALID)


def _check_date(normalized: str, quote: str, model_status: str) -> _Checked:
    nums = [int(x) for x in re.findall(r"\d+", quote)]
    if not nums:  # a relative expression ("next Friday"): keep the words, never invent a date
        return _Checked(quote.strip(), FieldStatus.AMBIGUOUS,
                        ((WarningCode.RELATIVE_DATE_NOT_RESOLVED, "a relative time expression, not a concrete date"),))
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", normalized.strip())
    if not m:
        raise _Reject("date_format")
    y, mo, d = (int(x) for x in m.groups())
    try:
        from datetime import date

        date(y, mo, d)
    except ValueError:
        raise _Reject("date_not_real") from None
    if d not in nums:
        raise _Reject("day_not_in_quote")
    has_month_word = bool(re.search(r"[^\W\d_]{3,}", quote))
    if mo not in nums and not has_month_word:
        raise _Reject("month_not_in_quote")
    if y not in nums and (y % 100) not in nums:
        # the year was inferred by the model, not written: keep only what is written
        return _Checked(f"--{mo:02d}-{d:02d}", FieldStatus.AMBIGUOUS,
                        ((WarningCode.YEAR_NOT_STATED, "no year is stated; the year was not assumed"),))
    return _Checked(normalized.strip(), FieldStatus.VALID)


def _check_email(normalized: str, quote: str) -> _Checked:
    n = normalized.strip().lower()
    if n not in quote.lower():
        raise _Reject("email_not_in_quote")
    return _Checked(n, FieldStatus.VALID)


def _check_phone(normalized: str, quote: str) -> _Checked:
    n, q = _digits(normalized), _digits(quote)
    if not n or not (q.endswith(n) or n.endswith(q)):
        raise _Reject("phone_not_in_quote")
    return _Checked(normalized.strip(), FieldStatus.VALID)


def _check_text(normalized: str, quote: str) -> _Checked:
    n = _squash(normalized)
    if not n or n not in _squash(quote):
        raise _Reject("text_not_in_quote")
    return _Checked(" ".join(normalized.split()), FieldStatus.VALID)


# These mean "the text is there, but the model's normalized form is not canonical". The quote is real,
# so nothing is invented: keep the QUOTED text (never the model's rewrite) and lower the status.
_FORMAT_ONLY = {"amount_format", "quantity_format", "date_format", "date_not_real"}


def _check_value(name: FieldName, normalized: str, quote: str, model_status: str) -> _Checked:
    try:
        return _check_value_strict(name, normalized, quote, model_status)
    except _Reject as exc:
        if str(exc) not in _FORMAT_ONLY:
            raise
        status = FieldStatus.AMBIGUOUS if model_status == "ambiguous" else FieldStatus.INVALID
        return _Checked(" ".join(quote.split()), status,
                        ((WarningCode.LLM_FORMAT_INVALID, "the text could not be normalized to the canonical format"),))


def _check_value_strict(name: FieldName, normalized: str, quote: str, model_status: str) -> _Checked:
    if _CONTROL.search(normalized):
        raise _Reject("control_characters")
    if name is FieldName.AMOUNT:
        return _check_amount(normalized, quote)
    if name is FieldName.QUANTITY:
        return _check_quantity(normalized, quote)
    if name in _DATE_FIELDS:
        return _check_date(normalized, quote, model_status)
    if name is FieldName.EMAIL:
        return _check_email(normalized, quote)
    if name is FieldName.PHONE:
        return _check_phone(normalized, quote)
    return _check_text(normalized, quote)


def _locate(quote: str, source: str, subject: str | None, body: str) -> tuple[SpanSource, int, int] | None:
    """(source, offset of the first occurrence, number of occurrences in BOTH subject and body).

    The named source is searched first. The caller decides what repeated occurrences mean."""
    order = [(SpanSource.SUBJECT, subject), (SpanSource.BODY, body)]
    if source == "body":
        order.reverse()
    total = sum(text.count(quote) for _s, text in order if text)
    for src, text in order:
        if text:
            i = text.find(quote)
            if i >= 0:
                return src, i, total
    return None


def _status(model_status: str, checked: FieldStatus) -> FieldStatus:
    """The model's status may only be lowered by local checks, never raised."""
    rank = {FieldStatus.VALID: 0, FieldStatus.AMBIGUOUS: 1, FieldStatus.INVALID: 2}
    given = FieldStatus(model_status)
    return given if rank[given] >= rank[checked] else checked


def _overlaps(ranges: list[tuple[int, int]], start: int, end: int) -> bool:
    return any(start < e and s < end for s, e in ranges)


def build_result(
    raw: RawAnalysis,
    message: AnalysisInput,
    *,
    analyzer_name: str,
    analyzer_version: str,
) -> tuple[AnalysisResult, VerificationStats]:
    stats = VerificationStats(proposed=len(raw.fields))
    subject, body = message.subject, message.body_text

    # instruction-like sentences: values taken from them are never accepted
    masked = {SpanSource.SUBJECT: [], SpanSource.BODY: []}
    warnings: list[AnalysisWarning] = []
    injection = False
    for src, text in ((SpanSource.SUBJECT, subject), (SpanSource.BODY, body)):
        if text and text.strip():
            hits = find_injections(text)
            masked[src] = sentences_to_mask(text, hits)
            for s, e in hits:
                injection = True
                warnings.append(AnalysisWarning(
                    code=WarningCode.POSSIBLE_PROMPT_INJECTION,
                    message="instruction-like text addressed to the system; not used for extraction",
                    span=SourceSpan(start=s, end=e, text=text[s:e], source=src),
                ))

    fields: list[AnalyzedField] = []
    seen: set[tuple[FieldName, str]] = set()
    unresolved_relative = False

    def reject(name: FieldName, code: str) -> None:
        stats.rejected += 1
        stats.reasons.append(code)
        warnings.append(AnalysisWarning(
            code=WarningCode.LLM_VALUE_REJECTED,
            message=f"a value proposed for '{name.value}' was rejected: {code.replace('_', ' ')}",
            field=name,
        ))

    for rf in raw.fields:
        quote = rf.quote
        if not quote.strip() or _CONTROL.search(quote):
            reject(rf.name, "empty_or_invalid_quote")
            continue
        loc = _locate(quote, rf.source, subject, body)
        if loc is None:
            reject(rf.name, "quote_not_found_in_source")
            continue
        src, start, occurrences = loc
        end = start + len(quote)
        if _overlaps(masked[src], start, end):
            reject(rf.name, "inside_instruction_like_text")
            continue
        try:
            checked = _check_value(rf.name, rf.normalized, quote, rf.status)
        except _Reject as exc:
            reject(rf.name, str(exc))
            continue

        status = _status(rf.status, checked.status)
        normalized = checked.normalized
        field_warnings = [AnalysisWarning(code=c, message=m, field=rf.name) for c, m in checked.warnings]
        if occurrences > 1 and rf.name in MATERIAL_FIELDS:
            # The same words appear more than once (in one source, or in subject AND body). For an amount, a
            # quantity or a date the role may differ per occurrence, so never silently pick the first one:
            # keep the first occurrence as the span, but mark the field ambiguous for a human.
            status = _status("ambiguous", status)
            field_warnings.append(AnalysisWarning(
                code=WarningCode.LLM_SPAN_AMBIGUOUS,
                message=f"the quoted text occurs {occurrences} times in the email; the first occurrence is shown, "
                "check which one is meant",
                field=rf.name,
            ))
        if status is FieldStatus.VALID and rf.name not in _TEXT_FIELDS:
            problem = check_value_shape(rf.name.value, normalized)
            if problem:  # supported by the quote, but not in the canonical shape
                status = FieldStatus.INVALID
                field_warnings.append(AnalysisWarning(
                    code=WarningCode.LLM_FORMAT_INVALID, message=f"not in the expected format: {problem}", field=rf.name))
        if status is not FieldStatus.VALID and rf.status == "valid":
            stats.downgraded += 1
        if any(c is WarningCode.RELATIVE_DATE_NOT_RESOLVED for c, _ in checked.warnings):
            unresolved_relative = True
        if rf.status != "valid" and not field_warnings and rf.note.strip():
            field_warnings.append(AnalysisWarning(
                code=WarningCode.LLM_MODEL_NOTE,
                message="model note: " + " ".join(rf.note.split())[:200],
                field=rf.name,
            ))

        key = (rf.name, normalized)
        if key in seen:
            continue
        seen.add(key)
        stats.accepted += 1
        fields.append(AnalyzedField(
            name=rf.name,
            value=ExtractedValue(
                kind=_KIND[rf.name],
                normalized=normalized,
                span=SourceSpan(start=start, end=end, text=quote, source=src),
                method=AnalysisMethod.LLM,
            ),
            status=status,
            warnings=tuple(field_warnings),
        ))

    fields.sort(key=lambda f: (_ORDER[f.name], f.value.span.start))
    alternatives = tuple(dict.fromkeys(c for c in raw.category_alternatives if c is not raw.category))
    reasons = build_review_reasons(
        raw.category,
        fields,
        ambiguous_category=bool(alternatives),
        injection_detected=injection,
        unresolved_relative_date=unresolved_relative,
    )
    result = AnalysisResult(
        analyzer_name=analyzer_name,
        analyzer_version=analyzer_version,
        analysis_method=AnalysisMethod.LLM,
        category=raw.category,
        category_alternatives=alternatives,
        fields=tuple(fields),
        warnings=tuple(warnings),
        review_reasons=reasons,
        requires_review=bool(reasons),
    )
    if not result.verify_provenance(body, subject):  # defence in depth: must never fail
        raise AssertionError("a verified span does not match the source text")
    return result, stats
