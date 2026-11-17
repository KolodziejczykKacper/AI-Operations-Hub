"""The fictional evaluation datasets and the evaluator itself.

Reading the numbers: dataset_v1 was written together with the rules (a
regression/development set). dataset_challenge_v1 was written afterwards, is
messier, and the rules were NOT tuned against it. Tests pin behaviour; they do
not claim the numbers generalise.
"""

import json
import re
from pathlib import Path

import pytest

from ai_ops_hub.analysis import RuleBasedAnalyzer
from ai_ops_hub.analysis.evaluation import EvalCase, evaluate, load_dataset, main
from ai_ops_hub.analysis.schemas import (
    AnalysisResult,
    AnalyzedField,
    Category,
    FieldName,
    FieldStatus,
    ReviewCode,
    ReviewReason,
)
from ai_ops_hub.parsing import parse_eml, parse_pasted_text
from ai_ops_hub.schemas import AnalysisMethod, ExtractedValue, SourceSpan, ValueKind

from conftest import SAMPLES, run_analysis, values

DATA = Path(__file__).parent / "data" / "eval"
DEV = DATA / "dataset_v1.jsonl"
CHALLENGE = DATA / "dataset_challenge_v1.jsonl"
HOLDOUT = DATA / "dataset_holdout_v1.jsonl"
VALID_CATEGORIES = {c.value for c in Category}
VALID_FIELDS = {f.value for f in FieldName}


@pytest.fixture(scope="module")
def dev_report():
    return evaluate(load_dataset(DEV), RuleBasedAnalyzer())


@pytest.fixture(scope="module")
def challenge_report():
    return evaluate(load_dataset(CHALLENGE), RuleBasedAnalyzer())


@pytest.fixture(scope="module")
def holdout_report():
    return evaluate(load_dataset(HOLDOUT), RuleBasedAnalyzer())


# --------------------------------------------------------------------------- dataset hygiene


@pytest.mark.parametrize("path", [DEV, CHALLENGE, HOLDOUT], ids=lambda p: p.name)
def test_dataset_is_well_formed(path):
    cases = load_dataset(path)  # also rejects duplicate ids
    assert len(cases) >= 20
    for c in cases:
        assert c.expected_category in VALID_CATEGORIES
        assert set(c.expected_fields) <= VALID_FIELDS
        assert set(c.expected_statuses) <= VALID_FIELDS
        assert set(c.expected_statuses.values()) <= {s.value for s in FieldStatus}
        assert c.language in {"pl", "en"}
        assert c.body.strip()
        assert all(isinstance(v, str) and v for vals in c.expected_fields.values() for v in vals)


@pytest.mark.parametrize("path", [DEV, CHALLENGE, HOLDOUT], ids=lambda p: p.name)
def test_dataset_is_fictional_and_contains_no_secrets(path):
    text = path.read_text(encoding="utf-8")
    for domain in re.findall(r"@([\w.-]+)", text):
        assert domain.lower().rstrip(".").endswith((".example", ".test")), domain
    for needle in ("api_key", "password", "sk-", "BEGIN PRIVATE"):
        assert needle.lower() not in text.lower(), needle


def test_dev_dataset_covers_every_required_area():
    cases = load_dataset(DEV)
    cats = {c.expected_category for c in cases}
    assert cats == VALID_CATEGORIES
    assert {"pl", "en"} == {c.language for c in cases}
    for cat in VALID_CATEGORIES:
        per_cat = [c for c in cases if c.expected_category == cat and c.language == "pl"]
        per_cat_en = [c for c in cases if c.expected_category == cat and c.language == "en"]
        assert per_cat and per_cat_en, f"{cat} lacks one language"
    tags = {t for c in cases for t in c.tags}
    required = {
        "missing_fields", "ambiguous_date", "invalid_date", "ambiguous_value", "unsupported_claims",
        "conflicting_indicators", "prompt_injection", "diacritics", "no_diacritics", "html",
        "relative_date", "missing_year", "currency_not_inferred", "multiple_values",
    }
    assert required <= tags, required - tags
    assert sum(c.body_format == "html" for c in cases) >= 2


