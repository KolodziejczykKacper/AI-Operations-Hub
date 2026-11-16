"""Deterministic deduplication keys and explicit conflict rules.

Rules
-----
* Key: normalized Message-ID when present (``mid:<id>``); otherwise a
  fallback key derived from content and the raw Date header (``fp:<sha256>``).
* Fingerprint: sha256 over normalized subject, sender and body. The Date header
  is excluded so a re-delivered copy of the same message is a duplicate.
* Known key + same fingerprint  -> DUPLICATE (safe to link, not to drop).
* Known key + different fingerprint -> CONFLICT. Never silently discarded;
  a human must resolve it.
* Unknown key -> NEW.
* Messages without Message-ID can never conflict (their key IS their content).
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Mapping

from ai_ops_hub.schemas import DedupDecision, DedupResult, ParsedMessage

_WS = re.compile(r"\s+")
_MID_BRACKETED = re.compile(r"<([^<>\s]+)>")


def normalize_message_id(raw: str | None) -> str | None:
    """Strip brackets/whitespace and lowercase the domain part.

    The local part stays case-sensitive (RFC 5322 allows it to be). Returns
    ``None`` for empty or unusable values.
    """
    if raw is None:
        return None
    s = raw.strip()
    m = _MID_BRACKETED.search(s)
    s = m.group(1) if m else s.strip("<> \t")
    if not s or _WS.search(s):
        return None
    local, sep, domain = s.rpartition("@")
    return f"{local}@{domain.lower()}" if sep else s


def _norm_text(value: str | None) -> str:
    if not value:
        return ""
    return _WS.sub(" ", unicodedata.normalize("NFC", value)).strip()


def content_fingerprint(msg: ParsedMessage) -> str:
    parts = (
        _norm_text(msg.subject),
        (msg.from_addr or "").strip().lower(),
        _norm_text(msg.body_text),
    )
    payload = "\x1f".join(parts).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def dedup_key(msg: ParsedMessage) -> str:
    mid = normalize_message_id(msg.message_id) or normalize_message_id(msg.message_id_raw)
    if mid:
        return f"mid:{mid}"
    payload = f"{content_fingerprint(msg)}\x1f{(msg.date_raw or '').strip()}"
    return "fp:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def classify(msg: ParsedMessage, known: Mapping[str, str]) -> DedupResult:
    """Compare ``msg`` with already-known messages (``key -> fingerprint``).

    Pure function: no storage, no side effects.
    """
    key = dedup_key(msg)
    fp = content_fingerprint(msg)
    existing = known.get(key)
    if existing is None:
        return DedupResult(
            decision=DedupDecision.NEW, key=key, fingerprint=fp, reason="key not seen before"
        )
    if existing == fp:
        return DedupResult(
            decision=DedupDecision.DUPLICATE,
            key=key,
            fingerprint=fp,
            existing_fingerprint=existing,
            reason="same key and identical content",
        )
    return DedupResult(
        decision=DedupDecision.CONFLICT,
        key=key,
        fingerprint=fp,
        existing_fingerprint=existing,
        reason="same key but different content; requires human review",
    )
