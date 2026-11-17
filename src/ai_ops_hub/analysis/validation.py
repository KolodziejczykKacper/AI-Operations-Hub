"""Category-specific validation: when must a human look at the analysis?

Rules are intentionally small and explicit. A missing monetary amount is NOT an
error for any category. ``requires_review`` only means "a person must check
this"; nothing here approves, rejects or sends anything.
"""

from __future__ import annotations

from ai_ops_hub.analysis.schemas import (
    MATERIAL_FIELDS,
    AnalyzedField,
    Category,
    FieldName,
    FieldStatus,
    ReviewCode,
    ReviewReason,
)

# Fields a message of this category is expected to state explicitly.
REQUIRED_FIELDS: dict[Category, tuple[FieldName, ...]] = {
    Category.ORDER: (FieldName.PRODUCT, FieldName.QUANTITY),
    Category.COMPLAINT: (FieldName.REFERENCE,),
    Category.DOCUMENT_REQUEST: (FieldName.REFERENCE,),
}


def build_review_reasons(
    category: Category,
    fields: list[AnalyzedField],
    *,
    ambiguous_category: bool,
    injection_detected: bool,
    unresolved_relative_date: bool,
    unresolved_date_range: bool = False,
) -> tuple[ReviewReason, ...]:
    reasons: list[ReviewReason] = []
    present = {f.name for f in fields}

    if category is Category.OTHER:
        reasons.append(ReviewReason(
            code=ReviewCode.NO_CATEGORY_MATCH,
            message="no category reached the minimum rule score; manual triage needed",
        ))
    if ambiguous_category:
        reasons.append(ReviewReason(
            code=ReviewCode.AMBIGUOUS_CATEGORY,
            message="indicators for more than one category; the highest-priority one was chosen",
        ))
    if injection_detected:
        reasons.append(ReviewReason(
            code=ReviewCode.POSSIBLE_PROMPT_INJECTION,
            message="instruction-like text addressed to the system was found and ignored",
        ))

    for name in REQUIRED_FIELDS.get(category, ()):
        if name not in present:
            reasons.append(ReviewReason(
                code=ReviewCode.MISSING_REQUIRED_FIELD,
                message=f"a {category.value} normally states '{name.value}' but none was found",
                field=name,
            ))
    if category is Category.INQUIRY and not present & {FieldName.PRODUCT, FieldName.REFERENCE}:
        reasons.append(ReviewReason(
            code=ReviewCode.MISSING_REQUIRED_FIELD,
            message="inquiry names neither a product nor a reference number",
        ))

    for f in fields:
        if f.name in MATERIAL_FIELDS and f.status is not FieldStatus.VALID:
            reasons.append(ReviewReason(
                code=ReviewCode.FIELD_NEEDS_REVIEW,
                message=f"'{f.name.value}' is {f.status.value}: {f.value.span.text!r}",
                field=f.name,
            ))
        elif f.status is FieldStatus.INVALID:
            reasons.append(ReviewReason(
                code=ReviewCode.FIELD_NEEDS_REVIEW,
                message=f"'{f.name.value}' is invalid: {f.value.span.text!r}",
                field=f.name,
            ))

    if unresolved_relative_date:
        reasons.append(ReviewReason(
            code=ReviewCode.UNRESOLVED_RELATIVE_DATE,
            message="a relative time expression was found and could not be resolved",
        ))
    if unresolved_date_range:
        reasons.append(ReviewReason(
            code=ReviewCode.UNRESOLVED_DATE_RANGE,
            message="a range of dates was given where a deadline or delivery date was expected",
        ))
    return tuple(reasons)
