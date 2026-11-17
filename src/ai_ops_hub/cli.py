"""Tiny developer CLI for manually checking persistence and deduplication.

    python -m ai_ops_hub.cli --db data/local/demo.db import FILE [FILE ...]
    python -m ai_ops_hub.cli --db data/local/demo.db list
    python -m ai_ops_hub.cli --db data/local/demo.db audit MESSAGE_ID

Not an API and not a UI: it only calls the same ImportService the API will use.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ai_ops_hub.config import get_settings
from ai_ops_hub.db.engine import create_db_engine, init_db
from ai_ops_hub.errors import AIOpsError
from ai_ops_hub.parsing import parse_eml, parse_pasted_text
from ai_ops_hub.services.import_service import ImportService


def _parse_file(path: Path):
    if path.suffix.lower() == ".eml":
        return parse_eml(path.read_bytes())
    return parse_pasted_text(path.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(prog="ai_ops_hub.cli")
    ap.add_argument("--db", help="SQLite file path (default: AIOPS_DATABASE_URL)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_imp = sub.add_parser("import", help="import .eml / .txt files")
    p_imp.add_argument("files", nargs="+", type=Path)
    sub.add_parser("list", help="list stored messages")
    p_aud = sub.add_parser("audit", help="show audit history of one message")
    p_aud.add_argument("message_id", type=int)
    args = ap.parse_args(argv)

    url = f"sqlite:///{Path(args.db).as_posix()}" if args.db else get_settings().database_url
    engine = create_db_engine(url)
    try:
        init_db(engine)
        service = ImportService(engine)

        if args.cmd == "import":
            exit_code = 0
            for f in args.files:
                try:
                    r = service.import_message(_parse_file(f))
                except (AIOpsError, OSError, UnicodeError) as exc:
                    print(f"{f.name}: ERROR {type(exc).__name__}: {exc}")
                    exit_code = 1
                    continue
                extra = (
                    f" (canonical id={r.existing_message.id})" if r.existing_message else ""
                )
                print(
                    f"{f.name}: {r.decision.value.upper()} -> id={r.message.id} "
                    f"import_status={r.message.import_status.value}{extra}"
                )
            return exit_code

        if args.cmd == "list":
            for m in service.list_messages(limit=1000):
                print(
                    f"id={m.id} import_status={m.import_status.value} key={m.dedup_key[:48]} "
                    f"subject={m.subject!r} imported_at={m.imported_at.isoformat()}"
                )
            return 0

        for e in service.audit_history(args.message_id):
            print(f"{e.id} {e.timestamp.isoformat()} {e.actor.value} {e.event_type.value} {e.details}")
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
