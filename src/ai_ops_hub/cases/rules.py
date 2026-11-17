"""Pure case rules: effective data, validation of human corrections, problems.

No I/O. The *effective* data of a case is the analyzer's output with the human's
latest corrections laid on top; the analysis itself is never modified.
"""

from __future__ import annotations

import re
from datetime import date

from ai_ops_hub.analysis.schemas import (
    MATERIAL_FIELDS,
    AnalysisResult,
    Category,
    FieldName,
    FieldStatus,
    ReviewCode,
)
from ai_ops_hub.analysis.validation import REQUIRED_FIELDS
from ai_ops_hub.cases.schemas import (
    CATEGORY_TARGET,
    CLOSED_STATUSES,
    EffectiveField,
    EffectiveValue,
    Problem,
    ReviewStatus,
)
from ai_ops_hub.errors import InvalidCorrectionError

MAX_VALUES_PER_FIELD = 20
MAX_VALUE_CHARS = 500

# Same normalized shapes the analyzer produces, so human and analyzer values are comparable.
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_AMOUNT = re.compile(r"^\d{1,15}(?:\.\d{1,2})? [A-Z]{3}$")
_QUANTITY = re.compile(r"^\d{1,12}(?:\.\d{1,4})?(?: [^\W\d_][\w.]{0,24})?$")
_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s.]{2,}$")
_PHONE = re.compile(r"^\+?\d{7,15}$")
_CONTROL = re.compile("[\x00-\x08\x0b-\x1f\x7f]")

TARGETS = frozenset({n.value for n in FieldName} | {CATEGORY_TARGET})

# analyzer review reasons that a human date correction resolves
_DATE_REASONS = {ReviewCode.UNRESOLVED_RELATIVE_DATE, ReviewCode.UNRESOLVED_DATE_RANGE}
_DATE_FIELDS = {FieldName.DEADLINE.value, FieldName.REQUESTED_DATE.value}


def normalize_human_values(target: str, values: list[str]) -> list[str]:
    """Validate and normalize the values a human typed for ``target``.

    Raises InvalidCorrectionError listing every bad value. An empty list is
    allowed for fields (it means "there is no such value") but not for the category.
    """
    if target not in TARGETS:
        raise InvalidCorrectionError(f"unknown correction target {target!r}")
    if len(values) > MAX_VALUES_PER_FIELD:
        raise InvalidCorrectionError(f"at most {MAX_VALUES_PER_FIELD} values per field")

    cleaned: list[str] = []
    problems: list[dict] = []
    for raw in values:
        v = raw.strip()
        if not v:
            problems.append({"value": raw, "message": "value must not be blank"})
        elif len(v) > MAX_VALUE_CHARS:
            problems.append({"value": v[:40] + "...", "message": f"longer than {MAX_VALUE_CHARS} characters"})
        elif _CONTROL.search(v):
            problems.append({"value": v[:40], "message": "control characters are not allowed"})
        else:
            msg = _check_shape(target, v)
            if msg:
                problems.append({"value": v, "message": msg})
            else:
                cleaned.append(v)
    if target == CATEGORY_TARGET and len(values) != 1 and not problems:
        problems.append({"value": values, "message": "exactly one category is required"})
    if problems:
        raise InvalidCorrectionError(f"invalid value(s) for {target!r}", problems)

    # duplicates carry no information
    return list(dict.fromkeys(cleaned))


def _check_shape(target: str, v: str) -> str | None:
    if target == CATEGORY_TARGET:
        return None if v in {c.value for c in Category} else f"not a category: {[c.value for c in Category]}"
    if target in _DATE_FIELDS:
        if not _DATE.match(v):
            return "expected a full date YYYY-MM-DD"
        try:
            date.fromisoformat(v)
        except ValueError:
            return "not a real calendar date"
    elif target == FieldName.AMOUNT.value and not _AMOUNT.match(v):
        return "expected e.g. '12500.00 PLN' (number with up to 2 decimals and a 3-letter currency)"
    elif target == FieldName.QUANTITY.value and not _QUANTITY.match(v):
        return "expected a number with an optional unit, e.g. '50 pcs'"
    elif target == FieldName.EMAIL.value and not _EMAIL.match(v):
        return "not a valid e-mail address"
    elif target == FieldName.PHONE.value and not _PHONE.match(v):
        return "expected digits with an optional leading '+' (7-15 digits)"
    return None


