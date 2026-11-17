"""Start the UI:  python -m ai_ops_hub.ui [--api-url URL] [--port N]

Streamlit listens on 127.0.0.1 only and sends no usage statistics. The UI needs the FastAPI
backend to be running (python -m ai_ops_hub.api); it never touches the database itself.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from ai_ops_hub.ui.client import DEFAULT_BASE_URL, _check_base_url


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ai_ops_hub.ui")
    ap.add_argument("--api-url", default=os.environ.get("AIOPS_API_URL", DEFAULT_BASE_URL))
    ap.add_argument("--port", type=int, default=8501)
    args = ap.parse_args(argv)

    try:
        api_url = _check_base_url(args.api_url, allow_remote=False)
    except ValueError as exc:
        print(f"error: {exc}")
        return 2
    os.environ["AIOPS_API_URL"] = api_url

    from streamlit.web import cli as stcli

    script = str(Path(__file__).with_name("streamlit_app.py"))
    sys.argv = [
        "streamlit", "run", script,
        "--server.address", "127.0.0.1",
        "--server.port", str(args.port),
        "--server.headless", "true",
        "--browser.gatherUsageStats", "false",
    ]
    return int(stcli.main() or 0)


if __name__ == "__main__":
    raise SystemExit(main())
