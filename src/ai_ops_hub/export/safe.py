"""Neutralise untrusted text before it goes into a spreadsheet cell (formula injection).

Email text, sender names and human-typed corrections are untrusted. A cell whose text starts
with ``=``, ``+``, ``-``, ``@`` (or a tab / carriage return that hides them) can be run as a
formula by some spreadsheet programs. (CSV export does not exist; if it is ever added, its safety
must be designed and tested separately: nothing here has been verified for CSV.)

Defences, all applied here:
* control characters and invisible format characters (zero-width space, BOM, bidi marks, ...)
  are removed, so they can not hide a dangerous first character from a naive check;
* if the first visible character is dangerous, the text gets a leading apostrophe. That is
  the usual spreadsheet convention for "this is text" (this is not claimed to be sufficient for CSV);
* the writer ALSO stores every string cell with an explicit string type, so it is never a formula cell.

Plain signed numbers such as ``+48600100200`` are left alone: they can not form a formula.
"""

from __future__ import annotations

import re
import unicodedata

MAX_CELL_CHARS = 32_000  # Excel's hard limit is 32 767 characters per cell
TRUNCATION_MARK = " …[obcięto]"

_DANGEROUS_FIRST = frozenset("=+-@|＝＋－＠｜")
_PLAIN_SIGNED_NUMBER = re.compile(r"^[+-]?\d+(?:[ .,]\d+)*$")


def clean(text: str, *, multiline: bool = False) -> str:
    """Remove control / invisible / unencodable characters. Newlines survive only if ``multiline``."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    out: list[str] = []
    for ch in text:
        cat = unicodedata.category(ch)
        if ch == "\n":
            out.append("\n" if multiline else " ")
        elif ch == "\t" or cat in ("Zl", "Zp"):
            out.append(" ")
        elif cat in ("Cc", "Cf", "Cs", "Co", "Cn"):
            continue
        else:
            out.append(ch)
    return "".join(out)


def first_visible(text: str) -> str:
    for ch in text:
        if not ch.isspace():
            return ch
    return ""


def needs_guard(text: str) -> bool:
    cleaned = clean(text, multiline=True)
    if first_visible(cleaned) not in _DANGEROUS_FIRST:
        return False
    return _PLAIN_SIGNED_NUMBER.match(cleaned.strip()) is None


def safe_text(value: object, *, multiline: bool = False) -> str:
    """Text that is safe to store in a cell: cleaned, clipped, and never formula-like."""
    text = clean("" if value is None else str(value), multiline=multiline)
    if len(text) > MAX_CELL_CHARS:
        text = text[: MAX_CELL_CHARS - len(TRUNCATION_MARK)] + TRUNCATION_MARK
    if needs_guard(text):
        text = "'" + text
    return text