def effective_category(result: AnalysisResult, latest: dict[str, list[str]]) -> tuple[Category, bool]:
    if CATEGORY_TARGET in latest:
        return Category(latest[CATEGORY_TARGET][0]), True
    return result.category, False


def effective_fields(result: AnalysisResult, latest: dict[str, list[str]]) -> tuple[EffectiveField, ...]:
    out: list[EffectiveField] = []
    for name in FieldName:
        analyzer = [f for f in result.fields if f.name is name]
        analyzer_values = tuple(f.value.normalized for f in analyzer)
        if name.value in latest:
            values = tuple(
                EffectiveValue(value=v, origin="human", status=FieldStatus.VALID)
                for v in latest[name.value]
            )
            out.append(EffectiveField(name=name, corrected=True, values=values, analyzer_values=analyzer_values))
        elif analyzer:
            values = tuple(
                EffectiveValue(
                    value=f.value.normalized,
                    origin="analyzer",
                    status=f.status,
                    span=f.value.span,
                    warnings=tuple(w.code.value for w in f.warnings),
                )
                for f in analyzer
            )
            out.append(EffectiveField(name=name, corrected=False, values=values, analyzer_values=analyzer_values))
    return tuple(out)


def find_problems(
    result: AnalysisResult,
    category: Category,
    category_corrected: bool,
    fields: tuple[EffectiveField, ...],
    latest: dict[str, list[str]],
) -> tuple[Problem, ...]:
    """What still stands between this case and a clean approval, judged on the
    EFFECTIVE data (analyzer output + human corrections), not on the analyzer's flag."""
    problems: list[Problem] = []
    present = {f.name for f in fields if f.values}

    if category is Category.OTHER:
        problems.append(Problem(code="category_unresolved", message="category is 'other'; choose the real category"))
    elif not category_corrected and result.category_alternatives:
        alts = ", ".join(c.value for c in result.category_alternatives)
        problems.append(Problem(
            code="ambiguous_category",
            message=f"the analyzer saw evidence for more than one category (also: {alts}); confirm the category",
        ))

    for name in REQUIRED_FIELDS.get(category, ()):
        if name not in present:
            problems.append(Problem(
                code="missing_required_field",
                message=f"a {category.value} normally states '{name.value}'; none present",
                field=name.value,
            ))
    if category is Category.INQUIRY and not present & {FieldName.PRODUCT, FieldName.REFERENCE}:
        problems.append(Problem(code="missing_required_field", message="inquiry names neither a product nor a reference"))

    for f in fields:
        for v in f.values:
            if v.origin != "analyzer" or v.status is FieldStatus.VALID:
                continue
            if f.name in MATERIAL_FIELDS or v.status is FieldStatus.INVALID:
                problems.append(Problem(
                    code="field_needs_review",
                    message=f"'{f.name.value}' is {v.status.value}: {v.value!r}; correct or confirm it",
                    field=f.name.value,
                ))

    codes = {r.code for r in result.review_reasons}
    if codes & _DATE_REASONS and not (_DATE_FIELDS & latest.keys()):
        problems.append(Problem(
            code="unresolved_date",
            message="a relative date or date range was found but no concrete date was entered",
        ))
    if ReviewCode.POSSIBLE_PROMPT_INJECTION in codes:
        problems.append(Problem(
            code="possible_prompt_injection",
            message="the email contains instruction-like text addressed to the system; "
            "approval needs a documented override",
        ))
    return tuple(problems)


def triage(problems: tuple[Problem, ...]) -> ReviewStatus:
    return ReviewStatus.NEEDS_REVIEW if problems else ReviewStatus.READY_FOR_REVIEW


def snapshot(category: Category, fields: tuple[EffectiveField, ...]) -> dict:
    """Compact record of the effective data, stored with every decision."""
    return {
        "category": category.value,
        "fields": {f.name.value: [v.value for v in f.values] for f in fields if f.values},
    }


def allowed_actions(status: ReviewStatus, problems: tuple[Problem, ...]) -> tuple[str, ...]:
    if status in CLOSED_STATUSES:
        return ("reopen",)
    approve = "approve_with_override" if problems else "approve"
    return ("edit", approve, "reject")
