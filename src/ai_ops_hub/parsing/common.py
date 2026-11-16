"""Helpers shared by the .eml and pasted-text parsers."""

from __future__ import annotations

import re
from datetime import datetime
from email.utils import getaddresses, parsedate_to_datetime
from html.parser import HTMLParser

REQUIRED_HEADERS = ("message-id", "from", "to", "subject", "date")


_BLOCK_TAGS = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table"}


class _HtmlToText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._out: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self._out.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._skip:
            self._skip -= 1
        elif tag in _BLOCK_TAGS:
            self._out.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self._out.append(data)

    def text(self) -> str:
        return "".join(self._out)


def html_to_text(html: str) -> str:
    parser = _HtmlToText()
    parser.feed(html)
    parser.close()
    return re.sub(r"\n{3,}", "\n\n", parser.text())


def canonicalize_body(text: str) -> str:
    """Canonical body: LF line endings, no NUL bytes, outer whitespace trimmed.

    Source spans index into the result of this function.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    return text.strip()


def parse_date(raw: str | None) -> datetime | None:
    if not raw or not raw.strip():
        return None
    raw = raw.strip()
    try:
        return parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        pass
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def parse_addresses(raw_values: list[str]) -> tuple[str, ...]:
    out: list[str] = []
    for _name, addr in getaddresses(raw_values):
        addr = addr.strip().lower()
        if addr and addr not in out:
            out.append(addr)
    return tuple(out)
