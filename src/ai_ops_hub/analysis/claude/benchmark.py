"""Compare the rule-based analyzer and the Claude analyzer on the fictional labelled datasets.

    python -m ai_ops_hub.analysis.claude.benchmark tests/data/eval/dataset_v1.jsonl            # estimate only
    python -m ai_ops_hub.analysis.claude.benchmark DATASET --simulate faithful|noisy          # offline pipeline check
    python -m ai_ops_hub.analysis.claude.benchmark DATASET --live --authorize-spending-usd 0.10 --max-cases 5

DEFAULT = NO NETWORK, NO COST: it prints the rule-based baseline and a price estimate.
``--simulate`` runs the REAL Claude pipeline (prompt, parsing, local verification) against a fake
provider. That checks the verification layer (false rejections / caught hallucinations); it says
NOTHING about how good Claude is. ``--live`` sends real requests and is refused unless ALL of:
  * AIOPS_CLAUDE_ENABLED=true and ANTHROPIC_API_KEY are set,
  * AIOPS_BENCHMARK_LIVE_AUTHORIZED=yes-i-authorize-spending is set,
  * --authorize-spending-usd is at least the worst-case estimate for the selected cases,
  * the holdout dataset is only used with --allow-holdout (do not tune on it).
Results of different datasets are NEVER pooled: run and read each dataset separately.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from ai_ops_hub.analysis.claude import pricing
from ai_ops_hub.analysis.claude.analyzer import ClaudeAnalyzer
from ai_ops_hub.analysis.evaluation import CaseResult, EvalCase, EvaluationReport, load_dataset
from ai_ops_hub.analysis.rule_based import RuleBasedAnalyzer
from ai_ops_hub.analysis.schemas import AnalysisResult, Category
from ai_ops_hub.errors import ClaudeError
from ai_ops_hub.schemas import AnalysisMethod

LIVE_ENV_FLAG = "AIOPS_BENCHMARK_LIVE_AUTHORIZED"
LIVE_ENV_VALUE = "yes-i-authorize-spending"


# --- a fake provider for offline pipeline checks -----------------------------------------------------


class ReplayClient:
    """Fake ``anthropic`` client. ``faithful``: returns what the rule-based analyzer found, quoted from the
    text (should pass verification). ``noisy``: adds fabricated values and a wrong quote (must be rejected)."""

    def __init__(self, mode: str, cases: list[EvalCase]) -> None:
        self.mode = mode
        self._by_text = {(c.analysis_input().subject or "", c.analysis_input().body_text): c for c in cases}
        self.requests: list[dict] = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kw):
        self.requests.append(kw)
        content = kw["messages"][0]["content"]
        subject = content.split("<email_subject>", 1)[1].split("</email_subject>", 1)[0]
        body = content.split("<email_body>", 1)[1].rsplit("</email_body>", 1)[0]
        base = RuleBasedAnalyzer().analyze(
            __import__("ai_ops_hub.analysis.schemas", fromlist=["AnalysisInput"]).AnalysisInput(
                subject=subject or None, body_text=body)
        )
        fields = [
            {"name": f.name.value, "quote": f.value.span.text, "source": f.value.span.source.value,
             "normalized": f.value.normalized, "status": f.status.value, "note": ""}
            for f in base.fields
        ]
        if self.mode == "noisy":
            fields += [
                {"name": "product", "quote": "totally invented widget", "source": "body",
                 "normalized": "totally invented widget", "status": "valid", "note": ""},
                {"name": "amount", "quote": "999 999,00 zł", "source": "body", "normalized": "999999.00 PLN",
                 "status": "valid", "note": ""},
            ]
        payload = {"category": base.category.value, "category_alternatives": [c.value for c in base.category_alternatives],
                   "fields": fields}
        usage = SimpleNamespace(input_tokens=pricing.estimate_input_tokens(len(subject) + len(body)) // 2,
                                output_tokens=40 + 25 * len(fields))
        resp = SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(payload))],
                               stop_reason="end_turn", usage=usage)
        resp._request_id = "req_simulated"
        return resp


# --- the benchmark -----------------------------------------------------------------------------------


@dataclass
class CaseStats:
    latency_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    proposed: int = 0
    rejected: int = 0
    failed: bool = False
    error: str = ""


@dataclass
class BenchmarkReport:
    label: str
    model: str
    report: EvaluationReport
    stats: dict[str, CaseStats]
    cost_usd: Decimal
    worst_case_usd: Decimal
    simulated: bool

    def totals(self) -> dict:
        s = list(self.stats.values())
        n = max(len(s), 1)
        return {
            "requests": len(s),
            "failed_requests": sum(x.failed for x in s),
            "proposed_values": sum(x.proposed for x in s),
            "rejected_unsupported_values": sum(x.rejected for x in s),
            "accepted_values_not_in_gold": sum(len(r.fp) for r in self.report.results),
            "mean_latency_ms": round(sum(x.latency_ms for x in s) / n, 1),
            "input_tokens": sum(x.input_tokens for x in s),
            "output_tokens": sum(x.output_tokens for x in s),
            "cost_usd": float(self.cost_usd),
            "worst_case_usd": float(self.worst_case_usd),
        }

    def to_text(self) -> str:
        r = self.report.to_dict()["all_cases"]
        c, e, t = r["classification"], r["extraction"]["overall"], self.totals()
        pct = lambda x: "n/a" if x is None else f"{100 * x:.1f}%"  # noqa: E731
        kind = "SIMULATED provider (pipeline check only, not model quality)" if self.simulated else "LIVE"
        return "\n".join([
            f"== {self.label} | {kind} | model {self.model} ==",
            f"classification accuracy : {c['correct']}/{c['total']} = {pct(c['accuracy'])}",
            f"extraction              : precision {pct(e['precision'])}  recall {pct(e['recall'])}  F1 {pct(e['f1'])}"
            f"  (TP {e['tp']}, FP {e['fp']}, FN {e['fn']})",
            f"fully correct cases     : {r['fully_correct_cases']}/{r['cases']}",
            f"unsupported/hallucinated: {t['rejected_unsupported_values']} of {t['proposed_values']} proposed values "
            f"rejected locally; {t['accepted_values_not_in_gold']} accepted values are not in the gold labels",
            f"failed requests         : {t['failed_requests']}/{t['requests']}",
            f"latency (mean)          : {t['mean_latency_ms']} ms   tokens in/out: {t['input_tokens']}/{t['output_tokens']}",
            f"cost                    : ${t['cost_usd']:.5f} computed from usage (worst-case estimate ${t['worst_case_usd']:.5f})",
        ])


def run_claude(cases: list[EvalCase], analyzer: ClaudeAnalyzer, *, label: str, simulated: bool) -> BenchmarkReport:
    results: list[CaseResult] = []
    stats: dict[str, CaseStats] = {}
    cost = worst = Decimal(0)
    for case in cases:
        inp = case.analysis_input()
        st = CaseStats()
        started = time.monotonic()
        _t, w = pricing.worst_case_cost_usd(analyzer.model, len(inp.subject or "") + len(inp.body_text), 4000)
        worst += w
        try:
            res = analyzer.analyze(inp)
            info = analyzer.last_call
            st.latency_ms = info.latency_ms if info else int((time.monotonic() - started) * 1000)
            if info:
                st.input_tokens, st.output_tokens = info.input_tokens, info.output_tokens
                st.proposed, st.rejected = info.values_proposed, info.values_rejected
                cost += pricing.cost_usd(analyzer.model, info.input_tokens, info.output_tokens)
        except ClaudeError as exc:  # a failed request is a failure, never silently a "correct" empty result
            st.failed, st.error = True, type(exc).__name__
            res = AnalysisResult(analyzer_name=analyzer.name, analyzer_version=analyzer.version,
                                 analysis_method=AnalysisMethod.LLM, category=Category.OTHER)
        cr = CaseResult(case=case, result=res)
        cr.gold = {(f, v) for f, vals in case.expected_fields.items() for v in vals}
        cr.predicted = {(f.name.value, f.value.normalized) for f in res.fields}
        for fname, want in case.expected_statuses.items():
            got = [f.status.value for f in res.fields if f.name.value == fname]
            cr.status_total += 1
            if not got or any(g != want for g in got):
                cr.status_wrong.append(f"{fname}: expected {want}, got {got or 'no field'}")
        results.append(cr)
        stats[case.id] = st
    return BenchmarkReport(label, analyzer.model, EvaluationReport(results), stats, cost, worst, simulated)


def estimate(cases: list[EvalCase], model: str, max_output_tokens: int) -> dict:
    """Price estimate for a live run (no network)."""
    chars = [len(c.analysis_input().subject or "") + len(c.analysis_input().body_text) for c in cases]
    worst = sum(pricing.worst_case_cost_usd(model, n, max_output_tokens)[1] for n in chars)
    typical = sum(pricing.cost_usd(model, pricing.estimate_input_tokens(n) // 2, 350) for n in chars)
    return {"cases": len(cases), "model": model, "worst_case_usd": float(worst), "typical_usd": float(typical),
            "prices_verified_on": pricing.PRICES_VERIFIED_ON}


# --- CLI ---------------------------------------------------------------------------------------------


def _refuse(msg: str) -> int:
    print(f"REFUSED: {msg}", file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(prog="ai_ops_hub.analysis.claude.benchmark")
    ap.add_argument("dataset", type=Path)
    ap.add_argument("--model", default=None)
    ap.add_argument("--simulate", choices=["faithful", "noisy"])
    ap.add_argument("--live", action="store_true", help="REAL paid requests (guarded, see module docstring)")
    ap.add_argument("--authorize-spending-usd", type=float, default=0.0)
    ap.add_argument("--max-cases", type=int, default=0, help="limit the number of cases (live runs: use a few)")
    ap.add_argument("--allow-holdout", action="store_true")
    args = ap.parse_args(argv)

    from ai_ops_hub.config import get_settings

    settings = get_settings()
    model = args.model or settings.claude_model
    if not pricing.known_model(model):
        return _refuse(f"model {model!r} has no known price")
    cases = load_dataset(args.dataset)
    if args.max_cases:
        cases = cases[: args.max_cases]
    label = args.dataset.name
    est = estimate(cases, model, settings.claude_max_output_tokens)

    print(f"dataset {label}: {len(cases)} cases (datasets are never pooled)")
    print(f"estimate for model {model}: typical ${est['typical_usd']:.4f}, worst case ${est['worst_case_usd']:.4f} "
          f"(prices verified {est['prices_verified_on']}; an estimate is not a spending limit)")
    base = __import__("ai_ops_hub.analysis.evaluation", fromlist=["evaluate"]).evaluate(cases, RuleBasedAnalyzer())
    print("\n-- rule_based baseline (offline, free) --")
    print(base.to_text().split("== WITHOUT")[0])

    if args.simulate:
        client = ReplayClient(args.simulate, cases)
        analyzer = ClaudeAnalyzer(client, model=model, max_output_tokens=settings.claude_max_output_tokens)
        print("\n" + run_claude(cases, analyzer, label=f"{label} / {args.simulate}", simulated=True).to_text())
        return 0

    if not args.live:
        print("\n(no network: pass --simulate for an offline pipeline check, or --live with authorization)")
        return 0

    if "holdout" in args.dataset.name and not args.allow_holdout:
        return _refuse("the holdout dataset is not used for tuning; pass --allow-holdout for a single final measurement")
    if not settings.claude_enabled:
        return _refuse("AIOPS_CLAUDE_ENABLED is not true")
    if settings.anthropic_api_key is None:
        return _refuse("ANTHROPIC_API_KEY is not set")
    if os.environ.get(LIVE_ENV_FLAG) != LIVE_ENV_VALUE:
        return _refuse(f"set {LIVE_ENV_FLAG}={LIVE_ENV_VALUE} to confirm that you authorize real, paid requests")
    if args.authorize_spending_usd + 1e-9 < est["worst_case_usd"]:
        return _refuse(f"--authorize-spending-usd {args.authorize_spending_usd} is below the worst-case estimate "
                       f"${est['worst_case_usd']:.4f}; use fewer cases (--max-cases) or authorize more")
    from ai_ops_hub.analysis.claude.analyzer import make_client

    client = make_client(settings.anthropic_api_key.get_secret_value(), settings.claude_timeout_seconds)
    analyzer = ClaudeAnalyzer(client, model=model, max_output_tokens=settings.claude_max_output_tokens)
    print("\n" + run_claude(cases, analyzer, label=label, simulated=False).to_text())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
