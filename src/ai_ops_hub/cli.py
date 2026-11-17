"""Tiny developer CLI for manually checking persistence and deduplication.

    python -m ai_ops_hub.cli --db data/local/demo.db import FILE [FILE ...]
    python -m ai_ops_hub.cli --db data/local/demo.db list
    python -m ai_ops_hub.cli --db data/local/demo.db audit MESSAGE_ID
    python -m ai_ops_hub.cli --db data/local/demo.db import --analyze FILE [FILE ...]
    python -m ai_ops_hub.cli --db data/local/demo.db analyze [--pending | MESSAGE_ID ...]

Not an API and not a UI: it only calls the same ImportService the API will use.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ai_ops_hub.analysis import RuleBasedAnalyzer
from ai_ops_hub.analysis.schemas import StoredAnalysis
from ai_ops_hub.config import get_settings
from ai_ops_hub.db.engine import create_db_engine, init_db
from ai_ops_hub.errors import AIOpsError
from ai_ops_hub.parsing import parse_eml, parse_pasted_text
from ai_ops_hub.schemas import DedupDecision
from ai_ops_hub.services.analysis_service import AnalysisService
from ai_ops_hub.services.import_service import ImportService


def _parse_file(path: Path):
    if path.suffix.lower() == ".eml":
        return parse_eml(path.read_bytes())
    return parse_pasted_text(path.read_text(encoding="utf-8"))


def _print_analysis(a: StoredAnalysis, created: bool) -> None:
    r = a.result
    state = "new" if created else "existing"
    print(
        f"message {a.message_id}: {a.category.value.upper()} "
        f"[analysis_method={a.analysis_method.value}, {a.analyzer_name} {a.analyzer_version}, {state}] "
        f"requires_review={a.requires_review}"
    )
    scores = ", ".join(f"{c.value}={v}" for c, v in r.heuristic_scores.items() if v)
    print(f"  heuristic rule scores (not probabilities): {scores or 'none'}")
    for f in r.fields:
        sp = f.value.span
        print(
            f"  {f.name.value}: {f.value.normalized!r} [{f.status.value}] "
            f"<- {sp.text!r} ({sp.source.value} {sp.start}-{sp.end})"
        )
        for w in f.warnings:
            print(f"      warning {w.code.value}: {w.message}")
    for w in r.warnings:
        where = f" <- {w.span.text!r}" if w.span else ""
        print(f"  warning {w.code.value}{where}")
    for rr in r.review_reasons:
        print(f"  REVIEW {rr.code.value}: {rr.message}")


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(prog="ai_ops_hub.cli")
    ap.add_argument("--db", help="SQLite file path (default: AIOPS_DATABASE_URL)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_imp = sub.add_parser("import", help="import .eml / .txt files")
    p_imp.add_argument("files", nargs="+", type=Path)
    p_imp.add_argument(
        "--analyze", action="store_true",
        help="also run the rule-based analyzer on newly imported messages (never on conflicts)",
    )
    sub.add_parser("list", help="list stored messages")
    p_aud = sub.add_parser("audit", help="show audit history of one message")
    p_aud.add_argument("message_id", type=int)
    p_ana = sub.add_parser("analyze", help="run the rule-based analyzer on stored messages")
    p_ana.add_argument("message_ids", nargs="*", type=int)
    p_ana.add_argument("--pending", action="store_true", help="all imported messages not yet analyzed")
    args = ap.parse_args(argv)

    url = f"sqlite:///{Path(args.db).as_posix()}" if args.db else get_settings().database_url
    engine = create_db_engine(url)
    try:
        init_db(engine)
        service = ImportService(engine)
        analysis = AnalysisService(engine, RuleBasedAnalyzer())

        if args.cmd == "analyze":
            if not args.pending and not args.message_ids:
                print("give MESSAGE_ID(s) or --pending")
                return 2
            exit_code = 0
            outcomes = [] if not args.pending else analysis.analyze_pending(limit=1000)
            for o in outcomes:
                _print_analysis(o.analysis, o.created)
            for mid in args.message_ids:
                try:
                    o = analysis.analyze_message(mid)
                except AIOpsError as exc:
                    print(f"message {mid}: REFUSED {type(exc).__name__}: {exc}")
                    exit_code = 1
                    continue
                _print_analysis(o.analysis, o.created)
            return exit_code

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
                if args.analyze and r.decision is DedupDecision.NEW:
                    o = analysis.analyze_message(r.message.id)
                    _print_analysis(o.analysis, o.created)
                elif args.analyze and r.decision is DedupDecision.CONFLICT:
                    print("  (conflicting import: not analyzed)")
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
