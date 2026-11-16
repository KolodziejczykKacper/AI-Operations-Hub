"""Parser for pasted message text, with optional PL/EN header block."""

from __future__ import annotations

import hashlib
import re

from ai_ops_hub.config import Settings, get_settings
from ai_ops_hub.dedup import normalize_message_id
from ai_ops_hub.errors import EmptyInputError, InputTooLargeError
from ai_ops_hub.parsing.common import (
    REQUIRED_HEADERS,
    canonicalize_body,
    parse_addresses,
    parse_date,
)
from ai_ops_hub.schemas import ParsedMessage, SourceKind

_LABELS = {
    "from": "from",
    "od": "from",
    "nadawca": "from",
    "to": "to",
    "do": "to",
    "odbiorca": "to",
    "subject": "subject",
    "temat": "subject",
    "date": "date",
    "data": "date",
    "message-id": "message-id",
    "id wiadomości": "message-id",
}
_HEADER_LINE = re.compile(r"^([^:\s][^:]{0,30}):[ \t]*(.*)$")


def _split_header_block(lines: list[str]) -> tuple[dict[str, str], int]:
    """Return (headers, index of first body line). Headers are only recognised
    at the very top, using known PL/EN labels."""
    headers: dict[str, str] = {}
    i = 0
    last: str | None = None
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            if headers:
                i += 1  # blank line terminates the header block
            break
        if line[0] in " \t" and last:
            headers[last] += " " + line.strip()  # folded continuation
            i += 1
            continue
        m = _HEADER_LINE.match(line)
        label = _LABELS.get(m.group(1).strip().lower()) if m else None
        if not label:
            break
        if label not in headers:
            headers[label] = m.group(2).strip()
            last = label
        i += 1
    return headers, i


def parse_pasted_text(text: str, settings: Settings | None = None) -> ParsedMessage:
    settings = settings or get_settings()
    if not isinstance(text, str):
        raise TypeError("parse_pasted_text expects str")
    if len(text) > settings.max_text_chars:
        raise InputTooLargeError(
            f"text is {len(text)} characters; limit is {settings.max_text_chars}"
        )
    warnings: list[str] = []
    if "\x00" in text:
        warnings.append("NUL characters removed")
    # Normalise line endings first so header detection and body offsets agree.
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    if not normalized.strip():
        raise EmptyInputError("empty pasted text")

    lines = normalized.lstrip("\n").split("\n")
    headers, body_start = _split_header_block(lines)
    body = canonicalize_body("\n".join(lines[body_start:]))
    if not body:
        warnings.append("empty body")

    mid_raw = headers.get("message-id")
    date_raw = headers.get("date")
    date = parse_date(date_raw)
    if date_raw and date is None:
        warnings.append("Date could not be parsed")
    from_addrs = parse_addresses([headers["from"]]) if "from" in headers else ()
    to_addrs = parse_addresses([headers["to"]]) if "to" in headers else ()
    missing = tuple(h for h in REQUIRED_HEADERS if h not in headers)

    return ParsedMessage(
        source_kind=SourceKind.PASTED_TEXT,
        message_id=normalize_message_id(mid_raw),
        message_id_raw=mid_raw,
        subject=headers.get("subject"),
        from_addr=from_addrs[0] if from_addrs else None,
        to_addrs=to_addrs,
        date_raw=date_raw,
        date=date,
        body_text=body,
        raw_sha256=hashlib.sha256(text.encode("utf-8", errors="surrogatepass")).hexdigest(),
        missing_headers=missing,
        warnings=tuple(warnings),
    )
