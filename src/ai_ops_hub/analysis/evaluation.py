"""Reproducible evaluation of an analyzer on a labelled fictional dataset.

    python -m ai_ops_hub.analysis.evaluation tests/data/eval/dataset_v1.jsonl [--failures]

What is measured (all computed from the dataset, nothing hard-coded):
* classification accuracy        - predicted category == expected category
* field extraction precision/recall/F1 - over (field, normalized value) pairs,
  micro-averaged, separately from classification
* status accuracy                - for gold fields that declare an expected status
* review-flag accuracy           - predicted requires_review == expected

Caveat the numbers must be read with: the dataset was written together with the
rules, so it is a regression/development set, NOT an independent benchmark.
Cases marked ``known_limitation`` document behaviour the rules cannot handle;
they are included in the headline numbers and also reported separately.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from ai_ops_hub.analysis.base import Analyzer
from ai_ops_hub.analysis.schemas import AnalysisInput, AnalysisResult
from ai_ops_hub.parsing.common import canonicalize_body, html_to_text


@dataclass(frozen=True)
class EvalCase:
    id: str
    language: str
    subject: str | None
    body: str  # raw; HTML is converted exactly as the .eml parser would
    body_format: str
    expected_category: str
    expected_review: bool
    expected_fields: dict[str, list[str]]
    expected_statuses: dict[str, str]
    tags: tuple[str, ...] = ()
    known_limitation: bool = False
    notes: str = ""

    def analysis_input(self) -> AnalysisInput:
        text = html_to_text(self.body) if self.body_format == "html" else self.body
        return AnalysisInput(subject=self.subject, body_text=canonicalize_body(text))


def load_dataset(path: str | Path) -> list[EvalCase]:
    cases: list[EvalCase] = []
    ids: set[str] = set()
    with open(path, encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, 1):
            if not line.strip():
                continue
            raw = json.loads(line)
            exp = raw["expected"]
            case = EvalCase(
                id=raw["id"],
                language=raw["language"],
                subject=raw.get("subject"),
                body=raw["body"],
                body_format=raw.get("body_format", "text"),
                expected_category=exp["category"],
                expected_review=exp["requires_review"],
                expected_fields={k: list(v) for k, v in exp.get("fields", {}).items()},
                expected_statuses=dict(exp.get("statuses", {})),
                tags=tuple(raw.get("tags", ())),
                known_limitation=bool(raw.get("known_limitation", False)),
                notes=raw.get("notes", ""),
            )
            if case.id in ids:
                raise ValueError(f"duplicate case id {case.id!r} (line {line_no})")
            ids.add(case.id)
            cases.append(case)
    return cases


@dataclass
class CaseResult:
    case: EvalCase
    result: AnalysisResult
    gold: set[tuple[str, str]] = field(default_factory=set)
    predicted: set[tuple[str, str]] = field(default_factory=set)
    status_total: int = 0
    status_wrong: list[str] = field(default_factory=list)

    @property
    def category_ok(self) -> bool:
        return self.result.category.value == self.case.expected_category

    @property
    def tp(self) -> set[tuple[str, str]]:
        return self.gold & self.predicted

    @property
    def fp(self) -> set[tuple[str, str]]:
        return self.predicted - self.gold

    @property
    def fn(self) -> set[tuple[str, str]]:
        return self.gold - self.predicted

    @property
    def review_ok(self) -> bool:
        return self.result.requires_review == self.case.expected_review

    @property
    def fields_exact(self) -> bool:
        return not self.fp and not self.fn

    @property
    def fully_correct(self) -> bool:
        return self.category_ok and self.fields_exact and self.review_ok and not self.status_wrong


def _prf(tp: int, fp: int, fn: int) -> dict[str, float | None]:
    p = tp / (tp + fp) if tp + fp else None
    r = tp / (tp + fn) if tp + fn else None
    f1 = 2 * p * r / (p + r) if p and r else (0.0 if p is not None and r is not None else None)
    return {"precision": p, "recall": r, "f1": f1, "tp": tp, "fp": fp, "fn": fn}


@dataclass
class EvaluationReport:
    results: list[CaseResult]

    def subset(self, include_limitations: bool) -> list[CaseResult]:
        return [r for r in self.results if include_limitations or not r.case.known_limitation]

    def classification(self, rows: list[CaseResult]) -> dict:
        n = len(rows)
        ok = sum(r.category_ok for r in rows)
        confusion: dict[str, Counter] = defaultdict(Counter)
        for r in rows:
            confusion[r.case.expected_category][r.result.category.value] += 1
        by_lang: dict[str, tuple[int, int]] = {}
        for lang in sorted({r.case.language for r in rows}):
            lr = [r for r in rows if r.case.language == lang]
            by_lang[lang] = (sum(r.category_ok for r in lr), len(lr))
        return {"correct": ok, "total": n, "accuracy": ok / n if n else None,
                "by_language": by_lang, "confusion": {k: dict(v) for k, v in confusion.items()}}

    def extraction(self, rows: list[CaseResult]) -> dict:
        tp = sum(len(r.tp) for r in rows)
        fp = sum(len(r.fp) for r in rows)
        fn = sum(len(r.fn) for r in rows)
        per_field: dict[str, dict] = {}
        names = sorted({f for r in rows for f, _ in (r.gold | r.predicted)})
        for name in names:
            per_field[name] = _prf(
                sum(1 for r in rows for f, _ in r.tp if f == name),
                sum(1 for r in rows for f, _ in r.fp if f == name),
                sum(1 for r in rows for f, _ in r.fn if f == name),
            )
        return {"overall": _prf(tp, fp, fn), "per_field": per_field,
                "cases_with_exact_fields": sum(r.fields_exact for r in rows), "total_cases": len(rows)}

    def statuses(self, rows: list[CaseResult]) -> dict:
        total = sum(r.status_total for r in rows)
        wrong = sum(len(r.status_wrong) for r in rows)
        return {"declared": total, "correct": total - wrong,
                "accuracy": (total - wrong) / total if total else None}

    def review(self, rows: list[CaseResult]) -> dict:
        ok = sum(r.review_ok for r in rows)
        return {"correct": ok, "total": len(rows), "accuracy": ok / len(rows) if rows else None}

    def provenance_ok(self) -> bool:
        return True  # enforced at evaluation time (evaluate() raises otherwise)

    def to_dict(self) -> dict:
        out = {}
        for label, inc in (("all_cases", True), ("without_known_limitations", False)):
            rows = self.subset(inc)
            out[label] = {
                "classification": self.classification(rows),
                "extraction": self.extraction(rows),
                "status": self.statuses(rows),
                "review_flag": self.review(rows),
                "fully_correct_cases": sum(r.fully_correct for r in rows),
                "cases": len(rows),
            }
        return out

    def to_text(self, show_failures: bool = False) -> str:
        def pct(x: float | None) -> str:
            return "n/a" if x is None else f"{100 * x:.1f}%"

        lines: list[str] = []
        for label, inc in (("ALL CASES", True), ("WITHOUT KNOWN-LIMITATION CASES", False)):
            rows = self.subset(inc)
            c, e = self.classification(rows), self.extraction(rows)
            s, rv = self.statuses(rows), self.review(rows)
            o = e["overall"]
            lines += [
                f"== {label} ({len(rows)} cases) ==",
                f"classification accuracy : {c['correct']}/{c['total']} = {pct(c['accuracy'])}",
                "  by language           : " + ", ".join(
                    f"{k} {a}/{b}" for k, (a, b) in c["by_language"].items()),
                f"field extraction (micro): precision {pct(o['precision'])}  recall {pct(o['recall'])}  "
                f"F1 {pct(o['f1'])}  (TP {o['tp']}, FP {o['fp']}, FN {o['fn']})",
                f"  cases with exactly right fields: {e['cases_with_exact_fields']}/{e['total_cases']}",
                f"status accuracy         : {s['correct']}/{s['declared']} = {pct(s['accuracy'])}",
                f"review-flag accuracy    : {rv['correct']}/{rv['total']} = {pct(rv['accuracy'])}",
                f"fully correct cases     : {sum(r.fully_correct for r in rows)}/{len(rows)}",
                "per field:",
            ]
            for name, m in e["per_field"].items():
                lines.append(
                    f"  {name:<15} P {pct(m['precision']):>6}  R {pct(m['recall']):>6}  "
                    f"(TP {m['tp']}, FP {m['fp']}, FN {m['fn']})"
                )
            lines.append("confusion (expected -> predicted): " + json.dumps(c["confusion"], ensure_ascii=False))
            lines.append("")
        if show_failures:
            lines.append("== CASES WITH ANY DEVIATION ==")
            for r in self.results:
                if r.fully_correct:
                    continue
                tag = " [known limitation]" if r.case.known_limitation else ""
                lines.append(f"- {r.case.id}{tag}")
                if not r.category_ok:
                    lines.append(f"    category: expected {r.case.expected_category}, got {r.result.category.value}")
                for f, v in sorted(r.fn):
                    lines.append(f"    MISSED   {f} = {v!r}")
                for f, v in sorted(r.fp):
                    lines.append(f"    EXTRA    {f} = {v!r}")
                for msg in r.status_wrong:
                    lines.append(f"    STATUS   {msg}")
                if not r.review_ok:
                    lines.append(f"    review flag: expected {r.case.expected_review}, got {r.result.requires_review}")
        return "\n".join(lines)


def evaluate(cases: list[EvalCase], analyzer: Analyzer) -> EvaluationReport:
    results: list[CaseResult] = []
    for case in cases:
        inp = case.analysis_input()
        res = analyzer.analyze(inp)
        if not res.verify_provenance(inp.body_text, inp.subject):
            raise AssertionError(f"{case.id}: a source span does not match the analysed text")
        cr = CaseResult(case=case, result=res)
        cr.gold = {(f, v) for f, vals in case.expected_fields.items() for v in vals}
        cr.predicted = {(f.name.value, f.value.normalized) for f in res.fields}
        for fname, want in case.expected_statuses.items():
            got = [f.status.value for f in res.fields if f.name.value == fname]
            cr.status_total += 1
            if not got or any(g != want for g in got):
                cr.status_wrong.append(f"{fname}: expected {want}, got {got or 'no field'}")
        results.append(cr)
    return EvaluationReport(results)


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    from ai_ops_hub.analysis.rule_based import RuleBasedAnalyzer

    ap = argparse.ArgumentParser(prog="ai_ops_hub.analysis.evaluation")
    ap.add_argument("dataset", type=Path)
    ap.add_argument("--failures", action="store_true", help="list every deviation")
    ap.add_argument("--json", action="store_true", help="print metrics as JSON")
    args = ap.parse_args(argv)

    report = evaluate(load_dataset(args.dataset), RuleBasedAnalyzer())
    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(report.to_text(show_failures=args.failures))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
