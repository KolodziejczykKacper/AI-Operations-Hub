"""Detection of instruction-like text addressed to the analysis system.

Email content is DATA. A message that says "ignore previous instructions" or
"approve this case" must not steer classification or extraction. The analyzer
cannot be talked into anything (it only matches patterns), but injected lines
could still add keyword hits, so they are blanked out before analysis and the
message is flagged for human review.
"""

from __future__ import annotations

import re

from ai_ops_hub.analysis.text import fold

# Folded (lowercase, no diacritics). Patterns are deliberately high-precision:
# ordinary business phrasing such as "please send us an email" is not flagged.
_PATTERNS = [
    r"ignor\w* (?:all |any |the |your )?(?:previous|prior|above|earlier|preceding)\b",
    r"disregard (?:all |any |the |your )?(?:previous|prior|above|earlier|preceding)\b",
    r"forget (?:all |any |your |the )?(?:previous |prior |earlier )?(?:instructions?|rules?|prompts?)\b",
    r"\byou are now\b",
    r"\bact as (?:an? |the )?(?:system|admin\w*|developer|root)\b",
    r"\bsystem prompt\b",
    r"(?:^|\n)\s*(?:system|assistant|developer)\s*:",
    r"\bnew instructions?\s*:",
    r"\b(?:classify|categori[sz]e|label|mark|tag)\s+(?:this|it|the (?:email|message|case|request))\s+as\b",
    r"\b(?:auto-?)?approve\b.{0,30}\b(?:this|the|my) (?:case|request|order|refund|claim|complaint)\b",
    r"\bmark\b.{0,20}\bas approved\b",
    r"\b(?:do not|don't|skip|bypass|avoid)\s+(?:flag\w*|human review|manual review|review)\b",
    # Polish
    r"\bzignoruj\w*\b.{0,40}\b(?:instrukcj\w*|polecen\w*|zasad\w*|poprzedni\w*)",
    r"\bpomin\w* (?:wszystkie )?(?:poprzednie|powyzsze) (?:instrukcje|polecenia|zasady)",
    r"\bjestes teraz\b",
    r"\b(?:sklasyfikuj|zaklasyfikuj|oznacz)\s+(?:to|ta|te|ten)\b.{0,25}\bjako\b",
    r"\bzatwierdz\w* (?:te|ta|ten|moje|moja|moj)\b.{0,20}\b(?:sprawe|zamowienie|reklamacje|wniosek|zgloszenie)",
    r"\bpolecenie systemowe\b",
    r"\bnowe instrukcje\s*:",
    r"\bnie (?:kieruj|przekazuj|wysylaj)\w* .{0,25}\b(?:przegl\w*|weryfikac\w*|czlowiek\w*)",
]
_COMPILED = [re.compile(p) for p in _PATTERNS]


def find_injections(text: str) -> list[tuple[int, int]]:
    """Return (start, end) of each match, as offsets into ``text``."""
    folded = fold(text)
    hits: list[tuple[int, int]] = []
    for rx in _COMPILED:
        for m in rx.finditer(folded):
            hits.append((m.start(), m.end()))
    return sorted(set(hits))


def sentences_to_mask(text: str, hits: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Expand each hit to the whole sentence it sits in (offsets into ``text``).

    Only the injected sentence is blanked, so a genuine request in the same line
    or paragraph is still analysed.
    """
    ranges: set[tuple[int, int]] = set()
    for start, end in hits:
        s = start
        while s > 0:
            prev = text[s - 1]
            if prev == "\n":
                break
            if prev in " \t" and s >= 2 and text[s - 2] in ".!?":
                break
            s -= 1
        e = end
        while e < len(text) and text[e] not in ".!?\n":
            e += 1
        if e < len(text) and text[e] in ".!?":
            e += 1
        ranges.add((s, e))
    return sorted(ranges)
