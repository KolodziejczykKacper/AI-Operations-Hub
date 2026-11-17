"""Text helpers shared by the rule-based components.

``fold`` produces a lowercase, diacritics-free copy used ONLY for matching. It
is always exactly as long as the input, so match offsets are valid offsets into
the original text and provenance spans stay exact.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ai_ops_hub.analysis.schemas import AnalysisWarning, FieldName, WarningCode
from ai_ops_hub.schemas import SourceSpan, SpanSource

_FOLD_MAP = str.maketrans(
    "ąćęłńóśźżĄĆĘŁŃÓŚŹŻ’‘“”–—",
    "acelnoszzacelnoszz''\"\"--",
)


def fold(text: str) -> str:
    """Lowercase, strip Polish diacritics, normalise quotes/dashes. 1:1 by length."""
    out: list[str] = []
    for ch in text.translate(_FOLD_MAP):
        low = ch.lower()
        out.append(low if len(low) == 1 else ch)
    folded = "".join(out)
    assert len(folded) == len(text)
    return folded


_NEGATORS = {"nie", "not", "no", "bez", "without", "never", "brak", "nigdy"}
_CLAUSE_END = re.compile(r"[,;:.!?\n]")


def is_negated(folded: str, start: int, max_words: int = 3) -> bool:
    """True when one of the ``max_words`` words before ``start`` (in the same
    clause) is a negator: "to nie jest reklamacja", "this is not a complaint",
    "no complaint", "nie zamawiamy". Deliberately simple: it covers the common
    negation frames only, not scope or double negation."""
    prefix = folded[max(0, start - 40):start]
    last = None
    for last in _CLAUSE_END.finditer(prefix):
        pass
    if last is not None:
        prefix = prefix[last.end():]
    words = re.findall(r"[\w']+", prefix)[-max_words:]
    return any(w in _NEGATORS or w.endswith("n't") for w in words)


@dataclass(frozen=True)
class Segment:
    """One analysable text (subject or body).

    ``original`` is what spans refer to. ``text`` is the same string with
    suspicious lines blanked out (same length); ``folded`` is ``fold(text)``.
    """

    source: SpanSource
    original: str
    text: str
    folded: str

    @classmethod
    def build(cls, source: SpanSource, original: str, masked_ranges: list[tuple[int, int]]) -> "Segment":
        chars = list(original)
        for start, end in masked_ranges:
            for i in range(start, end):
                if chars[i] not in "\n\r":
                    chars[i] = " "
        masked = "".join(chars)
        return cls(source, original, masked, fold(masked))

    def span(self, start: int, end: int) -> SourceSpan:
        return SourceSpan(
            start=start, end=end, text=self.original[start:end], source=self.source
        )


def make_warning(
    code: WarningCode,
    message: str,
    segment: Segment | None = None,
    start: int | None = None,
    end: int | None = None,
    field: FieldName | None = None,
) -> AnalysisWarning:
    span = segment.span(start, end) if segment is not None and start is not None and end else None
    return AnalysisWarning(code=code, message=message, span=span, field=field)