def test_known_limitation_cases_are_explained():
    for c in load_dataset(DEV):
        if c.known_limitation:
            assert c.notes, f"{c.id}: a known limitation must say what the rules cannot do"


def test_diacritic_pair_has_the_same_gold_structure():
    cases = {c.id: c for c in load_dataset(DEV)}
    a, b = cases["dia-with"], cases["dia-without"]
    assert a.expected_category == b.expected_category
    assert set(a.expected_fields) == set(b.expected_fields)


# --------------------------------------------------------------------------- accuracy on the development set


def test_every_non_limitation_case_is_fully_correct(dev_report):
    bad = [r.case.id for r in dev_report.subset(include_limitations=False) if not r.fully_correct]
    assert bad == []


def test_classification_and_extraction_are_measured_separately(dev_report):
    rep = dev_report.to_dict()["all_cases"]
    assert {"classification", "extraction", "status", "review_flag"} <= set(rep)
    assert 0 <= rep["classification"]["accuracy"] <= 1
    assert 0 <= rep["extraction"]["overall"]["f1"] <= 1
    # different units: categories (cases) vs. field values
    assert rep["classification"]["total"] == len(load_dataset(DEV))
    n_gold = sum(len(v) for c in load_dataset(DEV) for v in c.expected_fields.values())
    assert rep["extraction"]["overall"]["tp"] + rep["extraction"]["overall"]["fn"] == n_gold


def test_known_limitations_are_counted_in_the_headline_numbers(dev_report):
    allc = dev_report.to_dict()["all_cases"]
    sub = dev_report.to_dict()["without_known_limitations"]
    assert allc["cases"] > sub["cases"]
    n_lim = allc["cases"] - sub["cases"]
    assert allc["fully_correct_cases"] <= sub["fully_correct_cases"] + n_lim
    assert allc["classification"]["accuracy"] <= sub["classification"]["accuracy"]


def test_documented_limitations_really_are_limitations(dev_report):
    """If a limitation case starts passing, the dataset note is stale and should be updated."""
    still_failing = [r.case.id for r in dev_report.results if r.case.known_limitation and not r.fully_correct]
    flagged = [r.case.id for r in dev_report.results if r.case.known_limitation]
    assert sorted(still_failing) == sorted(flagged)  # every remaining limitation case still deviates
    assert len(flagged) == 3  # two earlier ones were fixed and moved to the regression cases


def test_prompt_injection_cases_are_flagged_and_never_change_the_category(dev_report):
    rows = [r for r in dev_report.results if "prompt_injection" in r.case.tags]
    assert len(rows) >= 3
    for r in rows:
        assert r.category_ok and r.result.requires_review
        assert any(x.code is ReviewCode.POSSIBLE_PROMPT_INJECTION for x in r.result.review_reasons)


def _keys_and_codes(obj, acc=None):
    """All dict keys plus every 'code' value in a dumped result (source quotes are excluded)."""
    acc = set() if acc is None else acc
    if isinstance(obj, dict):
        for k, v in obj.items():
            acc.add(str(k))
            if k == "code":
                acc.add(str(v))
            _keys_and_codes(v, acc)
    elif isinstance(obj, list):
        for v in obj:
            _keys_and_codes(v, acc)
    return acc


def test_no_result_in_either_dataset_carries_an_approval(dev_report, challenge_report):
    for rep in (dev_report, challenge_report):
        for r in rep.results:
            structure = _keys_and_codes(r.result.model_dump(mode="json"))
            assert not any("approv" in s.lower() or "send" in s.lower() for s in structure), r.case.id
            assert r.result.analysis_method is AnalysisMethod.RULE_BASED


def test_unsupported_claims_case_has_no_invented_fields(dev_report):
    r = next(x for x in dev_report.results if x.case.id == "spec-unsupported-claims")
    produced = {f.name.value for f in r.result.fields}
    assert produced == {"quantity", "product"}


