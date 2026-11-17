"""The deterministic, offline, rule-based analyzer.

Same input -> same output, always. No network, no randomness, no clock.
Results are always labelled ``analysis_method = rule_based``; they are pattern
matches, not AI inference, and scores are not probabilities.
"""

from __future__ import annotations

from ai_ops_hub.analysis.classification import classify
from ai_ops_hub.analysis.extraction import Candidate, extract_all, warnings_for
from ai_ops_hub.analysis.injection import find_injections, sentences_to_mask
from ai_ops_hub.analysis.schemas import (
    AnalysisInput,
    AnalysisResult,
    AnalysisWarning,
    AnalyzedField,
    FieldName,
    WarningCode,
)
from ai_ops_hub.analysis.text import Segment, make_warning
from ai_ops_hub.analysis.validation import build_review_reasons
from ai_ops_hub.schemas import AnalysisMethod, ExtractedValue, SpanSource, ValueKind

RULESET_VERSION = "1.0.0"

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
_FIELD_ORDER = {name: i for i, name in enumerate(FieldName)}
_SOURCE_RANK = {SpanSource.BODY: 0, SpanSource.SUBJECT: 1}


class RuleBasedAnalyzer:
    name = "rule_based_analyzer"
    version = RULESET_VERSION
    method = AnalysisMethod.RULE_BASED

    def analyze(self, message: AnalysisInput) -> AnalysisResult:
        segments: list[Segment] = []
        warnings: list[AnalysisWarning] = []
        injection = False

        for source, text in ((SpanSource.SUBJECT, message.subject), (SpanSource.BODY, message.body_text)):
            if not text or not text.strip():
                continue
            hits = find_injections(text)
            seg = Segment.build(source, text, sentences_to_mask(text, hits))
            for start, end in hits:
                injection = True
                warnings.append(make_warning(
                    WarningCode.POSSIBLE_PROMPT_INJECTION,
                    "instruction-like text addressed to the system; ignored for analysis",
                    seg, start, end,
                ))
            segments.append(seg)

        classification = classify(segments)

        collected: list[tuple[Segment, Candidate]] = []
        unresolved_relative = False
        unresolved_range = False
        for seg in segments:
            cands, notes = extract_all(seg)
            collected += [(seg, c) for c in cands]
            for n in notes:
                warnings.append(make_warning(n.code, n.message, seg, n.start, n.end))
                if n.code is WarningCode.RELATIVE_DATE_NOT_RESOLVED:
                    unresolved_relative = True
                if n.code is WarningCode.DATE_RANGE:
                    unresolved_range = True

        collected.sort(key=lambda sc: (_FIELD_ORDER[sc[1].name], _SOURCE_RANK[sc[0].source], sc[1].start))
        fields: list[AnalyzedField] = []
        seen: set[tuple[FieldName, str]] = set()
        for seg, c in collected:
            key = (c.name, c.normalized)
            if key in seen:  # same value in subject and body: keep the body occurrence
                continue
            seen.add(key)
            fields.append(AnalyzedField(
                name=c.name,
                value=ExtractedValue(
                    kind=_KIND[c.name],
                    normalized=c.normalized,
                    span=seg.span(c.start, c.end),
                    method=AnalysisMethod.RULE_BASED,
                ),
                status=c.status,
                warnings=warnings_for(seg, c),
            ))

        qty = {f.value.normalized for f in fields if f.name is FieldName.QUANTITY}
        if len(qty) > 1:
            warnings.append(AnalysisWarning(
                code=WarningCode.MULTIPLE_QUANTITIES,
                message="several different quantities found; their association with products is not determined",
                field=FieldName.QUANTITY,
            ))
        names = {f.value.normalized for f in fields if f.name is FieldName.CONTACT_NAME}
        if len(names) > 1:
            warnings.append(AnalysisWarning(
                code=WarningCode.MULTIPLE_NAMES,
                message="several different contact names found; sender identity is not inferred",
                field=FieldName.CONTACT_NAME,
            ))

        reasons = build_review_reasons(
            classification.category,
            fields,
            ambiguous_category=bool(classification.alternatives),
            injection_detected=injection,
            unresolved_relative_date=unresolved_relative,
            unresolved_date_range=unresolved_range,
        )
        return AnalysisResult(
            analyzer_name=self.name,
            analyzer_version=self.version,
            analysis_method=self.method,
            category=classification.category,
            heuristic_scores=classification.scores,
            indicators=classification.indicators,
            category_alternatives=classification.alternatives,
            fields=tuple(fields),
            warnings=tuple(warnings),
            review_reasons=reasons,
            requires_review=bool(reasons),
        )
