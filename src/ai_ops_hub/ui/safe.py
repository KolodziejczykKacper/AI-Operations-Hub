"""Helpers for showing UNTRUSTED text (email content, extracted values, server messages).

Rule of the UI: untrusted text is shown with ``st.text`` / ``st.code`` / ``st.dataframe``
(which never interpret markup). Where Streamlit only offers markdown (alerts, captions,
labels), the text goes through ``esc`` first. ``unsafe_allow_html`` is never used.
"""

from __future__ import annotations

import re

_MD_SPECIAL = re.compile(r"([\\`*_{}\[\]()<>#+\-.!|~$&:@/])")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def esc(text: object) -> str:
    """Make ``text`` render literally inside markdown (no links, images, emphasis, HTML, emoji codes)."""
    s = _CONTROL.sub("", "" if text is None else str(text))
    return _MD_SPECIAL.sub(r"\\\1", s)


def clip(text: str, limit: int = 120) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def context_snippet(body: str, start: int, end: int, radius: int = 35) -> str:
    """The source fragment with a little surrounding text; the match is wrapped in ``[[ ]]``.

    Plain text on purpose: the caller shows it with ``st.text`` / a dataframe cell.
    """
    if not (0 <= start < end <= len(body)):
        return body[start:end] if body[start:end] else ""
    left = body[max(0, start - radius) : start]
    right = body[end : end + radius]
    pre = "…" if start - radius > 0 else ""
    post = "…" if end + radius < len(body) else ""
    return " ".join(f"{pre}{left}[[{body[start:end]}]]{right}{post}".split())