# --------------------------------------------------------------------------- challenge set (not tuned)


def test_challenge_set_regression_floor(challenge_report):
    """Written when the rules were frozen: 12/20 categories, TP 34 / FP 3 / FN 5. After the
    quality iteration: 14/20, TP 36 / FP 0 / FN 3 - but the rules were then fixed for exactly
    these errors, so the set is now a development set. The floor only guards against regressions."""
    d = challenge_report.to_dict()["all_cases"]
    assert d["classification"]["correct"] >= 14
    ex = d["extraction"]["overall"]
    assert ex["tp"] >= 36 and ex["fp"] <= 0 and ex["fn"] <= 3


def test_the_challenge_set_is_still_harder_than_the_development_set(dev_report, challenge_report):
    dev = dev_report.to_dict()["all_cases"]["classification"]["accuracy"]
    ch = challenge_report.to_dict()["all_cases"]["classification"]["accuracy"]
    assert ch < dev


def test_evaluation_is_deterministic(dev_report):
    again = evaluate(load_dataset(DEV), RuleBasedAnalyzer())
    assert again.to_dict() == dev_report.to_dict()
    assert again.to_text(show_failures=True) == dev_report.to_text(show_failures=True)


def test_all_provenance_spans_verify_on_every_case(dev_report, challenge_report):
    for rep in (dev_report, challenge_report):
        for r in rep.results:
            inp = r.case.analysis_input()
            assert r.result.verify_provenance(inp.body_text, inp.subject), r.case.id


# --------------------------------------------------------------------------- the evaluator's own arithmetic


def make_result(category, items, review=False):
    fields = tuple(
        AnalyzedField(
            name=FieldName(name),
            value=ExtractedValue(
                kind=ValueKind.OTHER, normalized=norm,
                span=SourceSpan(start=0, end=1, text="x"), method=AnalysisMethod.RULE_BASED,
            ),
            status=FieldStatus(status),
        )
        for name, norm, status in items
    )
    reasons = (ReviewReason(code=ReviewCode.NO_CATEGORY_MATCH, message="r"),) if review else ()
    return AnalysisResult(
        analyzer_name="stub", analyzer_version="0", analysis_method=AnalysisMethod.RULE_BASED,
        category=category, fields=fields, review_reasons=reasons, requires_review=review,
    )


class StubAnalyzer:
    name, version, method = "stub", "0", AnalysisMethod.RULE_BASED

    def __init__(self, table):
        self.table = table

    def analyze(self, message):
        return self.table[message.body_text]


def stub_cases(tmp_lines):
    return [EvalCase(**c) for c in tmp_lines]


def case_kwargs(id_, body, category, fields, review=False, statuses=None):
    return dict(
        id=id_, language="en", subject=None, body=body, body_format="text",
        expected_category=category, expected_review=review,
        expected_fields=fields, expected_statuses=statuses or {},
    )


def test_metric_arithmetic_on_a_hand_computed_example():
    cases = stub_cases([
        case_kwargs("a", "x A", "order", {"quantity": ["1"], "product": ["p"]}),
        case_kwargs("b", "x B", "complaint", {"amount": ["5.00 PLN"]}, statuses={"amount": "valid"}),
        case_kwargs("c", "x C", "other", {}, review=True),
        case_kwargs("d", "x D", "inquiry", {"phone": ["+48600100200"]}),
    ])
    stub = StubAnalyzer({
        "x A": make_result(Category.ORDER, [("quantity", "1", "valid"), ("product", "p", "valid")]),
        "x B": make_result(Category.ORDER, [("amount", "6.00 PLN", "ambiguous")]),  # wrong category, wrong value
        "x C": make_result(Category.OTHER, [("email", "a@b.example", "valid")], review=True),  # extra field
        "x D": make_result(Category.INQUIRY, []),  # missed field
    })
    rep = evaluate(cases, stub).to_dict()["all_cases"]

    assert rep["classification"]["correct"] == 3 and rep["classification"]["total"] == 4
    ex = rep["extraction"]["overall"]
    assert (ex["tp"], ex["fp"], ex["fn"]) == (2, 2, 2)
    assert ex["precision"] == ex["recall"] == ex["f1"] == 0.5
    assert rep["extraction"]["per_field"]["amount"]["fp"] == 1
    assert rep["extraction"]["per_field"]["amount"]["fn"] == 1
    assert rep["extraction"]["per_field"]["phone"]["fn"] == 1
    assert rep["extraction"]["per_field"]["email"]["fp"] == 1
    assert rep["extraction"]["cases_with_exact_fields"] == 1  # only case "a"
    assert rep["status"] == {"declared": 1, "correct": 0, "accuracy": 0.0}
    assert rep["review_flag"]["correct"] == 4  # c predicted review (expected), others not


