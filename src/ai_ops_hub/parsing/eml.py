"""Parser for RFC 5322 / MIME ``.eml`` files (stdlib only, offline)."""

from __future__ import annotations

import hashlib
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser

from ai_ops_hub.config import Settings, get_settings
from ai_ops_hub.dedup import normalize_message_id
from ai_ops_hub.errors import EmptyInputError, InputTooLargeError, MalformedMessageError
from ai_ops_hub.parsing.common import (
    REQUIRED_HEADERS,
    canonicalize_body,
    html_to_text,
    parse_addresses,
    parse_date,
)
from ai_ops_hub.schemas import ParsedMessage, SourceKind


def _header_values(msg: EmailMessage, name: str, warnings: list[str]) -> list[str]:
    try:
        return [str(v) for v in (msg.get_all(name) or [])]
    except Exception as exc:  # noqa: BLE001 - malformed headers must not crash ingestion
        warnings.append(f"header '{name}' unreadable ({type(exc).__name__})")
        return []


def _part_text(part: EmailMessage, warnings: list[str]) -> str:
    if part.get_content_charset() is None:
        # No declared charset: the stdlib would assume US-ASCII and mangle
        # non-ASCII bytes, so try UTF-8 explicitly.
        payload = part.get_payload(decode=True) or b""
        if payload.isascii():
            return payload.decode("ascii")
        try:
            text = payload.decode("utf-8")
            warnings.append("charset not declared; decoded as utf-8")
            return text
        except UnicodeDecodeError:
            warnings.append("charset not declared and not valid utf-8; replacement characters used")
            return payload.decode("utf-8", errors="replace")
    try:
        return part.get_content()
    except (LookupError, UnicodeError, ValueError, KeyError):
        payload = part.get_payload(decode=True) or b""
        warnings.append("body decoded with utf-8/replacement fallback (bad charset or encoding)")
        return payload.decode("utf-8", errors="replace")


def parse_eml(data: bytes, settings: Settings | None = None) -> ParsedMessage:
    settings = settings or get_settings()
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError("parse_eml expects bytes")
    if not bytes(data).strip():
        raise EmptyInputError("empty .eml input")
    if len(data) > settings.max_eml_bytes:
        raise InputTooLargeError(
            f".eml is {len(data)} bytes; limit is {settings.max_eml_bytes}"
        )

    try:
        msg = BytesParser(policy=policy.default).parsebytes(bytes(data))
    except (RecursionError, ValueError, TypeError, UnicodeError) as exc:
        raise MalformedMessageError(f"cannot parse MIME structure: {type(exc).__name__}") from exc

    if len(msg.keys()) > settings.max_headers:
        raise InputTooLargeError(f"too many headers (limit {settings.max_headers})")
    try:
        parts = 0
        for _ in msg.walk():
            parts += 1
            if parts > settings.max_mime_parts:
                raise InputTooLargeError(f"too many MIME parts (limit {settings.max_mime_parts})")
    except RecursionError as exc:
        raise MalformedMessageError("MIME nesting too deep") from exc

    present = {k.lower() for k in msg.keys()}
    if not present & set(REQUIRED_HEADERS):
        raise MalformedMessageError("no recognizable email headers found")

    warnings: list[str] = []
    missing = tuple(h for h in REQUIRED_HEADERS if h not in present)

    mids = _header_values(msg, "Message-ID", warnings)
    if len(mids) > 1:
        warnings.append("multiple Message-ID headers; first one used")
    mid_raw = mids[0] if mids else None

    subjects = _header_values(msg, "Subject", warnings)
    from_addrs = parse_addresses(_header_values(msg, "From", warnings))
    to_addrs = parse_addresses(_header_values(msg, "To", warnings))
    dates = _header_values(msg, "Date", warnings)
    date_raw = dates[0] if dates else None
    date = parse_date(date_raw)
    if date_raw and date is None:
        warnings.append("Date header could not be parsed")

    for defect in msg.defects:
        warnings.append(f"MIME defect: {type(defect).__name__}")

    body = ""
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        warnings.append("no text body part found")
    else:
        body = _part_text(part, warnings)
        if part.get_content_type() == "text/html":
            body = html_to_text(body)
            warnings.append("body derived from HTML (no text/plain part)")
    body = canonicalize_body(body)
    if not body:
        warnings.append("empty body")

    return ParsedMessage(
        source_kind=SourceKind.EML,
        message_id=normalize_message_id(mid_raw),
        message_id_raw=mid_raw,
        subject=subjects[0] if subjects else None,
        from_addr=from_addrs[0] if from_addrs else None,
        to_addrs=to_addrs,
        date_raw=date_raw,
        date=date,
        body_text=body,
        raw_sha256=hashlib.sha256(bytes(data)).hexdigest(),
        missing_headers=missing,
        warnings=tuple(warnings),
    )
