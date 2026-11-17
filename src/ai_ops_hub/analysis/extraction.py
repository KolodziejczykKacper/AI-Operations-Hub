"""Rule-based extraction of explicitly stated values.

Principles
----------
* Extract only what the text states. Never infer a missing year, currency,
  country code or identity; flag the gap instead.
* Every candidate carries the exact character span it came from; normalized
  values are derived from that span only.
* Patterns run on ``segment.folded`` (lowercase, no Polish diacritics, same
  length as the original), case-sensitive details are read from
  ``segment.original`` at the same offsets.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from ai_ops_hub.analysis.schemas import AnalysisWarning, FieldName, FieldStatus, WarningCode
from ai_ops_hub.analysis.text import Segment, fold, is_negated, make_warning


@dataclass(frozen=True)
class Candidate:
    name: FieldName
    start: int
    end: int
    normalized: str
    status: FieldStatus = FieldStatus.VALID
    warnings: tuple[tuple[WarningCode, str], ...] = ()


@dataclass(frozen=True)
class SegmentNote:
    """A segment-level warning that is not tied to an extracted field."""

    code: WarningCode
    message: str
    start: int
    end: int


# --------------------------------------------------------------------------- numbers

_NUM = r"(?:\d{1,3}(?:[   ]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)*)"
_SPACES = re.compile(r"[   ]")


def parse_number(raw: str) -> tuple[Decimal | None, bool]:
    """Parse a localized number. Returns (value, separator_ambiguous).

    ``None`` means unparseable. A single separator followed by exactly three
    digits (``1,500``) could be a thousands separator or a decimal comma; it is
    read as thousands and reported as ambiguous.
    """
    s = _SPACES.sub("", raw)
    has_dot, has_comma = "." in s, "," in s
    try:
        if has_dot and has_comma:
            dec = "." if s.rfind(".") > s.rfind(",") else ","
            thou = "," if dec == "." else "."
            int_part, frac = s.rsplit(dec, 1)
            if not re.fullmatch(rf"\d{{1,3}}(?:{re.escape(thou)}\d{{3}})*", int_part):
                return None, False
            return Decimal(int_part.replace(thou, "") + "." + frac), False
        if has_dot or has_comma:
            sep = "." if has_dot else ","
            parts = s.split(sep)
            if len(parts) > 2:
                if 1 <= len(parts[0]) <= 3 and all(len(p) == 3 for p in parts[1:]):
                    return Decimal("".join(parts)), False
                return None, False
            left, right = parts
            if len(right) == 3 and 1 <= len(left) <= 3 and left != "0":
                return Decimal(left + right), True
            return Decimal(left + "." + right), False
        return Decimal(s), False
    except InvalidOperation:
        return None, False


def _fmt(value: Decimal) -> str:
    if -value.as_tuple().exponent <= 2:
        return f"{value:.2f}"
    return format(value.normalize(), "f")


# --------------------------------------------------------------------------- amounts

_CUR_MAP: dict[str, str | None] = {
    "pln": "PLN", "zl": "PLN", "zloty": "PLN", "zlote": "PLN", "zlotych": "PLN",
    "eur": "EUR", "euro": "EUR", "€": "EUR",
    "usd": "USD", "us$": "USD",
    "gbp": "GBP", "£": "GBP",
    "chf": "CHF",
    "$": None, "dollar": None, "dollars": None,  # which dollar? not inferred
}
_CUR_WORDS = r"(?:pln|zlotych|zlote|zloty|zl|eur|euro|usd|gbp|chf|dollars?|dolar\w*)"
_UNIT_WORDS_FOR_LOOKAHEAD = (
    r"(?:szt|sztuk\w*|pcs|pc|pieces|units?|kg|pallets?|palet\w*|cartons?|karton\w*|boxes|box|"
    r"licenses?|licences?|licenc\w*|hours?|godzin\w*|egz\w*|liters?|litres?|litr\w*)"
)

_AMOUNT_NUM_CUR = re.compile(
    rf"(?<![\w.,-])(?P<num>{_NUM})\s*(?P<cur>€|£|\$|{_CUR_WORDS}(?![a-z0-9]))"
)
_AMOUNT_CUR_NUM = re.compile(
    rf"(?<![a-z0-9])(?P<cur>us\$|€|£|\$|pln|eur|usd|gbp|chf)\s*(?P<num>{_NUM})(?![\d])"
)
_AMOUNT_CUE = re.compile(
    rf"(?<![a-z])(?:kwot\w*|wartosc\w*|cena|cene|cenie|ceny|razem|lacznie|total|amount|price|sum|cost|"
    rf"koszt\w*|do zaplaty|do zwrotu|naleznosc\w*|saldo|balance)\s*(?:wynosi|wynosza|jest|is|are|of|na|:|-|=)?\s*"
    rf"(?P<num>{_NUM})(?![\d])(?!\s*{_UNIT_WORDS_FOR_LOOKAHEAD}\b)(?!\s*%)(?!\s*(?:€|£|\$|{_CUR_WORDS}\b))"
)


def _amount_candidate(seg: Segment, start: int, end: int, num: str, cur_token: str | None, cue_only: bool) -> Candidate:
    value, sep_amb = parse_number(num)
    warns: list[tuple[WarningCode, str]] = []
    status = FieldStatus.VALID
    if value is None:
        return Candidate(
            FieldName.AMOUNT, start, end, seg.original[start:end].strip(), FieldStatus.INVALID,
            ((WarningCode.NUMBER_UNPARSEABLE, "amount text is not a valid number"),),
        )
    currency: str | None = None
    if cur_token is not None:
        currency = _CUR_MAP.get(cur_token) if cur_token in _CUR_MAP else (
            "PLN" if cur_token.startswith("zlot") else None
        )
        if cur_token.startswith("dolar"):
            currency = None
        if currency is None:
            status = FieldStatus.AMBIGUOUS
            warns.append((WarningCode.CURRENCY_AMBIGUOUS, "currency symbol/word does not identify a single currency; not inferred"))
    else:
        status = FieldStatus.AMBIGUOUS
        warns.append((WarningCode.CURRENCY_NOT_STATED, "no currency stated next to the amount; not inferred"))
    if sep_amb:
        status = FieldStatus.AMBIGUOUS
        warns.append((WarningCode.NUMBER_FORMAT_AMBIGUOUS, "single separator before three digits: read as thousands separator, could be a decimal"))
    normalized = _fmt(value) + (f" {currency}" if currency else "")
    return Candidate(FieldName.AMOUNT, start, end, normalized, status, tuple(warns))


def extract_amounts(seg: Segment) -> list[Candidate]:
    out: list[Candidate] = []
    taken: list[tuple[int, int]] = []

    def free(s: int, e: int) -> bool:
        return all(e <= ts or s >= te for ts, te in taken)

    for m in _AMOUNT_NUM_CUR.finditer(seg.folded):
        out.append(_amount_candidate(seg, m.start(), m.end(), m.group("num"), m.group("cur"), False))
        taken.append((m.start(), m.end()))
    for m in _AMOUNT_CUR_NUM.finditer(seg.folded):
        if free(m.start(), m.end()):
            out.append(_amount_candidate(seg, m.start(), m.end(), m.group("num"), m.group("cur"), False))
            taken.append((m.start(), m.end()))
    for m in _AMOUNT_CUE.finditer(seg.folded):
        s, e = m.start("num"), m.end("num")
        if free(s, e):
            out.append(_amount_candidate(seg, s, e, m.group("num"), None, True))
            taken.append((s, e))
    return out


# --------------------------------------------------------------------------- dates

_MONTHS = {
    "stycznia": 1, "styczen": 1, "lutego": 2, "luty": 2, "marca": 3, "marzec": 3,
    "kwietnia": 4, "kwiecien": 4, "maja": 5, "maj": 5, "czerwca": 6, "czerwiec": 6,
    "lipca": 7, "lipiec": 7, "sierpnia": 8, "sierpien": 8, "wrzesnia": 9, "wrzesien": 9,
    "pazdziernika": 10, "pazdziernik": 10, "listopada": 11, "listopad": 11,
    "grudnia": 12, "grudzien": 12,
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7,
    "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
    "sept": 9, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_MONTH_RX = "(?:" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + ")"

_ISO = re.compile(r"(?<![\d-])(\d{4})-(\d{2})-(\d{2})(?![\d-])")
_DOTTED = re.compile(r"(?<![\d.])(\d{1,2})\.(\d{1,2})\.(\d{4}|\d{2})(?!\d|\.\d)")
_SLASHED = re.compile(r"(?<![\d/-])(\d{1,2})([/-])(\d{1,2})\2(\d{4}|\d{2})(?![\d/-])")
_DAY_MONTH = re.compile(
    rf"(?<![\d.,/-])(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({_MONTH_RX})\b\.?(?:,?\s+(\d{{4}})(?!\d))?"
)
_MONTH_DAY = re.compile(
    rf"\b({_MONTH_RX})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?(?!\d)(?:,?\s+(\d{{4}})(?!\d))?"
)
_MONTH_LEVEL = re.compile(
    rf"(?:do konca|koniec|by the end of|by end of|end of)\s+({_MONTH_RX})\b(?:\s+(\d{{4}})(?!\d))?"
)

# Numeric dates WITHOUT a year ("15/12", "10.03."): only read when a deadline/request cue precedes them.
_DOTTED_NOYEAR = re.compile(r"(?<![\d.,/-])(\d{1,2})\.(\d{1,2})\.(?!\d)")
_SLASHED_NOYEAR = re.compile(r"(?<![\d/.,-])(\d{1,2})/(\d{1,2})(?![\d/])(?!\s*(?:%|kg|szt|pcs))")

# Ranges of dates ("between 4 and 6 March", "od 3 do 5 lutego", "4-6 March"): a window, not one date.
_RANGE_WORD = re.compile(
    rf"(?<![\w])(?:between|from|od|miedzy)\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:\s+(?:of\s+)?{_MONTH_RX}\b)?(?:\s+\d{{4}})?"
    rf"\s+(?:and|to|until|do|i|a|-)\s+(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?{_MONTH_RX}\b\.?(?:,?\s+\d{{4}}(?!\d))?"
)
_RANGE_DASH = re.compile(
    rf"(?<![\d.,/-])(\d{{1,2}})(?:st|nd|rd|th)?\s*-\s*(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?{_MONTH_RX}\b\.?(?:,?\s+\d{{4}}(?!\d))?"
)

# A date after an absence / availability phrase is a return or closure date, not a business deadline.
_ABSENCE = re.compile(
    r"\b(?:out of (?:the )?office|on (?:leave|vacation|holiday|holidays)|away|closed|unavailable|ooo|"
    r"will be back|back on|back in the office|nieobecn\w*|urlop\w*|poza biurem|zamkniet\w*|"
    r"niedostepn\w*|wracam|wracamy|wroce|wrocimy)\b"
)

_DEADLINE_CUE = re.compile(
    r"\b(?:do|najpozniej|nie pozniej niz|termin\w*|przed|until|by|before|no later than|not later than|"
    r"deadline|due|within|prior to|at the latest)\b"
)
_REQUEST_CUE = re.compile(
    r"\b(?:na dzien|dostaw\w*|wysylk\w*|realizacj\w*|odbior\w*|delivery|deliver\w*|ship\w*|"
    r"dispatch\w*|pickup|collect\w*|requested date|preferred|prefer\w*|expected|oczekiw\w*|"
    r"wymagan\w*)\b"
)
# Relative expressions that are deadlines by themselves ...
_RELATIVE_SELF = re.compile(
    r"\b(?:do konca (?:tygodnia|miesiaca|roku|dnia)|jak najszybciej|niezwlocznie|"
    r"w ciagu \d+ (?:dni|dnia|tygodni|tygodnie|godzin)|w terminie \d+ dni|"
    r"asap|as soon as possible|by (?:the )?end of (?:the )?(?:week|month|year|day)|"
    r"within \d+ (?:days?|weeks?|hours?))\b"
)
# ... and ones that only count when a deadline/delivery cue precedes them.
_RELATIVE_NEEDS_CUE = re.compile(
    r"\b(?:jutro|dzisiaj|dzis|pojutrze|w tym tygodniu|w przyszlym tygodniu|w przyszlym miesiacu|"
    r"tomorrow|today|next (?:week|month|year|monday|tuesday|wednesday|thursday|friday)|"
    r"this (?:week|month))\b"
)
_ABBREVIATIONS = {"szt", "ok", "np", "tj", "tzn", "nr", "ul", "godz", "min", "max", "ca", "pcs",
                  "no", "approx", "tel", "kom", "ref", "egz"}
_BOUNDARY = re.compile(r"[.!?;]\s|\n")


def _sentence_window(folded: str, start: int, width: int = 45) -> str:
    """Text before ``start`` back to the start of its sentence (abbreviation dots don't count)."""
    window = folded[max(0, start - width):start]
    cut = 0
    for m in _BOUNDARY.finditer(window):
        if m.group(0)[0] == ".":
            word = re.search(r"([a-z]+)$", window[:m.start()])
            if word and word.group(1) in _ABBREVIATIONS:
                continue
        cut = m.end()
    return window[cut:]


def _date_role(folded: str, start: int) -> FieldName | None:
    window = _sentence_window(folded, start)
    d = [m.end() for m in _DEADLINE_CUE.finditer(window)]
    r = [m.end() for m in _REQUEST_CUE.finditer(window)]
    if not d and not r:
        return None
    if d and (not r or max(d) >= max(r)):
        return FieldName.DEADLINE
    return FieldName.REQUESTED_DATE


def _valid_ymd(y: int, m: int, d: int) -> bool:
    try:
        date(y, m, d)
        return True
    except ValueError:
        return False


def _date_candidate(
    name: FieldName, start: int, end: int, raw: str, y: int | None, m: int | None, d: int | None
) -> Candidate:
    """Build a candidate from already-split components. ``y``/``d`` may be None."""
    warns: list[tuple[WarningCode, str]] = []
    if m is None or not 1 <= m <= 12:
        return Candidate(name, start, end, raw, FieldStatus.INVALID, ((WarningCode.DATE_INVALID, "month out of range"),))
    if d is None:
        norm = f"{y:04d}-{m:02d}" if y is not None else f"--{m:02d}"
        warns.append((WarningCode.DAY_NOT_STATED, "only a month is stated; the day is not inferred"))
        if y is None:
            warns.append((WarningCode.YEAR_NOT_STATED, "year not stated; not inferred"))
        return Candidate(name, start, end, norm, FieldStatus.AMBIGUOUS, tuple(warns))
    if y is None:
        if not 1 <= d <= calendar.monthrange(2000, m)[1]:
            return Candidate(name, start, end, raw, FieldStatus.INVALID, ((WarningCode.DATE_INVALID, "day does not exist in that month"),))
        warns.append((WarningCode.YEAR_NOT_STATED, "year not stated; not inferred"))
        return Candidate(name, start, end, f"--{m:02d}-{d:02d}", FieldStatus.AMBIGUOUS, tuple(warns))
    if not _valid_ymd(y, m, d):
        return Candidate(name, start, end, raw, FieldStatus.INVALID, ((WarningCode.DATE_INVALID, "not a valid calendar date"),))
    return Candidate(name, start, end, f"{y:04d}-{m:02d}-{d:02d}")


def extract_dates(seg: Segment) -> tuple[list[Candidate], list[SegmentNote]]:
    f = seg.folded
    raw_matches: list[tuple[int, int, str, tuple[int | None, int | None, int | None] | Candidate]] = []

    for m in _ISO.finditer(f):
        raw_matches.append((m.start(), m.end(), "iso", (int(m.group(1)), int(m.group(2)), int(m.group(3)))))
    for m in _DOTTED.finditer(f):
        d_, mo, yr = int(m.group(1)), int(m.group(2)), m.group(3)
        raw_matches.append((m.start(), m.end(), "dotted", (d_, mo, yr)))
    for m in _SLASHED.finditer(f):
        raw_matches.append((m.start(), m.end(), "slashed", (int(m.group(1)), int(m.group(3)), m.group(4))))
    for m in _DAY_MONTH.finditer(f):
        yr = int(m.group(3)) if m.group(3) else None
        raw_matches.append((m.start(), m.end(), "named", (yr, _MONTHS[m.group(2)], int(m.group(1)))))
    for m in _MONTH_DAY.finditer(f):
        yr = int(m.group(3)) if m.group(3) else None
        raw_matches.append((m.start(), m.end(), "named", (yr, _MONTHS[m.group(1)], int(m.group(2)))))
    for m in _DOTTED_NOYEAR.finditer(f):
        raw_matches.append((m.start(), m.end(), "dotted_ny", (int(m.group(1)), int(m.group(2)))))
    for m in _SLASHED_NOYEAR.finditer(f):
        raw_matches.append((m.start(), m.end(), "slashed_ny", (int(m.group(1)), int(m.group(2)))))
    month_level: list[tuple[int, int, tuple[int | None, int | None, int | None]]] = []
    for m in _MONTH_LEVEL.finditer(f):
        yr = int(m.group(2)) if m.group(2) else None
        month_level.append((m.start(), m.end(), (yr, _MONTHS[m.group(1)], None)))

    notes: list[SegmentNote] = []

    # Ranges first: a date inside a range must never be reported as a single date.
    range_spans: list[tuple[int, int]] = []
    for rx in (_RANGE_WORD, _RANGE_DASH):
        for m in rx.finditer(f):
            if any(not (m.end() <= s or m.start() >= e) for s, e in range_spans):
                continue
            range_spans.append((m.start(), m.end()))
            if _ABSENCE.search(_sentence_window(f, m.start(), 90)):
                notes.append(SegmentNote(WarningCode.AVAILABILITY_DATE,
                                         "dates describe availability or absence, not a business deadline",
                                         m.start(), m.end()))
            elif _date_role(f, m.start()) is not None:
                notes.append(SegmentNote(WarningCode.DATE_RANGE,
                                         "a range of dates was given, not a single deadline or delivery date; not extracted",
                                         m.start(), m.end()))
            else:
                notes.append(SegmentNote(WarningCode.DATE_WITHOUT_ROLE,
                                         "date range without a deadline/requested-date cue; not extracted as a field",
                                         m.start(), m.end()))
    raw_matches = [r for r in raw_matches if all(r[1] <= s or r[0] >= e for s, e in range_spans)]

    # resolve overlaps between date patterns: keep the longest
    raw_matches.sort(key=lambda t: (t[0], -(t[1] - t[0])))
    kept: list[tuple[int, int, str, object]] = []
    for item in raw_matches:
        if all(item[1] <= k[0] or item[0] >= k[1] for k in kept):
            kept.append(item)
    for s, e, comps in month_level:
        if all(e <= k[0] or s >= k[1] for k in kept):
            kept.append((s, e, "month", comps))
    kept.sort(key=lambda t: t[0])

    cands: list[Candidate] = []
    for start, end, kind, comps in kept:
        raw = seg.original[start:end].strip()
        if _ABSENCE.search(_sentence_window(f, start, 90)):
            notes.append(SegmentNote(
                WarningCode.AVAILABILITY_DATE,
                "date follows an absence/availability phrase (return or closure date); not a deadline",
                start, end,
            ))
            continue
        if kind == "month" and f[start:end].startswith(("do konca", "by ")):
            name = FieldName.DEADLINE  # the matched phrase itself ("until the end of ...") is the cue
        else:
            name = _date_role(f, start)
        if name is None and kind.endswith("_ny"):
            continue  # "1/2", "3.5." without a cue are far more likely fractions or decimals
        if name is None:
            notes.append(SegmentNote(
                WarningCode.DATE_WITHOUT_ROLE,
                "date found without a deadline/requested-date cue; not extracted as a field",
                start, end,
            ))
            continue
        if kind == "iso":
            y, mo, d = comps  # type: ignore[misc]
            cands.append(_date_candidate(name, start, end, raw, y, mo, d))
        elif kind == "dotted":
            d, mo, yr = comps  # type: ignore[misc]
            if len(yr) == 2:
                cands.append(Candidate(name, start, end, raw, FieldStatus.AMBIGUOUS,
                                       ((WarningCode.TWO_DIGIT_YEAR, "two-digit year; century not inferred"),)))
            else:
                cands.append(_date_candidate(name, start, end, raw, int(yr), mo, d))
        elif kind == "slashed":
            a, b, yr = comps  # type: ignore[misc]
            if len(yr) == 2:
                cands.append(Candidate(name, start, end, raw, FieldStatus.AMBIGUOUS,
                                       ((WarningCode.TWO_DIGIT_YEAR, "two-digit year; century not inferred"),)))
                continue
            y = int(yr)
            if a > 12 and b <= 12:
                cands.append(_date_candidate(name, start, end, raw, y, b, a))
            elif b > 12 and a <= 12:
                cands.append(_date_candidate(name, start, end, raw, y, a, b))
            elif a == b and a <= 12:
                cands.append(_date_candidate(name, start, end, raw, y, a, b))
            elif a <= 12 and b <= 12:
                dm = f"{y:04d}-{b:02d}-{a:02d}"
                md = f"{y:04d}-{a:02d}-{b:02d}"
                cands.append(Candidate(name, start, end, raw, FieldStatus.AMBIGUOUS, (
                    (WarningCode.DATE_FORMAT_AMBIGUOUS, f"day-month or month-day order not determinable: {dm} or {md}"),
                )))
            else:
                cands.append(Candidate(name, start, end, raw, FieldStatus.INVALID,
                                       ((WarningCode.DATE_INVALID, "no valid day/month reading"),)))
        elif kind == "dotted_ny":
            d, mo = comps  # type: ignore[misc]
            cands.append(_date_candidate(name, start, end, raw, None, mo, d))  # day.month, year not inferred
        elif kind == "slashed_ny":
            a, b = comps  # type: ignore[misc]
            if a > 12 and b <= 12:
                cands.append(_date_candidate(name, start, end, raw, None, b, a))
            elif b > 12 and a <= 12:
                cands.append(_date_candidate(name, start, end, raw, None, a, b))
            elif a == b and a <= 12:
                cands.append(_date_candidate(name, start, end, raw, None, a, b))
            elif a <= 12 and b <= 12:
                cands.append(Candidate(name, start, end, raw, FieldStatus.AMBIGUOUS, (
                    (WarningCode.DATE_FORMAT_AMBIGUOUS, "day-month or month-day order not determinable"),
                    (WarningCode.YEAR_NOT_STATED, "year not stated; not inferred"),
                )))
            else:
                cands.append(Candidate(name, start, end, raw, FieldStatus.INVALID,
                                       ((WarningCode.DATE_INVALID, "no valid day/month reading"),)))
        elif kind == "month":
            y, mo, _ = comps  # type: ignore[misc]
            cands.append(_date_candidate(name, start, end, raw, y, mo, None))
        else:  # named month with day
            y, mo, d = comps  # type: ignore[misc]
            cands.append(_date_candidate(name, start, end, raw, y, mo, d))

    for m in _RELATIVE_SELF.finditer(f):
        notes.append(SegmentNote(
            WarningCode.RELATIVE_DATE_NOT_RESOLVED,
            "relative time expression cannot be resolved without a reference date",
            m.start(), m.end(),
        ))
    for m in _RELATIVE_NEEDS_CUE.finditer(f):
        if _date_role(f, m.start()) is not None:
            notes.append(SegmentNote(
                WarningCode.RELATIVE_DATE_NOT_RESOLVED,
                "relative time expression cannot be resolved without a reference date",
                m.start(), m.end(),
            ))
    return cands, notes


# --------------------------------------------------------------------------- quantity

_UNITS = {
    "szt": "pcs", "sztuk": "pcs", "sztuki": "pcs", "sztuka": "pcs", "pcs": "pcs", "pc": "pcs",
    "pieces": "pcs", "piece": "pcs",
    "unit": "units", "units": "units",
    "egz": "copies", "egzemplarzy": "copies", "egzemplarze": "copies",
    "kg": "kg",
    "pallet": "pallets", "pallets": "pallets", "palet": "pallets", "palety": "pallets", "paleta": "pallets",
    "karton": "cartons", "kartonow": "cartons", "kartony": "cartons", "carton": "cartons", "cartons": "cartons",
    "box": "boxes", "boxes": "boxes",
    "license": "licenses", "licenses": "licenses", "licence": "licenses", "licences": "licenses",
    "licencje": "licenses", "licencji": "licenses", "licencja": "licenses",
    "hour": "hours", "hours": "hours", "godzin": "hours", "godziny": "hours", "godzina": "hours",
    "liter": "liters", "liters": "liters", "litre": "liters", "litres": "liters",
    "litrow": "liters", "litry": "liters",
}
_UNIT_RX = "(?:" + "|".join(sorted(_UNITS, key=len, reverse=True)) + ")"
_QTY_UNIT = re.compile(
    rf"(?<![\w.,/-])(?P<num>\d{{1,3}}(?:[  .,]\d{{3}})+|\d+(?:[.,]\d+)?)(?:\s*[-–]\s*(?P<num2>\d+))?\s*(?P<unit>{_UNIT_RX})(?![a-z0-9])(?P<dot>\.)?"
)
_QTY_TIMES = re.compile(r"(?<![\w.,/-])(?P<num>\d+)\s*[x×]\s+(?=[^\W\d_])")
_QTY_LABEL = re.compile(
    rf"(?:ilosc|liczba|quantity|qty)\s*[:\-]?\s*(?P<num>\d+(?:[  .,]\d{{3}})*)(?!\d)(?!\s*{_UNIT_RX}\b)"
)
_APPROX_BEFORE = re.compile(r"(?:\bok\.?|\bokolo|\bapprox\.?|\bapproximately|\babout|\bcirca|\bca\.?|\bminimum|\bmin\.?|\bat least|\bco najmniej|\bup to|\bmax\.?)\s*$")


# A time unit counts as a quantity only when it is not a duration of a delay or a time limit.
_DURATION_AFTER = re.compile(
    r"\s*(?:late|early|ago|overdue|later|earlier|delay\w*|opoznien\w*|spozni\w*|temu|wczesniej|pozniej)\b"
)
_DURATION_BEFORE = re.compile(
    r"(?:\bwithin|\bw ciagu|\blate by|\bdelayed by|\bspozni\w*(?: sie)? o|\bopozni\w*(?: sie)? o|\bopoznienie(?: o)?)\s*$"
)


def _is_duration(folded: str, start: int, end: int) -> bool:
    return bool(_DURATION_AFTER.match(folded, end)) or bool(_DURATION_BEFORE.search(folded[max(0, start - 30):start]))


def extract_quantities(seg: Segment) -> list[tuple[Candidate, re.Match | None]]:
    """Return quantity candidates, each with its regex match (used to find a following product)."""
    f = seg.folded
    out: list[tuple[Candidate, re.Match | None]] = []
    taken: list[tuple[int, int]] = []

    def approx(start: int) -> bool:
        return bool(_APPROX_BEFORE.search(f[max(0, start - 14):start]))

    for m in _QTY_UNIT.finditer(f):
        value, sep_amb = parse_number(m.group("num"))
        if value is None:
            continue
        unit = _UNITS[m.group("unit")]
        if unit == "hours" and _is_duration(f, m.start(), m.end()):
            continue  # "3 hours late" / "within 24 hours" is a duration, not a quantity of goods
        warns: list[tuple[WarningCode, str]] = []
        status = FieldStatus.VALID
        num_txt = _fmt(value) if value % 1 else str(int(value))
        end = m.end() - (1 if m.group("dot") else 0)  # a trailing '.' is punctuation / abbreviation dot
        if m.group("dot") and m.group("unit") in ("szt", "pcs", "pc", "egz", "godz"):
            end = m.end()  # keep the abbreviation dot inside the span
        if m.group("num2"):
            num_txt = f"{num_txt}-{m.group('num2')}"
            status = FieldStatus.AMBIGUOUS
            warns.append((WarningCode.QUANTITY_RANGE, "a range was given, not a single quantity"))
        if sep_amb:
            status = FieldStatus.AMBIGUOUS
            warns.append((WarningCode.NUMBER_FORMAT_AMBIGUOUS, "single separator before three digits: read as thousands, could be a decimal"))
        if approx(m.start()):
            status = FieldStatus.AMBIGUOUS
            warns.append((WarningCode.APPROXIMATE_QUANTITY, "quantity is approximate or only a bound"))
        out.append((Candidate(FieldName.QUANTITY, m.start(), end, f"{num_txt} {unit}", status, tuple(warns)), m))
        taken.append((m.start(), m.end()))

    def free(s: int, e: int) -> bool:
        return all(e <= ts or s >= te for ts, te in taken)

    for m in _QTY_TIMES.finditer(f):
        if free(m.start(), m.end()):
            warns = []
            status = FieldStatus.VALID
            if approx(m.start()):
                status = FieldStatus.AMBIGUOUS
                warns.append((WarningCode.APPROXIMATE_QUANTITY, "quantity is approximate or only a bound"))
            out.append((Candidate(FieldName.QUANTITY, m.start(), m.end("num"), m.group("num"), status, tuple(warns)), m))
            taken.append((m.start(), m.end()))
    for m in _QTY_LABEL.finditer(f):
        s, e = m.start("num"), m.end("num")
        if free(s, e):
            value, sep_amb = parse_number(m.group("num"))
            if value is None:
                continue
            status = FieldStatus.AMBIGUOUS if sep_amb else FieldStatus.VALID
            warns = ((WarningCode.NUMBER_FORMAT_AMBIGUOUS, "single separator before three digits"),) if sep_amb else ()
            out.append((Candidate(FieldName.QUANTITY, s, e, str(int(value)) if value % 1 == 0 else _fmt(value), status, warns), None))
            taken.append((s, e))
    return out


# --------------------------------------------------------------------------- product

_PRODUCT_STOP = (
    r"(?:do|na|for|by|until|before|w terminie|prosimy|prosze|please|oraz|and|at|delivered|"
    r"dostarczon\w*|po cenie|w cenie|za|zgodnie|according|from|od|w ramach|ktore|ktora|ktory|"
    r"which|that|jak|as|with|dla|so|thanks|thank|dziekuj\w*|regards|pozdrawiam\w*|in|w|to|"
    r"asap|as soon as possible|immediately|today|tomorrow|next|natychmiast\w*)"
)
_REJECT_PREFIXES = ("wadliw", "uszkodz", "defect", "damag", "broken", "zepsut", "niezgodn", "faulty")
_PHRASE = rf"[^\W\d_][^\n,;:.()!?]{{1,70}}?(?=\s+{_PRODUCT_STOP}\b|[\n,;:.()!?]|\s*$)"
_REJECT_FIRST = {
    "do", "na", "w", "z", "za", "po", "od", "i", "oraz", "and", "or", "this", "that", "them", "these",
    "those", "tego", "tych", "ich", "to", "it", "one", "ones", "you", "us", "we", "i", "my", "our", "your",
    "nas", "was", "nam", "wam",
    # approximation words that precede a number must never become a product name
    "approx", "approximately", "about", "around", "circa", "ok", "okolo", "ca", "min", "max",
    "minimum", "maximum", "least",
    # temporal words ("3 hours late") are never a product
    "late", "early", "ago", "later", "earlier", "overdue", "delayed",
    "monthly", "weekly", "daily", "yearly", "annually", "quarterly",
    "miesiecznie", "tygodniowo", "dziennie", "rocznie", "kwartalnie", "opoznione", "spoznione",
}
_ARTICLES = ("the ", "a ", "an ")
_AFTER_QTY = re.compile(rf"\s*(?:of\s+)?(?P<prod>{_PHRASE})")
_LABELLED = re.compile(
    r"(?<![a-z])(?:produkt|product|towar|artykul|item|model|usluga|service|sku)\s*[:\-]\s*(?P<prod>[^\W_](?:[^\n,;().]|\.(?=\S))*)"
)
_ORDER_VERB = re.compile(
    r"(?:zamawiam|zamawiamy|zamawiaja|chcemy zamowic|chcialbym zamowic|chcialabym zamowic|"
    r"chcielibysmy zamowic|prosimy o dostawe|prosze o dostawe|prosimy o przygotowanie|"
    r"zamowienie na|order of|we would like to order|i would like to order|we'd like to order|"
    r"i'd like to order|please ship|please send us|please deliver)"
    r"\s+(?:(?P<num>\d+)\s+(?!" + _MONTH_RX + r"\b)(?!" + _UNIT_RX + r"\b))?(?P<prod>" + _PHRASE + ")"
)
_MONTH_WORD = re.compile(rf"^{_MONTH_RX}\b")


def _clean_product(seg: Segment, start: int, end: int) -> tuple[int, int] | None:
    """Trim articles/whitespace; reject pronouns, months and over-long phrases."""
    text = seg.folded[start:end]
    stripped = text.lstrip()
    start += len(text) - len(stripped)
    for art in _ARTICLES:
        if seg.folded[start:].startswith(art):
            start += len(art)
            break
    while end > start and seg.original[end - 1] in " \t.-":
        end -= 1
    phrase = seg.folded[start:end]
    if not phrase:
        return None
    words = phrase.split()
    if len(phrase) < 2 or len(words) > 7:
        return None
    if words[0] in _REJECT_FIRST or _MONTH_WORD.match(phrase) or words[0].startswith(_REJECT_PREFIXES):
        return None
    return start, end


def extract_products(seg: Segment, quantities: list[tuple[Candidate, re.Match | None]]) -> tuple[list[Candidate], list[Candidate]]:
    """Return (product candidates, extra unit-less quantity candidates)."""
    f = seg.folded
    products: list[Candidate] = []
    extra_qty: list[Candidate] = []

    def add(start: int, end: int) -> None:
        cleaned = _clean_product(seg, start, end)
        if cleaned:
            s, e = cleaned
            norm = " ".join(seg.original[s:e].split())
            products.append(Candidate(FieldName.PRODUCT, s, e, norm))

    for cand, m in quantities:
        if m is None:
            continue
        # a '.' right after the unit may end the sentence; then require a lowercase word start
        dot_after = m.groupdict().get("dot") is not None if "dot" in m.groupdict() else False
        pm = _AFTER_QTY.match(f, m.end())
        if not pm:
            continue
        s = pm.start("prod")
        if dot_after and s < len(seg.original) and seg.original[s].isupper():
            continue
        add(s, pm.end("prod"))

    for m in _LABELLED.finditer(f):
        s, e = m.start("prod"), m.end("prod")
        add(s, e)

    for m in _ORDER_VERB.finditer(f):
        if is_negated(f, m.start()):
            continue  # "nie zamawiamy ...": no order, so nothing to extract from it
        if m.group("num"):
            extra_qty.append(Candidate(FieldName.QUANTITY, m.start("num"), m.end("num"), m.group("num")))
        add(m.start("prod"), m.end("prod"))
    return products, extra_qty


# --------------------------------------------------------------------------- references

_REF_TOKEN = re.compile(
    r"(?<![\w/@.-])(?:[A-Z]{2,6}-\d{2,}(?:-\d+)*|[A-Z]{2,6}/\d{2,4}/\d{2,}|[A-Z]{2,6}\d{4,}|\d{2,4}/[A-Z]{2,6}/\d+)(?![\w/@-])"
)
_REF_LABEL = re.compile(
    r"(?<![a-z])(?P<label>zamowienie|zamowienia|zam|faktura|faktury|fakture|fakturze|invoice|umowa|umowy|umowie|contract|"
    r"case|ticket|sprawa|sprawy|zgloszenie|zgloszenia|oferta|oferty|reference|referencja|ref|order|"
    r"shipment|przesylka|przesylki|paczka|paczki|package|parcel|reklamacja|reklamacji|complaint|dispute|awb|"
    r"numer|nr|no|number)\.?\s*(?P<marker>nr\.?|no\.?|number|numer|#|:)?\s*(?P<id>[a-z0-9][a-z0-9\-/_.]{2,30})"
)
_MARKER_LABELS = {"nr", "no", "numer", "number"}
_DATE_LIKE = re.compile(r"^\d{1,2}[./-]\d{1,2}[./-]\d{2,4}$|^\d{4}-\d{2}-\d{2}$")

# A code-like token ("HP-300", "TS-120") is a reference only with some justification in the text:
# either a reference label next to it ("order no. KL-4455") or a well-known document prefix
# (FV = invoice, PO = purchase order, ...). Otherwise it is more likely a model/part code.
_KNOWN_REF_PREFIXES = {
    "FV", "FA", "FK", "FS", "PZ", "WZ", "ZAM", "ZK", "ZO", "UM", "CTR", "MSA", "SLA", "NDA",
    "INV", "PO", "SO", "RMA", "REF", "ORD", "SH", "TRK", "AWB", "TKT", "REK", "OF",
}
_PREFIX_LETTERS = re.compile(r"[A-Z]{2,6}")


def extract_references(seg: Segment) -> tuple[list[Candidate], list[SegmentNote]]:
    labelled: list[Candidate] = []
    for m in _REF_LABEL.finditer(seg.folded):
        s, e = m.start("id"), m.end("id")
        token = seg.folded[s:e].rstrip(".-/_")
        e = s + len(token)
        if not token or not any(c.isdigit() for c in token) or _DATE_LIKE.match(token):
            continue
        marker = m.group("marker") or (m.group("label") in _MARKER_LABELS)
        has_letters = any(c.isalpha() for c in token)
        if not has_letters and not marker:
            continue  # bare digits need an explicit "nr"/"no."/"#" marker
        labelled.append(Candidate(FieldName.REFERENCE, s, e, seg.original[s:e]))

    def overlaps(a: tuple[int, int], b: tuple[int, int]) -> bool:
        return not (a[1] <= b[0] or a[0] >= b[1])

    out: list[Candidate] = []
    notes: list[SegmentNote] = []
    for m in _REF_TOKEN.finditer(seg.original):
        span = (m.start(), m.end())
        prefix = _PREFIX_LETTERS.search(m.group(0))
        known = prefix is not None and prefix.group(0) in _KNOWN_REF_PREFIXES
        if known or any(overlaps(span, (c.start, c.end)) for c in labelled):
            out.append(Candidate(FieldName.REFERENCE, m.start(), m.end(), m.group(0)))
        else:
            notes.append(SegmentNote(
                WarningCode.UNLABELLED_CODE,
                "code-like token without a reference label or a known document prefix; "
                "not treated as a reference number (may be a model or part code)",
                m.start(), m.end(),
            ))
    accepted = [(c.start, c.end) for c in out]
    out += [c for c in labelled if not any(overlaps((c.start, c.end), a) for a in accepted)]
    return out, notes


# --------------------------------------------------------------------------- names / organizations

_NAME_TOKEN = r"[A-ZĄĆĘŁŃÓŚŹŻ][a-ząćęłńóśźż'’\-]+"
# an optional leading initial ("J. Smith") is accepted
_NAME_RE = re.compile(rf"(?:[A-ZĄĆĘŁŃÓŚŹŻ]\.[ \t]+)?{_NAME_TOKEN}(?:[ \t]+{_NAME_TOKEN}){{0,2}}")
_CLOSING = re.compile(
    r"(?:z powazaniem|z wyrazami szacunku|serdecznie pozdrawiam\w*|pozdrawiam\w*|z gory dziekuj\w*|"
    r"dziekuj\w*|best regards|kind regards|warm regards|regards|sincerely|yours sincerely|"
    r"yours faithfully|many thanks|thank you|thanks|cheers|best wishes|rgds|brgds|kr|bw|best)\s*[,.!]?"
)
_NAME_LABEL = re.compile(
    r"(?<![a-z])(?:osoba kontaktowa|kontakt|contact person|contact|nazywam sie|my name is|imie i nazwisko)\s*[:\-]?[ \t]*"
)
_NAME_STOP = {"team", "zespol", "support", "dzial", "department", "customer", "service", "obsluga",
              "regards", "thanks", "best", "sales", "office", "biuro", "logistics", "hello", "dear",
              "szanowni", "witam", "dzien", "good"}
_LEGAL_SUFFIX = {"ltd", "inc", "llc", "gmbh", "sa", "limited", "corp", "spolka", "sp"}
_ORG_RE = re.compile(
    r"(?:[A-ZĄĆĘŁŃÓŚŹŻ][\w&'’\-]*[ \t]+){1,4}"
    r"(?:Sp\.[ \t]?z[ \t]?o\.[ \t]?o\.|Sp\.[ \t]?k\.|S\.A\.|Ltd\.?|Limited|Inc\.?|LLC|GmbH|Corp\.?)"
)


def _name_ok(text: str) -> bool:
    tokens = [fold(t) for t in re.split(r"[ \t]+", text)]
    return not (tokens[-1] in _LEGAL_SUFFIX or any(t in _NAME_STOP for t in tokens))


def _name_candidate(seg: Segment, s: int, e: int) -> Candidate | None:
    text = seg.original[s:e]
    if not _name_ok(text):
        return None
    if " " not in text.strip() and "\t" not in text:
        return Candidate(FieldName.CONTACT_NAME, s, e, text, FieldStatus.AMBIGUOUS,
                         ((WarningCode.PARTIAL_NAME, "only a single name token was found"),))
    return Candidate(FieldName.CONTACT_NAME, s, e, " ".join(text.split()))


def extract_names(seg: Segment) -> list[Candidate]:
    out: list[Candidate] = []
    f, o = seg.folded, seg.original
    lines: list[tuple[int, int]] = []
    pos = 0
    for line in o.split("\n"):
        lines.append((pos, pos + len(line)))
        pos += len(line) + 1

    for idx, (ls, le) in enumerate(lines):
        line_f = f[ls:le]
        cm = _CLOSING.fullmatch(line_f.strip())
        if cm:  # closing on its own line: name on the next non-empty line
            for ns, ne in lines[idx + 1: idx + 3]:
                if not o[ns:ne].strip():
                    continue
                nm = _NAME_RE.fullmatch(o[ns:ne].strip().rstrip(","))
                if nm:
                    lead = len(o[ns:ne]) - len(o[ns:ne].lstrip())
                    c = _name_candidate(seg, ns + lead, ns + lead + len(nm.group(0)))
                    if c:
                        out.append(c)
                break
            continue
        cm2 = _CLOSING.match(line_f.lstrip())
        if cm2:  # "Best regards, Anna Nowak" on one line
            lead = len(line_f) - len(line_f.lstrip())
            rest_start = ls + lead + cm2.end()
            nm = _NAME_RE.match(o, rest_start + (len(o[rest_start:le]) - len(o[rest_start:le].lstrip())))
            if nm and nm.end() <= le and not o[nm.end():le].strip(" ,."):
                c = _name_candidate(seg, nm.start(), nm.end())
                if c:
                    out.append(c)

    for m in _NAME_LABEL.finditer(f):
        nm = _NAME_RE.match(o, m.end())
        if nm and nm.end() > nm.start():
            c = _name_candidate(seg, nm.start(), nm.end())
            if c:
                out.append(c)
    return out


def extract_organizations(seg: Segment) -> list[Candidate]:
    out: list[Candidate] = []
    for m in _ORG_RE.finditer(seg.original):
        norm = " ".join(m.group(0).split())
        out.append(Candidate(FieldName.ORGANIZATION, m.start(), m.end(), norm))
    return out


# --------------------------------------------------------------------------- contact info

_EMAIL = re.compile(r"(?<![\w.+-])[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}")
_PHONE_CUE = re.compile(
    r"(?:\btel(?:efon)?\.?|\bphone|\bmobile|\bmob\.?|\bkom(?:orka)?\.?|\bfax|\bcall us|\bcall me|\bzadzwon\w*)\s*[:\-]?\s*"
    r"(?P<ph>\+?\(?\d[\d  ().\-]{5,20}\d)"
)
_PHONE_INTL = re.compile(r"(?<![\w+])\+\d{1,3}[  .\-]?(?:\(?\d{1,4}\)?[  .\-]?){2,5}\d")


def extract_emails(seg: Segment) -> list[Candidate]:
    out = []
    for m in _EMAIL.finditer(seg.original):
        addr = m.group(0).rstrip(".")
        status = FieldStatus.VALID
        warns: tuple[tuple[WarningCode, str], ...] = ()
        if ".." in addr:
            status, warns = FieldStatus.INVALID, ((WarningCode.EMAIL_INVALID, "consecutive dots in address"),)
        out.append(Candidate(FieldName.EMAIL, m.start(), m.start() + len(addr), addr.lower(), status, warns))
    return out


def _normalize_phone(raw: str) -> tuple[str, int, bool]:
    stripped = raw.strip()
    plus = stripped.startswith("+")
    digits = re.sub(r"\D", "", stripped)
    if not plus and digits.startswith("00"):
        plus, digits = True, digits[2:]
    return ("+" if plus else "") + digits, len(digits), plus


def extract_phones(seg: Segment) -> list[Candidate]:
    out: list[Candidate] = []
    taken: list[tuple[int, int]] = []

    def add(s: int, e: int, raw: str) -> None:
        norm, ndigits, has_cc = _normalize_phone(raw)
        warns: list[tuple[WarningCode, str]] = []
        status = FieldStatus.VALID
        if not 7 <= ndigits <= 15:
            status = FieldStatus.INVALID
            warns.append((WarningCode.PHONE_LENGTH_INVALID, "phone number must have 7-15 digits"))
        elif not has_cc:
            warns.append((WarningCode.NO_COUNTRY_CODE, "no country code stated; not inferred"))
        out.append(Candidate(FieldName.PHONE, s, e, norm, status, tuple(warns)))
        taken.append((s, e))

    for m in _PHONE_CUE.finditer(seg.folded):
        raw = seg.original[m.start("ph"):m.end("ph")]
        if _DATE_LIKE.match(raw.strip()):
            continue
        add(m.start("ph"), m.end("ph"), raw)
    for m in _PHONE_INTL.finditer(seg.original):
        if all(m.end() <= s or m.start() >= e for s, e in taken):
            add(m.start(), m.end(), m.group(0))
    return out


# --------------------------------------------------------------------------- orchestration

# Lower number wins when candidates of different fields overlap.
_PRIORITY = {
    FieldName.AMOUNT: 0, FieldName.DEADLINE: 1, FieldName.REQUESTED_DATE: 1, FieldName.EMAIL: 2,
    FieldName.PHONE: 3, FieldName.REFERENCE: 4, FieldName.QUANTITY: 5, FieldName.PRODUCT: 6,
    FieldName.ORGANIZATION: 7, FieldName.CONTACT_NAME: 8,
}


def extract_all(seg: Segment) -> tuple[list[Candidate], list[SegmentNote]]:
    quantities = extract_quantities(seg)
    products, extra_qty = extract_products(seg, quantities)
    dates, notes = extract_dates(seg)

    cands: list[Candidate] = []
    cands += extract_amounts(seg)
    cands += dates
    cands += extract_emails(seg)
    cands += extract_phones(seg)
    ref_cands, ref_notes = extract_references(seg)
    cands += ref_cands
    notes += ref_notes
    cands += [c for c, _ in quantities]
    cands += extra_qty
    cands += products
    cands += extract_organizations(seg)
    cands += extract_names(seg)

    # drop lower-priority candidates overlapping a higher-priority one
    cands.sort(key=lambda c: (_PRIORITY[c.name], c.start))
    kept: list[Candidate] = []
    for c in cands:
        if all(c.end <= k.start or c.start >= k.end for k in kept):
            kept.append(c)
    kept.sort(key=lambda c: (c.start, c.end))
    return kept, notes


def warnings_for(seg: Segment, cand: Candidate) -> tuple[AnalysisWarning, ...]:
    return tuple(
        make_warning(code, msg, seg, cand.start, cand.end, cand.name) for code, msg in cand.warnings
    )
