from ai_ops_hub import cli

from conftest import SAMPLES


def run(capsys, db, *args):
    code = cli.main(["--db", str(db), *map(str, args)])
    return code, capsys.readouterr().out


def test_cli_import_list_audit_and_reimport(capsys, db_dir):
    db = db_dir / "cli.db"
    s1 = SAMPLES / "01_invoice_dispute_pl.eml"
    s2 = SAMPLES / "02_invoice_dispute_pl_conflict.eml"

    code, out = run(capsys, db, "import", s1, s2)
    assert code == 0
    assert "01_invoice_dispute_pl.eml: NEW -> id=1" in out
    assert "02_invoice_dispute_pl_conflict.eml: CONFLICT -> id=2" in out
    assert "canonical id=1" in out

    _, out = run(capsys, db, "list")
    assert "id=1 import_status=imported" in out and "id=2 import_status=conflict" in out

    _, out = run(capsys, db, "audit", 1)
    assert "message_imported" in out and "conflict_detected" in out

    code, out = run(capsys, db, "import", s1, s2)  # second run, separate process-like call
    assert code == 0
    assert out.count("DUPLICATE") == 2


def test_cli_reports_bad_input_and_continues(capsys, db_dir):
    bad = db_dir / "bad.eml"
    bad.write_bytes(b"")
    code, out = run(capsys, db_dir / "cli.db", "import", bad, SAMPLES / "09_payment_reminder_en.txt")
    assert code == 1
    assert "ERROR EmptyInputError" in out
    assert "09_payment_reminder_en.txt: NEW" in out


# --- Stage 3: analysis commands -------------------------------------------------


def test_cli_import_with_analyze_analyzes_new_messages_but_never_conflicts(capsys, db_dir):
    db = db_dir / "analysis.db"
    s1 = SAMPLES / "01_invoice_dispute_pl.eml"
    s2 = SAMPLES / "02_invoice_dispute_pl_conflict.eml"

    code, out = run(capsys, db, "import", "--analyze", s1, s2)
    assert code == 0
    assert "message 1: COMPLAINT [analysis_method=rule_based" in out
    assert "reference: 'FV/2026/0042'" in out and "amount: '12500.00 PLN'" in out
    assert "heuristic rule scores (not probabilities)" in out
    assert "(conflicting import: not analyzed)" in out
    assert "message 2:" not in out  # the conflict was not analyzed

    code, out = run(capsys, db, "analyze", 2)  # explicit request for the conflict is refused
    assert code == 1 and "REFUSED ConflictNotAnalyzableError" in out

    code, out = run(capsys, db, "analyze", 1)  # idempotent re-run reports the existing analysis
    assert code == 0 and "existing" in out


def test_cli_analyze_pending_only_touches_imported_messages(capsys, db_dir):
    db = db_dir / "pending.db"
    run(capsys, db, "import", SAMPLES / "01_invoice_dispute_pl.eml", SAMPLES / "02_invoice_dispute_pl_conflict.eml",
        SAMPLES / "03_contract_renewal_en.eml")
    code, out = run(capsys, db, "analyze", "--pending")
    assert code == 0
    assert out.count("[analysis_method=rule_based") == 2  # messages 1 and 3, not the conflict (2)
    assert "message 2:" not in out
    code, out = run(capsys, db, "analyze", "--pending")
    assert code == 0 and out.strip() == ""  # nothing left


def test_cli_analyze_requires_a_target(capsys, db_dir):
    code, out = run(capsys, db_dir / "x.db", "analyze")
    assert code == 2 and "MESSAGE_ID" in out


def test_cli_audit_shows_the_analysis_event(capsys, db_dir):
    db = db_dir / "audit.db"
    run(capsys, db, "import", "--analyze", SAMPLES / "03_contract_renewal_en.eml")
    _, out = run(capsys, db, "audit", 1)
    assert "message_imported" in out and "analysis_completed" in out
