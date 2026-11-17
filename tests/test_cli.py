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
