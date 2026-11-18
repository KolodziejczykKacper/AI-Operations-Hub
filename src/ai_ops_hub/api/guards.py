"""Browser cross-site request guard (pure ASGI).

The API has no authentication and listens on localhost. A web page the user happens to open in the same browser
could still make the browser send requests to http://127.0.0.1:8000 (CSRF). Browsers label every such request with
``Sec-Fetch-Site``; for any state-changing method we accept only ``same-origin`` (Swagger UI) or ``none`` (typed in the
address bar) and refuse ``cross-site`` / ``same-site`` (another port on localhost counts as same-site).

Requests without the header (the Streamlit server, scripts, curl, tests) are not browser requests and pass: this guard
protects against hostile web pages, it is NOT authentication.
"""

from __future__ import annotations

import json

from starlette.types import ASGIApp, Receive, Scope, Send

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
ALLOWED_SITES = {b"same-origin", b"none"}


class CrossSiteGuardMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] not in SAFE_METHODS:
            site = dict(scope["headers"]).get(b"sec-fetch-site")
            if site is not None and site.lower() not in ALLOWED_SITES:
                body = json.dumps({"error": {
                    "code": "cross_site_request_refused",
                    "message": "state-changing requests from another web origin are refused",
                    "details": None,
                }}).encode()
                await send({"type": "http.response.start", "status": 403, "headers": [
                    (b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)