def test_perfect_and_empty_analyzers():
    cases = stub_cases([case_kwargs("a", "x A", "order", {"quantity": ["1"]})])
    perfect = evaluate(cases, StubAnalyzer({"x A": make_result(Category.ORDER, [("quantity", "1", "valid")])}))
    r = perfect.to_dict()["all_cases"]
    assert r["classification"]["accuracy"] == 1.0 and r["extraction"]["overall"]["f1"] == 1.0
    empty = evaluate(cases, StubAnalyzer({"x A": make_result(Category.OTHER, [])})).to_dict()["all_cases"]
    assert empty["classification"]["accuracy"] == 0.0
    assert empty["extraction"]["overall"]["recall"] == 0.0
    assert empty["extraction"]["overall"]["precision"] is None  # nothing predicted: undefined, not 0 or 1


def test_evaluator_rejects_results_whose_spans_do_not_match_the_text():
    cases = stub_cases([case_kwargs("a", "y A", "order", {})])  # body starts with 'y' but the span says 'x'
    stub = StubAnalyzer({"y A": make_result(Category.ORDER, [("quantity", "1", "valid")])})
    with pytest.raises(AssertionError, match="span"):
        evaluate(cases, stub)


def test_duplicate_case_ids_are_rejected(tmp_path_factory):
    import tempfile

    line = json.dumps({"id": "same", "language": "en", "subject": None, "body": "x",
                       "expected": {"category": "other", "requires_review": True}})
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        p = Path(d) / "dup.jsonl"
        p.write_text(line + "\n" + line + "\n", encoding="utf-8")
        with pytest.raises(ValueError, match="duplicate"):
            load_dataset(p)


def test_command_line_report(capsys):
    assert main([str(DEV), "--failures"]) == 0
    out = capsys.readouterr().out
    assert "classification accuracy" in out and "field extraction" in out
    assert "WITHOUT KNOWN-LIMITATION CASES" in out
    assert "lim-order-table" in out  # failures are listed by id
    assert main([str(DEV), "--json"]) == 0
    assert "all_cases" in json.loads(capsys.readouterr().out)


# --------------------------------------------------------------------------- the Stage 1 sample messages


