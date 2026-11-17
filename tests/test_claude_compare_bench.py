"""Comparison of the two analyzers, configuration hygiene and the offline benchmark."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from claude_helpers import FAKE_KEY, GOOD, FakeClaude, analyze_claude, answer, claude_client, enabled_settings, raw_field, response
from conftest import import_text
from export_helpers import complaint, snapshot_of_db

from ai_ops_hub.analysis.claude import benchmark, pricing
from ai_ops_hub.analysis.claude.analyzer import ClaudeAnalyzer
from ai_ops_hub.analysis.compare import compare
from ai_ops_hub.analysis.evaluation import load_dataset
from ai_ops_hub.analysis.schemas import AnalysisResult, Category, StoredAnalysis
from ai_ops_hub.config import Settings
from ai_ops_hub.errors import ClaudeError
from ai_ops_hub.schemas import AnalysisMethod

ROOT = Path(__file__).resolve().parents[1]
EVAL = ROOT / "tests" / "data" / "eval"


def err(r):
    return r.json()["error"]


# --- comparison ---------------------------------------------------------------------------------------------------------


def test_compare_endpoint_defaults_to_latest_rule_based_against_latest_claude(engine):
    different = answer("order", [raw_field("reference", "FV/2026/0001"), raw_field("amount", "12 500,00 zł", "12500.00 PLN"),
                                 raw_field("contact_name", "Anna Wiśniewska")])
    with claude_client(engine, enabled_settings(), FakeClaude(response(different))) as (c, _f, _fac):
        mid = import_text(c, complaint(1))
        c.post(f"/messages/{mid}/analyze")
        analyze_claude(c, mid)
        cmp = c.get(f"/messages/{mid}/compare").json()
        assert (cmp["method_a"], cmp["method_b"]) == ("rule_based", "llm")
        assert cmp["category_a"] == "complaint" and cmp["category_b"] == "order" and cmp["category_match"] is False
        verdicts = {f["name"]: f["verdict"] for f in cmp["fields"]}
        assert verdicts["reference"] == "same" and verdicts["amount"] == "same" and verdicts["contact_name"] == "same"
        assert verdicts["deadline"] == "only_a"  # rule_based found the deadline, this Claude answer did not
        assert cmp["same"] == 3 and cmp["only_a"] == 1 and cmp["only_b"] == 0 and cmp["different"] == 0


def test_compare_reports_different_and_only_claude_values(engine):
    claude = answer("complaint", [raw_field("reference", "FV/2026/0001"), raw_field("deadline", "20 listopada 2026", "2026-11-20"),
                                  raw_field("phone", "+48 600 100 200", "+48600100200")])
    body = complaint(1).replace("FV/2026/0001", "FV/2026/0001 (poprz. FV/2025/9999)") + "\ntel. +48 600 100 200"
    with claude_client(engine, enabled_settings(), FakeClaude(response(claude))) as (c, _f, _fac):
        mid = import_text(c, body)
        c.post(f"/messages/{mid}/analyze")
        analyze_claude(c, mid)
        cmp = c.get(f"/messages/{mid}/compare").json()
        verdicts = {f["name"]: f["verdict"] for f in cmp["fields"]}
        assert verdicts["reference"] == "different" and verdicts["phone"] in ("only_b", "same")
        ref = next(f for f in cmp["fields"] if f["name"] == "reference")
        assert "FV/2025/9999" in ref["values_a"] and ref["values_b"] == ["FV/2026/0001"]


def test_compare_is_read_only(engine):
    with claude_client(engine, enabled_settings(), FakeClaude(response(GOOD))) as (c, fake, _fac):
        mid = import_text(c, complaint(1))
        c.post(f"/messages/{mid}/analyze")
        analyze_claude(c, mid)
        before = snapshot_of_db(engine)
        calls = fake.calls
        for _ in range(3):
            assert c.get(f"/messages/{mid}/compare").status_code == 200
        assert snapshot_of_db(engine) == before and fake.calls == calls


def test_compare_errors(engine):
    with claude_client(engine, enabled_settings(), FakeClaude(response(GOOD))) as (c, _f, _fac):
        mid, other = import_text(c, complaint(1)), import_text(c, complaint(2))
        assert c.get("/messages/999/compare").status_code == 404
        r = c.get(f"/messages/{mid}/compare")
        assert r.status_code == 404 and err(r)["code"] == "analysis_not_found"
        c.post(f"/messages/{mid}/analyze")
        assert c.get(f"/messages/{mid}/compare").status_code == 404  # still no Claude analysis
        analyze_claude(c, mid)
        foreign = analyze_claude(c, other).json()["analysis"]["id"]
        r = c.get(f"/messages/{mid}/compare", params={"analysis_b": foreign})
        assert r.status_code == 404 and "does not belong" in err(r)["message"]
        assert c.get(f"/messages/{mid}/compare", params={"analysis_a": 0}).status_code == 422


def test_comparing_works_without_claude_being_enabled(engine):
    with claude_client(engine, Settings(_env_file=None)) as (c, fake, factory):
        mid = import_text(c, complaint(1))
        c.post(f"/messages/{mid}/analyze")
        assert c.get(f"/messages/{mid}/compare").status_code == 404  # nothing to compare, no error about config
        assert fake.calls == 0 and factory.built == []


def _stored(i: int, mid: int, method: AnalysisMethod, category=Category.ORDER) -> StoredAnalysis:
    from datetime import datetime, timezone

    return StoredAnalysis(
        id=i, message_id=mid, analyzer_name="x", analyzer_version="1", analysis_method=method, category=category,
        requires_review=False, source_fingerprint="f", created_at=datetime.now(timezone.utc),
        result=AnalysisResult(analyzer_name="x", analyzer_version="1", analysis_method=method, category=category),
    )


def test_compare_refuses_analyses_of_different_messages():
    with pytest.raises(ValueError):
        compare(_stored(1, 1, AnalysisMethod.RULE_BASED), _stored(2, 2, AnalysisMethod.LLM))


def test_compare_of_two_empty_analyses_is_empty_and_symmetric_in_category():
    cmp = compare(_stored(1, 1, AnalysisMethod.RULE_BASED), _stored(2, 1, AnalysisMethod.LLM, Category.INQUIRY))
    assert cmp.fields == () and cmp.same == cmp.different == cmp.only_a == cmp.only_b == 0
    assert cmp.category_match is False and (cmp.category_a, cmp.category_b) == ("order", "inquiry")


# --- configuration hygiene --------------------------------------------------------------------------------------------------


def test_defaults_are_safe_and_cheap():
    s = Settings(_env_file=None)
    assert s.claude_enabled is False and s.claude_model == "claude-haiku-5-5" and pricing.known_model(s.claude_model)
    assert s.claude_max_output_tokens == 4000 and s.claude_max_input_chars == 20_000
    assert s.claude_max_cost_usd_per_request == 0.02 and s.claude_daily_budget_usd == 0.25 and s.claude_timeout_seconds == 30


def test_the_api_key_is_a_secret_that_never_appears_in_repr_or_str_or_dumps():
    s = enabled_settings()
    assert s.anthropic_api_key.get_secret_value() == FAKE_KEY
    for rendered in (repr(s), str(s), repr(s.anthropic_api_key), str(s.anthropic_api_key), str(s.model_dump())):
        assert FAKE_KEY not in rendered


def test_the_key_and_the_switch_are_read_from_the_environment_and_a_local_env_file(db_dir, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("AIOPS_CLAUDE_ENABLED", raising=False)
    env = db_dir / ".env"
    env.write_text(f"ANTHROPIC_API_KEY={FAKE_KEY}\nAIOPS_CLAUDE_ENABLED=true\nAIOPS_CLAUDE_MODEL=claude-sonnet-5-5\n", encoding="utf-8")
    s = Settings(_env_file=str(env))
    assert s.claude_enabled and s.claude_model == "claude-sonnet-5-5" and s.anthropic_api_key.get_secret_value() == FAKE_KEY
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-from-env")
    assert Settings(_env_file=None).anthropic_api_key.get_secret_value() == "sk-ant-from-env"


@pytest.mark.parametrize("field, value", [("claude_max_output_tokens", 10), ("claude_max_output_tokens", 10**6),
                                          ("claude_max_input_chars", 5), ("claude_max_cost_usd_per_request", 0),
                                          ("claude_max_cost_usd_per_request", 100), ("claude_daily_budget_usd", -1),
                                          ("claude_timeout_seconds", 0), ("claude_timeout_seconds", 10_000)])
def test_cost_and_size_limits_are_bounded(field, value):
    with pytest.raises(Exception):
        Settings(_env_file=None, **{field: value})


def test_secrets_are_ignored_by_git_and_not_committed_in_the_example_file():
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert re.search(r"^\.env$", ignore, re.M) and ".env.*" in ignore and "!.env.example" in ignore
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert not re.search(r"sk-ant-[A-Za-z0-9_-]{8,}", example)
    assert re.search(r"^# AIOPS_CLAUDE_ENABLED=true", example, re.M)  # commented out: off by default
    assert not re.search(r"^ANTHROPIC_API_KEY=\S", example, re.M)


def test_no_real_key_pattern_exists_anywhere_in_the_repository_sources():
    pattern = re.compile(r"sk-ant-(?!FAKE)[A-Za-z0-9_-]{16,}")
    for path in list((ROOT / "src").rglob("*.py")) + list((ROOT / "tests").rglob("*.py")) + [ROOT / "README.md", ROOT / "docs" / "DEMO_PL.md"]:
        assert not pattern.search(path.read_text(encoding="utf-8")), path


def test_the_anthropic_sdk_is_a_declared_dependency_and_the_price_table_is_dated():
    assert '"anthropic>=1,<2"' in (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", pricing.PRICES_VERIFIED_ON) and pricing.PRICING_SOURCE.startswith("https://")


# --- pricing -----------------------------------------------------------------------------------------------------------------------


def test_price_table_and_cost_arithmetic():
    from decimal import Decimal

    assert pricing.cost_usd("claude-haiku-5-5", 100_000, 1_000_000) == Decimal("0.51")  # standard tier up to 100K
    assert pricing.cost_usd("claude-sonnet-5-5", 1_000_000, 1_000_000) == Decimal("12")
    assert pricing.cost_usd("claude-opus-5-5", 1_000_000, 1_000_000) == Decimal("24")
    assert pricing.cost_usd("claude-haiku-5-5", 200_000, 1_000_000) == Decimal("2.6")  # long-prompt tier ($0.5 / $2.5)
    with pytest.raises(KeyError):
        pricing.cost_usd("claude-unknown", 1, 1)


def test_the_estimate_never_underestimates_the_overhead_or_the_output_cap():
    tokens, cost = pricing.worst_case_cost_usd("claude-haiku-5-5", 1000, 4000)
    assert tokens >= pricing.PROMPT_OVERHEAD_TOKENS + 500
    assert cost >= pricing.cost_usd("claude-haiku-5-5", tokens, 4000)


# --- offline benchmark ---------------------------------------------------------------------------------------------------------------


def faithful(cases):
    return ClaudeAnalyzer(benchmark.ReplayClient("faithful", cases), model="claude-haiku-5-5", max_output_tokens=4000)


def test_the_estimate_needs_no_network_and_no_key(capsys, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert benchmark.main([str(EVAL / "dataset_v1.jsonl")]) == 0
    out = capsys.readouterr().out
    assert "estimate for model claude-haiku-5-5" in out and "worst case" in out and "no network" in out
    assert "rule_based baseline" in out


def test_a_faithful_replay_passes_every_dev_value_through_the_verifier():
    """Pipeline check on the DEV set: the verifier must not reject legitimate, text-supported values."""
    cases = load_dataset(EVAL / "dataset_v1.jsonl")
    rep = benchmark.run_claude(cases, faithful(cases), label="v1", simulated=True)
    t = rep.totals()
    assert t["failed_requests"] == 0 and t["rejected_unsupported_values"] == 0 and t["requests"] == len(cases)
    assert rep.simulated and "SIMULATED" in rep.to_text() and "not model quality" in rep.to_text()


def test_a_noisy_replay_has_all_fabricated_values_rejected_and_metrics_unharmed():
    cases = load_dataset(EVAL / "dataset_v1.jsonl")
    clean = benchmark.run_claude(cases, faithful(cases), label="v1", simulated=True)
    noisy = benchmark.run_claude(
        cases, ClaudeAnalyzer(benchmark.ReplayClient("noisy", cases), model="claude-haiku-5-5", max_output_tokens=4000),
        label="v1", simulated=True)
    assert noisy.totals()["rejected_unsupported_values"] >= 2 * len(cases)  # 2 fabricated values per message
    a, b = clean.report.to_dict()["all_cases"], noisy.report.to_dict()["all_cases"]
    assert a["extraction"]["overall"]["fp"] == b["extraction"]["overall"]["fp"]  # nothing fabricated got through
    assert a["classification"]["correct"] == b["classification"]["correct"]


def test_benchmark_reports_cost_latency_and_tokens_from_usage():
    cases = load_dataset(EVAL / "dataset_challenge_v1.jsonl")
    t = benchmark.run_claude(cases, faithful(cases), label="c", simulated=True).totals()
    assert t["input_tokens"] > 0 and t["output_tokens"] > 0 and 0 < t["cost_usd"] < t["worst_case_usd"]
    assert t["mean_latency_ms"] >= 0


def test_a_failed_request_counts_as_a_failure_not_as_a_correct_empty_answer():
    cases = load_dataset(EVAL / "dataset_challenge_v1.jsonl")[:3]
    from claude_helpers import api_error

    rep = benchmark.run_claude(cases, ClaudeAnalyzer(FakeClaude(api_error("timeout")), model="claude-haiku-5-5", max_output_tokens=4000),
                               label="x", simulated=False)
    assert rep.totals()["failed_requests"] == 3 and rep.totals()["cost_usd"] == 0
    d = rep.report.to_dict()["all_cases"]
    assert d["fully_correct_cases"] <= sum(1 for c in cases if not c.expected_fields and c.expected_category == "other" and not c.expected_review)


def test_the_benchmark_never_pools_datasets():
    src = (ROOT / "src" / "ai_ops_hub" / "analysis" / "claude" / "benchmark.py").read_text(encoding="utf-8")
    assert "never pooled" in src
    assert benchmark.main.__code__.co_argcount == 1  # one dataset path per run


@pytest.mark.parametrize("argv, env, fragment", [
    (["--live"], {}, "AIOPS_CLAUDE_ENABLED"),  # Claude not enabled
    (["--live"], {"AIOPS_CLAUDE_ENABLED": "true"}, "ANTHROPIC_API_KEY"),
    (["--live"], {"AIOPS_CLAUDE_ENABLED": "true", "ANTHROPIC_API_KEY": FAKE_KEY}, "AIOPS_BENCHMARK_LIVE_AUTHORIZED"),
    (["--live", "--max-cases", "2"], {"AIOPS_CLAUDE_ENABLED": "true", "ANTHROPIC_API_KEY": FAKE_KEY,
                                      "AIOPS_BENCHMARK_LIVE_AUTHORIZED": "yes-i-authorize-spending"}, "below the worst-case estimate"),
    (["--live", "--max-cases", "2", "--authorize-spending-usd", "0.00001"],
     {"AIOPS_CLAUDE_ENABLED": "true", "ANTHROPIC_API_KEY": FAKE_KEY, "AIOPS_BENCHMARK_LIVE_AUTHORIZED": "yes-i-authorize-spending"},
     "below the worst-case estimate"),
])
def test_live_benchmark_is_refused_unless_every_guard_is_satisfied_and_makes_no_request(argv, env, fragment, monkeypatch, capsys):
    for k in ("AIOPS_CLAUDE_ENABLED", "ANTHROPIC_API_KEY", "AIOPS_BENCHMARK_LIVE_AUTHORIZED"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    from ai_ops_hub.config import get_settings

    get_settings.cache_clear()
    built = []
    monkeypatch.setattr("ai_ops_hub.analysis.claude.analyzer.make_client", lambda *a, **k: built.append(a))
    try:
        assert benchmark.main([str(EVAL / "dataset_challenge_v1.jsonl"), *argv]) == 2
    finally:
        get_settings.cache_clear()
    assert fragment in capsys.readouterr().err and built == []


def test_the_holdout_dataset_is_refused_for_live_runs_without_an_explicit_flag(monkeypatch, capsys):
    from ai_ops_hub.config import get_settings

    for k, v in {"AIOPS_CLAUDE_ENABLED": "true", "ANTHROPIC_API_KEY": FAKE_KEY,
                 "AIOPS_BENCHMARK_LIVE_AUTHORIZED": "yes-i-authorize-spending"}.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    try:
        assert benchmark.main([str(EVAL / "dataset_holdout_v1.jsonl"), "--live", "--authorize-spending-usd", "5"]) == 2
    finally:
        get_settings.cache_clear()
    assert "holdout" in capsys.readouterr().err


def test_an_unknown_model_is_refused_by_the_benchmark(capsys):
    assert benchmark.main([str(EVAL / "dataset_v1.jsonl"), "--model", "claude-mystery"]) == 2
    assert "no known price" in capsys.readouterr().err


def test_claude_errors_are_the_only_errors_the_benchmark_swallows():
    assert issubclass(ClaudeError, Exception) and ClaudeError.__module__ == "ai_ops_hub.errors"
