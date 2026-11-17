"""Request-size guard (pure ASGI, so it also stops chunked uploads without Content-Length).

The body is counted while it streams in; the request is refused with 413 as soon as
the limit is crossed, before the application ever sees the whole payload.
"""

from __future__ import annotations

import json

from starlette.types import ASGIApp, Message, Receive, Scope, Send


class _TooLarge(Exception):
    pass


def _envelope(limit: int) -> bytes:
    return json.dumps(
        {
            "error": {
                "code": "payload_too_large",
                "message": f"request body exceeds {limit} bytes",
                "details": {"limit_bytes": limit},
            }
        }
    ).encode()


class BodySizeLimitMiddleware:
    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        declared = dict(scope["headers"]).get(b"content-length")
        if declared is not None:
            try:
                too_big = int(declared) > self.max_bytes
            except ValueError:
                too_big = False  # malformed header: the server layer rejects it
            if too_big:
                await self._reject(send)
                return

        seen = 0
        started = False

        async def counting_receive() -> Message:
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > self.max_bytes:
                    raise _TooLarge
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, counting_receive, tracking_send)
        except _TooLarge:
            if not started:
                await self._reject(send)

    async def _reject(self, send: Send) -> None:
        body = _envelope(self.max_bytes)
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
