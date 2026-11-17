"""Rule-based message classification (fictional-business PL/EN lexicon).

Scoring is a transparent sum of weighted phrase hits; each distinct phrase counts
once per segment, subject hits count double. Scores are heuristic rule hits,
NOT probabilities, and are only meaningful relative to each other.

Patterns run on folded text (lowercase, no Polish diacritics).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ai_ops_hub.analysis.schemas import Category, CategoryIndicator
from ai_ops_hub.analysis.text import Segment, is_negated
from ai_ops_hub.schemas import SpanSource

MIN_SCORE = 2  # below this the message is "other"
CLOSE_RATIO = 0.6  # a runner-up at >= 60% of the leader makes the category ambiguous
SUBJECT_MULTIPLIER = 2

# Tie-break order when scores are equal (risk-first).
PRIORITY = (Category.COMPLAINT, Category.ORDER, Category.DOCUMENT_REQUEST, Category.INQUIRY)

_DOC_NOUN = (
    r"(?:faktur\w*|rachunk\w*|umow\w*|certyfikat\w*|zaswiadczen\w*|oswiadczen\w*|protokol\w*|"
    r"dokument\w*|kopi\w*|odpis\w*|invoices?|contracts?|certificates?|statements?|receipts?|"
    r"documents?|agreements?|copy|copies)"
)

_RAW: dict[Category, list[tuple[str, int]]] = {
    Category.COMPLAINT: [
        (r"\breklamac\w*", 3),
        (r"\bskarg\w*", 3),
        (r"\bzazalen\w*", 3),
        (r"\bsprzeciw\w*", 3),
        (r"\bcomplain\w*", 3),
        (r"\bdisput(?:e|ed|es|ing)\b", 3),
        (r"\bniezadowol\w*|\bdissatisf\w*|\bunhappy\b", 2),
        (r"\buszkodz\w*|\bdamaged?\b", 2),
        (r"\bwadliw\w*|\bdefective\b|\bfaulty\b|\bbroken\b|\bzepsut\w*", 2),
        (r"\bniezgodn\w*|\bnot as (?:described|ordered)\b|\bwrong (?:item|product|size|goods)\b", 2),
        (r"\bnie (?:dotarl\w*|otrzymal\w*|zostal\w* dostarczon\w*)|\bnie dostarczono\b", 2),
        (r"\b(?:did not|didn't|have not|haven't) (?:arrive|receive)\w*|\bnever (?:received|arrived)\b|\bnot (?:been )?(?:delivered|received)\b", 2),
        (r"\bunacceptable\b|\bnieakceptowaln\w*", 2),
        (r"\bzwrot\w* (?:pieniedzy|kwoty|naleznosci|srodkow|towaru|platnosci)|\brefund\b|\bmoney back\b", 2),
        (r"\bkorekt\w*|\bcorrect(?:ion|ed)\b", 2),
        (r"\bopozni\w*|\bdelayed?\b|\blate delivery\b", 1),
        (r"\bbledn\w*|\bincorrect\b|\bmistake\b", 1),
    ],
    Category.ORDER: [
        (r"\bzamawia\w*", 4),
        (r"\b(?:chcial\w*|chcem\w*|chcielibysmy|chcialbym|chcialabym)\s+(?:\w+\s+)?zamowic\b", 4),
        (r"\bskladam\w*\s+zamowieni\w*", 4),
        (r"\bplace (?:an|a new|our|the) order\b", 4),
        (r"\b(?:i|we)(?:'d| would| wish to| want to)\s+(?:like to\s+)?order\b", 4),
        (r"\b(?:i|we) (?:wish|want) to order\b", 4),
        (r"\bpros(?:z\w*|im\w*) o (?:realizacj\w*|dostaw\w*|wysylk\w*|przygotowan\w*)", 2),
        (r"\bplease (?:ship|deliver|dispatch)\b", 2),
        (r"\bpurchase orders?\b|\bzamowieni\w* zakupu\b", 2),
        (r"\bzamowieni\w*", 1),
        (r"\borders?\b", 1),
    ],
    Category.INQUIRY: [
        (r"\bzapytani\w*|\bzapytuj\w*|\bzapytac\b|\benquir\w*|\binquir\w*", 3),
        (r"\bpros(?:z\w*|im\w*) o (?:informacj\w*|oferte|wycene|cennik)|\brequest for (?:quotation|quote|information|pricing)\b", 3),
        (r"\bplease (?:provide|send)(?: us| me)? (?:a |your |an )?(?:quote|quotation|price list|pricing|offer)\b", 3),
        (r"\boferta\b|\boferty\b|\boferte\b|\bwycen\w*|\bcennik\w*|\bquot(?:e|ation)s?\b|\bprice list\b|\bpricing\b", 2),
        (r"\bdostepnosc\w*|\bavailab\w*", 2),
        (r"\b(?:could|can|would) you (?:please )?(?:tell|let|inform|advise|clarify|provide|confirm)\b|\bwould it be possible\b", 2),
        # implicit business questions: "do you (guys) ship ...", "are you able to ...", "czy oferujecie ..."
        (r"\b(?:do|can|could|would|will) you(?: guys)? (?:have|offer|provide|sell|ship|deliver|supply|stock|carry|accept)\b|\bare you (?:able|in a position) to\b", 2),
        (r"\bczy\s+(?:panstwo\s+|pan\s+|pani\s+|wy\s+)?(?:oferuj\w*|dostarcz\w*|wysyla\w*|sprzedaj\w*|realizuj\w*|przyjmuj\w*|posiada\w*|macie|mozecie|moga|mozna|mozliwe)\b", 2),
        # expressions of interest: an implicit request for information
        (r"\b(?:we are|we're|i am|i'm) (?:interested in|looking for)\b|\binteresuje (?:nas|mnie)\b|\bszukamy\b|\bposzukujemy\b", 2),
        (r"\b(?:i|we)(?:'d| would) like to know\b|\bi am (?:interested|curious)\b|\bzainteresowan\w*", 2),
        (r"\bhow (?:much|long|many)\b|\bile (?:kosztuje|kosztuja|wynosi)\b|\bjaki (?:jest|bedzie) (?:koszt|termin|czas)\b|\bjaka jest (?:cena|dostepnosc)\b|\bjak dlugo\b", 2),
        (r"\binformacj\w* o\b|\binformation (?:about|on|regarding)\b", 1),
    ],
    Category.DOCUMENT_REQUEST: [
        (rf"\bpros(?:z\w*|im\w*) o (?:przeslanie|przekazanie|wystawienie|udostepnienie|dostarczenie|wyslanie|kopie|odpis|duplikat)\b", 3),
        (rf"\b(?:please |kindly )?(?:send|provide|issue|forward|resend|share|supply)(?: us| me)?(?: with)? (?:a |an |the |our |your |another )?(?:copy of (?:the |our |my )?)?(?:\w+ ){{0,2}}?{_DOC_NOUN}\b", 3),
        (rf"\brequest(?:ing)? (?:a |an |the )?(?:copy|duplicate|certificate|statement)\b", 3),
        (rf"\b(?:i|we) (?:need|require)(?: a| an| the)? (?:copy|duplicate|invoice|certificate|statement|receipt|contract|document)\b", 3),
        (r"\bproof of (?:payment|delivery|purchase)\b|\bpotwierdzeni\w* (?:platnosci|przelewu|odbioru)\b", 2),
        (r"\bduplikat\w*|\bduplicate (?:invoice|copy)\b", 3),
        (rf"\b{_DOC_NOUN}\b", 1),
    ],
}

_QUANTITY_HINT = re.compile(r"\b\d[\d .,]*\s*(?:szt|sztuk\w*|pcs|pieces|units|pallets?|palet\w*|cartons?|boxes|licenses?|licenc\w*)\b")

_COMPILED = {
    cat: [(re.compile(p), w) for p, w in items] for cat, items in _RAW.items()
}


@dataclass(frozen=True)
class Classification:
    category: Category
    scores: dict[Category, int]
    indicators: tuple[CategoryIndicator, ...]
    alternatives: tuple[Category, ...]


def classify(segments: list[Segment]) -> Classification:
    scores: dict[Category, int] = {c: 0 for c in _RAW}
    indicators: list[CategoryIndicator] = []

    for seg in segments:
        mult = SUBJECT_MULTIPLIER if seg.source is SpanSource.SUBJECT else 1
        for cat, items in _COMPILED.items():
            for rx, weight in items:
                hits = [m for m in rx.finditer(seg.folded) if m.end() > m.start()]
                if not hits:
                    continue
                live = [m for m in hits if not is_negated(seg.folded, m.start())]
                if live:
                    m = live[0]
                    scores[cat] += weight * mult
                    indicators.append(
                        CategoryIndicator(
                            category=cat, weight=weight * mult, span=seg.span(m.start(), m.end())
                        )
                    )
                else:  # every occurrence is negated ("this is not a complaint"): recorded, not counted
                    m = hits[0]
                    indicators.append(
                        CategoryIndicator(
                            category=cat, weight=0, span=seg.span(m.start(), m.end()), negated=True
                        )
                    )
        # structural hints (one each per segment)
        if seg.source is SpanSource.BODY:
            if "?" in seg.text:
                m_idx = seg.text.index("?")
                scores[Category.INQUIRY] += 1
                indicators.append(
                    CategoryIndicator(category=Category.INQUIRY, weight=1, span=seg.span(m_idx, m_idx + 1))
                )
            q = _QUANTITY_HINT.search(seg.folded)
            if q:
                scores[Category.ORDER] += 1
                indicators.append(
                    CategoryIndicator(category=Category.ORDER, weight=1, span=seg.span(q.start(), q.end()))
                )

    ranked = sorted(
        (c for c in PRIORITY if scores[c] > 0), key=lambda c: (-scores[c], PRIORITY.index(c))
    )
    if not ranked or scores[ranked[0]] < MIN_SCORE:
        return Classification(Category.OTHER, scores, tuple(indicators), ())

    top = ranked[0]
    threshold = max(MIN_SCORE, CLOSE_RATIO * scores[top])
    alt = {c for c in ranked[1:] if scores[c] >= threshold}
    # Risk-first: mis-routing a complaint is the costliest mistake, so any real complaint
    # evidence that did not win still makes the category ambiguous (-> human review).
    if top is not Category.COMPLAINT and scores[Category.COMPLAINT] >= MIN_SCORE:
        alt.add(Category.COMPLAINT)
    alternatives = tuple(c for c in PRIORITY if c in alt)
    return Classification(top, scores, tuple(indicators), alternatives)
