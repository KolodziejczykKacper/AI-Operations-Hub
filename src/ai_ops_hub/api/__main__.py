"""Start the local API:  python -m ai_ops_hub.api [--db FILE] [--port N]

Binds to 127.0.0.1 by default. There is no authentication, so listening on any other
address is refused unless --allow-remote is given explicitly.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn

from ai_ops_hub.api.app import create_app
from ai_ops_hub.config import get_settings

LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    ap = argparse.ArgumentParser(prog="ai_ops_hub.api")
    ap.add_argument("--db", help="SQLite file (default: AIOPS_DATABASE_URL)")
    ap.add_argument("--host", default=settings.api_host)
    ap.add_argument("--port", type=int, default=settings.api_port)
    ap.add_argument("--allow-remote", action="store_true", help="DANGEROUS: the API has no authentication")
    args = ap.parse_args(argv)

    if args.host not in LOOPBACK and not args.allow_remote:
        print(f"refusing to listen on {args.host!r}: the API has no authentication (use --allow-remote to override)")
        return 2

    if settings.claude_enabled and args.host not in LOOPBACK:
        # The API has no authentication and the paid Claude endpoint takes its consent from the request body, so any
        # machine that can reach the port could spend your money. Never combine the two.
        print("refusing to listen on a non-local address while Claude analysis is enabled (no authentication, paid API)")
        return 2

    if args.db:
        settings = settings.model_copy(update={"database_url": f"sqlite:///{Path(args.db).as_posix()}"})
    app = create_app(settings=settings)
    print(f"Swagger UI: http://{args.host}:{args.port}/docs   (no authentication; local demo only)")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