def _parse(path):
    if path.suffix == ".eml":
        return parse_eml(path.read_bytes())
    return parse_pasted_text(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("path", sorted(p for p in SAMPLES.iterdir() if p.suffix in {".eml", ".txt"}), ids=lambda p: p.name)
def test_every_stage1_sample_can_be_analyzed_with_valid_provenance(path):
    msg = _parse(path)
    r = run_analysis(msg.body_text, msg.subject)
    assert r.verify_provenance(msg.body_text, msg.subject)
    assert r.analysis_method is AnalysisMethod.RULE_BASED
    assert r == run_analysis(msg.body_text, msg.subject)  # deterministic


def test_stage1_sample_values():
    m1 = _parse(SAMPLES / "01_invoice_dispute_pl.eml")
    r1 = run_analysis(m1.body_text, m1.subject)
    assert r1.category is Category.COMPLAINT
    assert values(r1, "reference") == ["FV/2026/0042"]
    assert values(r1, "amount") == ["12500.00 PLN"]
    assert values(r1, "deadline") == ["2026-11-20"]
    assert values(r1, "organization") == ["Zielona Dolina Sp. z o.o."]
    # the "conflicting" variant differs in amount and deadline, and the analysis reflects it
    m2 = _parse(SAMPLES / "02_invoice_dispute_pl_conflict.eml")
    r2 = run_analysis(m2.body_text, m2.subject)
    assert values(r2, "amount") == ["21500.00 PLN"] and values(r2, "deadline") == ["2026-12-20"]

    m5 = _parse(SAMPLES / "05_html_only_en.eml")
    r5 = run_analysis(m5.body_text, m5.subject)
    assert values(r5, "reference") == ["INV-5521"] and values(r5, "amount") == ["3250.00 USD"]
    assert values(r5, "deadline") == ["2026-11-15"]


# --------------------------------------------------------------------------- holdout set (frozen before the fixes)

HOLDOUT_SHA256 = "6060a2907a1323d5d987c9012acab9263b50403374df28b9a287fd4c020937cf"

# Measured on the SAME analyzer code before the quality iteration (recorded, not re-derived):
BASELINE_BEFORE_FIXES = {
    "dataset_v1": {"classification": 57, "tp": 126, "fp": 2, "fn": 6},
    "dataset_challenge_v1": {"classification": 12, "tp": 34, "fp": 3, "fn": 5},
    "dataset_holdout_v1": {"classification": 36, "tp": 64, "fp": 15, "fn": 7},
}


def test_the_holdout_file_is_frozen():
    """The holdout set was written before the quality fixes and must not be edited to fit
    the rules. If this fails, someone changed the labels or the messages: that makes the
    set a development set and invalidates every 'independent' claim made about it."""
    import hashlib

    text = HOLDOUT.read_text(encoding="utf-8").replace("\r\n", "\n")  # robust to autocrlf checkouts
    assert hashlib.sha256(text.encode("utf-8")).hexdigest() == HOLDOUT_SHA256


def test_holdout_cases_are_tagged_and_not_marked_as_limitations():
    cases = load_dataset(HOLDOUT)
    assert len(cases) == 40
    assert all("holdout" in c.tags and not c.known_limitation for c in cases)


def test_holdout_regression_floor(holdout_report):
    """Measured after the fixes: 39/40 categories, TP 66 / FP 4 / FN 5. Not a quality claim."""
    d = holdout_report.to_dict()["all_cases"]
    assert d["classification"]["correct"] >= 39
    ex = d["extraction"]["overall"]
    assert ex["tp"] >= 66 and ex["fp"] <= 4 and ex["fn"] <= 5


def test_no_dataset_got_worse_than_before_the_fixes(dev_report, challenge_report, holdout_report):
    """Classification and extraction must not regress on ANY dataset (no hidden degradation)."""
    for name, rep in (("dataset_v1", dev_report), ("dataset_challenge_v1", challenge_report),
                      ("dataset_holdout_v1", holdout_report)):
        d = rep.to_dict()["all_cases"]
        ex = d["extraction"]["overall"]
        base = BASELINE_BEFORE_FIXES[name]
        assert d["classification"]["correct"] >= base["classification"], name
        assert ex["tp"] >= base["tp"] and ex["fp"] <= base["fp"] and ex["fn"] <= base["fn"], name


def test_no_fully_correct_holdout_case_depends_on_a_known_failure_mode(holdout_report):
    """Cases written to probe the fixes use different sentences than the development sets."""
    dev_bodies = {c.body for c in load_dataset(DEV)} | {c.body for c in load_dataset(CHALLENGE)}
    assert not [c.id for c in load_dataset(HOLDOUT) if c.body in dev_bodies]


def test_every_non_valid_field_carries_a_reason_on_all_three_datasets(dev_report, challenge_report, holdout_report):
    for rep in (dev_report, challenge_report, holdout_report):
        for r in rep.results:
            for f in r.result.fields:
                if f.status is not FieldStatus.VALID:
                    assert f.warnings, (r.case.id, f.name.value, f.value.normalized)
