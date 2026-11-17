"""Read-only comparison of two stored analyses of the SAME message (pure, no I/O).

It never changes either analysis and has no notion of "which one is right": it only shows
where they agree and where they differ, so a human can decide.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from ai_ops_hub.analysis.schemas import FieldName, StoredAnalysis


class FieldDiff(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: FieldName
    verdict: Literal["same", "different", "only_a", "only_b"]
    values_a: tuple[str, ...]
    values_b: tuple[str, ...]
    statuses_a: tuple[str, ...]
    statuses_b: tuple[str, ...]


class AnalysisComparison(BaseModel):
    model_config = ConfigDict(frozen=True)

    message_id: int
    analysis_a_id: int
    analysis_b_id: int
    method_a: str
    method_b: str
    analyzer_a: str
    analyzer_b: str
    version_a: str
    version_b: str
    category_a: str
    category_b: str
    category_match: bool
    requires_review_a: bool
    requires_review_b: bool
    warning_count_a: int
    warning_count_b: int
    fields: tuple[FieldDiff, ...]
    same: int
    different: int
    only_a: int
    only_b: int


def _values(a: StoredAnalysis, name: FieldName) -> tuple[tuple[str, ...], tuple[str, ...]]:
    found = sorted(((f.value.normalized, f.status.value) for f in a.result.fields if f.name is name))
    return tuple(v for v, _ in found), tuple(s for _, s in found)


def compare(a: StoredAnalysis, b: StoredAnalysis) -> AnalysisComparison:
    if a.message_id != b.message_id:
        raise ValueError("only analyses of the same message can be compared")
    diffs: list[FieldDiff] = []
    for name in FieldName:
        va, sa = _values(a, name)
        vb, sb = _values(b, name)
        if not va and not vb:
            continue
        if va and not vb:
            verdict = "only_a"
        elif vb and not va:
            verdict = "only_b"
        else:
            verdict = "same" if set(va) == set(vb) else "different"
        diffs.append(FieldDiff(name=name, verdict=verdict, values_a=va, values_b=vb, statuses_a=sa, statuses_b=sb))
    count = lambda v: sum(1 for d in diffs if d.verdict == v)  # noqa: E731
    return AnalysisComparison(
        message_id=a.message_id,
        analysis_a_id=a.id,
        analysis_b_id=b.id,
        method_a=a.analysis_method.value,
        method_b=b.analysis_method.value,
        analyzer_a=a.analyzer_name,
        analyzer_b=b.analyzer_name,
        version_a=a.analyzer_version,
        version_b=b.analyzer_version,
        category_a=a.category.value,
        category_b=b.category.value,
        category_match=a.category == b.category,
        requires_review_a=a.requires_review,
        requires_review_b=b.requires_review,
        warning_count_a=len(a.result.warnings),
        warning_count_b=len(b.result.warnings),
        fields=tuple(diffs),
        same=count("same"),
        different=count("different"),
        only_a=count("only_a"),
        only_b=count("only_b"),
    )
