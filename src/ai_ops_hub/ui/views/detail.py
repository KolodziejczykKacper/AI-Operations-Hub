"""Case details and human review.

Three kinds of information are kept visibly apart:
  1. the ORIGINAL email (never changes),
  2. what the automated analysis (rule_based, pattern matching, not AI) extracted,
  3. the HUMAN corrections (separate, append-only on the server).

Safety rules implemented here
  * Untrusted text (email, extracted values, messages) is shown with st.text / st.text_area /
    st.dataframe / st.json, or escaped with ``esc`` when a markdown-only widget is needed.
  * Every state-changing call sends the version the user was LOOKING AT (pinned in
    ``view["version"]``), not the version fetched during the current rerun; the server
    answers 409 ``version_conflict`` if the case moved on, and nothing is overwritten.
  * A call is made only inside a button branch (a button is True for exactly one rerun).
  * Approval / rejection / reopening need a second, explicit confirmation step.
  * No validation or transition rules live here: ``allowed_actions`` and ``problems`` come
    from the server, and the server validates every corrected value.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import pandas as pd
import streamlit as st

from ai_ops_hub.ui import models as m
from ai_ops_hub.ui.client import (
    ApiClient,
    ApiError,
    ApprovalBlocked,
    BackendUnavailable,
    InvalidCorrection,
    InvalidResponse,
    ValidationFailed,
    VersionConflict,
)
from ai_ops_hub.ui.common import call, go, show_backend_down
from ai_ops_hub.ui.labels import (
    ACTION_LABELS,
    CATEGORY_LABELS,
    EVENT_LABELS,
    FIELD_HINTS,
    FIELD_LABELS,
    STATUS_LABELS,
    VALIDITY_LABELS,
    fmt_dt,
    label,
)
from ai_ops_hub.ui.safe import clip, context_snippet, esc

FIELDS = tuple(FIELD_LABELS)
MIN_OVERRIDE_CHARS = 10  # the server enforces its own minimum; this only avoids a pointless round trip
SECTIONS = ("1 · Oryginalny mail", "2 · Analiza i poprawki", "3 · Historia i audyt")
CONFIRM_KEYS = ("confirm_note", "confirm_override", "confirm_ack", "confirm_reason")


# --- view state (what the user is looking at) ------------------------------------------------


def _lines(text: str) -> list[str]:
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def _baseline(d: m.CaseDetail) -> dict[str, str]:
    base = {f.name: "\n".join(v.value for v in f.values) for f in d.fields}
    out = {name: base.get(name, "") for name in FIELDS}
    out["category"] = d.effective_category
    return out


def _init_edit_widgets(d: m.CaseDetail, base: dict[str, str]) -> None:
    ss = st.session_state
    for name in FIELDS:
        ss[f"edit_{name}"] = base[name]
        ss[f"edit_revert_{name}"] = False
    ss["edit_category"] = base["category"]
    ss["edit_note"] = ""


def _clear_transient() -> None:
    ss = st.session_state
    for key in ("pending", "force_override", "errors", *CONFIRM_KEYS):
        ss.pop(key, None)


def _sync_view(d: m.CaseDetail) -> dict:
    """Keep ``ss["view"]`` (case id, pinned version, baseline of the edit form) consistent."""
    ss = st.session_state
    view = ss.get("view")
    if view is None or view["case_id"] != d.case.id:
        _clear_transient()
        base = _baseline(d)
        view = ss["view"] = {"case_id": d.case.id, "version": d.case.version, "baseline": base}
        _init_edit_widgets(d, base)
    elif ss.pop("reset_edit", False):  # after a successful save/decision: forget the draft
        base = _baseline(d)
        view.update(version=d.case.version, baseline=base)
        _init_edit_widgets(d, base)
    elif ss.pop("accept_current", False):  # after a conflict / explicit reload: adopt the newest data
        new_base = _baseline(d)
        for key, old in view["baseline"].items():
            widget = "edit_category" if key == "category" else f"edit_{key}"
            if ss.get(widget) == old:  # untouched by the user: show the new server value
                ss[widget] = new_base[key]
                view["baseline"][key] = new_base[key]
            # touched fields keep the user's text AND the old baseline, so they still count as changes
        for name in FIELDS:
            ss[f"edit_revert_{name}"] = False
        view["version"] = d.case.version
    return view


# --- calling the API for state changes -----------------------------------------------------------


def _problem_lines(problems: list[dict]) -> list[str]:
    lines = []
    for p in problems:
        text = p.get("message") or ""
        value = p.get("value")
        lines.append(f"{text} (wartość: {value})" if value not in (None, "") else text)
    return [ln for ln in lines if ln]


def _run(fn: Callable[[], m.CaseDetail], success: str) -> None:
    """Perform ONE state-changing call and translate every outcome for the user."""
    ss = st.session_state
    ss.pop("errors", None)
    try:
        fn()
    except VersionConflict as exc:
        ss["flash"] = (
            "warning",
            "Ten przypadek został w międzyczasie zmieniony (aktualna wersja: "
            f"{exc.current_version}). Wczytano aktualny stan. Twoje niezapisane zmiany w formularzu "
            "zachowano: sprawdź je i zapisz ponownie. Nic nie zostało nadpisane.",
        )
        ss["accept_current"] = True
        ss.pop("pending", None)
        st.rerun()
    except ApprovalBlocked as exc:
        ss["force_override"] = True
        ss["errors"] = [
            "Serwer odmówił zatwierdzenia: dane przypadku mają nierozwiązane problemy. "
            "Popraw je albo podaj pisemne uzasadnienie zatwierdzenia mimo ostrzeżeń.",
            *_problem_lines(exc.problems),
        ]
    except InvalidCorrection as exc:
        ss["errors"] = [f"Serwer odrzucił poprawkę: {exc.message}", *_problem_lines(exc.problems)]
    except ValidationFailed as exc:
        ss["errors"] = [f"Nieprawidłowe dane: {exc.message}", *[str(p.get("message", "")) for p in exc.problems]]
    except ApiError as exc:
        ss["errors"] = [f"Błąd API ({exc.code}): {exc.message}"]
    except BackendUnavailable as exc:
        note = (
            " Nie wiadomo, czy operacja została wykonana: przeładuj przypadek i sprawdź historię "
            "przed ponowieniem (nic nie jest ponawiane automatycznie)."
            if exc.outcome_unknown
            else ""
        )
        ss["errors"] = [f"{exc}{note}"]
    except InvalidResponse as exc:
        ss["errors"] = [f"Nieoczekiwana odpowiedź API: {exc}"]
    else:
        ss["flash"] = ("success", success)
        ss["reset_edit"] = True
        for key in ("pending", "force_override", *CONFIRM_KEYS):
            ss.pop(key, None)
        st.rerun()
    # A failure: the banners that show ss["errors"] were drawn earlier in this run, so run once
    # more to display the message. Widget values (the user's draft) live in session state and survive.
    st.rerun()


# --- rendering helpers ---------------------------------------------------------------------------


def _show_errors() -> None:
    errors = st.session_state.get("errors")
    if errors:
        st.error("**Operacja nie została wykonana.**\n\n" + "\n\n".join(f"- {esc(e)}" for e in errors))


def _source_text(d: m.CaseDetail, span: m.Span | None) -> str:
    if span is None:
        return "—"
    base = (d.message.subject or "") if span.source == "subject" else d.message.body_text
    prefix = "[temat] " if span.source == "subject" else ""
    return prefix + (context_snippet(base, span.start, span.end) or span.text)


def _field_table(d: m.CaseDetail) -> pd.DataFrame:
    msgs = {(f.name, f.value.normalized): [w.message or w.code for w in f.warnings] for f in d.analysis.result.fields}
    rows: list[dict[str, str]] = []
    for f in d.fields:
        name = label(FIELD_LABELS, f.name)
        if f.corrected:
            human = "; ".join(v.value for v in f.values) or "(człowiek: brak wartości)"
            rows.append({
                "Pole": name,
                "Wartość z analizy (rule_based)": "; ".join(f.analyzer_values) or "—",
                "Poprawka człowieka": human,
                "Obowiązuje": "; ".join(v.value for v in f.values) or "—",
                "Status": "✅ wpisane przez człowieka",
                "Fragment źródłowy": "—",
                "Ostrzeżenia": "",
            })
            continue
        for v in f.values:
            rows.append({
                "Pole": name,
                "Wartość z analizy (rule_based)": v.value,
                "Poprawka człowieka": "—",
                "Obowiązuje": v.value,
                "Status": label(VALIDITY_LABELS, v.status),
                "Fragment źródłowy": _source_text(d, v.span),
                "Ostrzeżenia": "; ".join(msgs.get((f.name, v.value), []) or v.warnings),
            })
    return pd.DataFrame(rows)


def _flash() -> None:
    flash = st.session_state.pop("flash", None)
    if flash:
        {"success": st.success, "warning": st.warning, "error": st.error}[flash[0]](esc(flash[1]))


# --- tabs -------------------------------------------------------------------------------------------


def _tab_original(d: m.CaseDetail) -> None:
    msg = d.message
    st.caption("ORYGINAŁ: treść wiadomości nigdy nie jest zmieniana i jest wyświetlana jako zwykły tekst.")
    st.text(f"Temat:   {msg.subject or '(brak)'}")
    st.text(f"Od:      {msg.from_addr or '(brak)'}")
    st.text(f"Do:      {', '.join(msg.to_addrs) or '(brak)'}")
    st.text(f"Data:    {msg.date_raw or '(brak)'}")
    st.text(f"Źródło:  {msg.source_kind}  ·  zaimportowano {fmt_dt(msg.imported_at)}  ·  "
            f"import: {msg.import_status}")
    st.text_area("Treść", value=msg.body_text, height=320, disabled=True, key=f"body_{d.case.id}")
    if msg.warnings:
        with st.expander("Uwagi parsera"):
            for w in msg.warnings:
                st.text(w)


def _tab_analysis(client: ApiClient, d: m.CaseDetail, view: dict) -> None:
    a = d.analysis
    st.markdown("#### Wyniki analizy automatycznej")
    st.info(
        f"Metoda: **{esc(a.analysis_method)}** ({esc(a.analyzer_name)} {esc(a.analyzer_version)}), "
        "czyli dopasowywanie wzorców według reguł. To **nie jest** model AI i nie rozumie tekstu. "
        "Wyniki to sugestie do sprawdzenia."
    )
    cat_line = (
        f"Kategoria: analizator → {label(CATEGORY_LABELS, d.analyzer_category)}"
        f"  ·  obowiązująca → {label(CATEGORY_LABELS, d.effective_category)}"
        + ("  (poprawiona przez człowieka)" if d.category_corrected else "")
    )
    st.text(cat_line)
    if a.result.category_alternatives:
        st.text("Analizator widział też dowody dla: "
                + ", ".join(label(CATEGORY_LABELS, c) for c in a.result.category_alternatives))

    table = _field_table(d)
    if table.empty:
        st.caption("Analiza nie wyodrębniła żadnych pól.")
    else:
        st.dataframe(table, hide_index=True, width="stretch")

    warnings = a.result.warnings
    reasons = a.result.review_reasons
    if warnings or reasons:
        with st.expander(f"Ostrzeżenia i powody przeglądu ({len(warnings) + len(reasons)})"):
            for r in reasons:
                st.text(f"[przegląd] {r.code}: {r.message}")
            for w in warnings:
                where = f"  ← „{clip(w.span.text, 60)}”" if w.span else ""
                st.text(f"[ostrzeżenie] {w.code}: {w.message}{where}")

    _edit_form(client, d, view)


def _edit_form(client: ApiClient, d: m.CaseDetail, view: dict) -> None:
    st.markdown("#### Poprawki człowieka")
    if "edit" not in d.allowed_actions:
        st.info("Przypadek jest zamknięty. Aby go edytować, najpierw otwórz go ponownie (sekcja „Decyzja”).")
        return
    st.caption(
        "Każda wartość w osobnej linii; puste pole oznacza „brak wartości”. Format sprawdza serwer. "
        "Oryginalny mail i wynik analizy pozostają niezmienione: poprawki są zapisywane osobno."
    )
    ss = st.session_state
    st.selectbox(
        "Kategoria", list(CATEGORY_LABELS), key="edit_category",
        format_func=lambda c: CATEGORY_LABELS.get(c, c),
    )
    corrected = {f.name for f in d.fields if f.corrected}
    for name in FIELDS:
        c1, c2 = st.columns([4, 1])
        hint = f" ({FIELD_HINTS[name]})" if name in FIELD_HINTS else ""
        c1.text_area(f"{FIELD_LABELS[name]}{hint}", key=f"edit_{name}", height=70)
        if name in corrected:
            c2.checkbox("Przywróć wartość z analizy", key=f"edit_revert_{name}")
    st.text_area("Notatka przeglądu (opcjonalnie)", key="edit_note", height=80, max_chars=2000)

    if st.button("Zapisz poprawki", type="primary", key="btn_save_edit"):
        base = view["baseline"]
        corrections: dict[str, list[str] | None] = {}
        for name in FIELDS:
            if name in corrected and ss.get(f"edit_revert_{name}"):
                corrections[name] = None
            elif _lines(ss[f"edit_{name}"]) != _lines(base[name]):
                corrections[name] = _lines(ss[f"edit_{name}"])
        if ss["edit_category"] != base["category"]:
            corrections["category"] = [ss["edit_category"]]
        note = ss["edit_note"].strip() or None
        if not corrections and not note:
            st.info("Brak zmian do zapisania.")
        else:
            _run(
                lambda: client.edit_case(d.case.id, view["version"], corrections, note),
                "Zapisano poprawki. Oryginał i analiza pozostały bez zmian.",
            )


def _tab_history(client: ApiClient, d: m.CaseDetail) -> None:
    history = call(client.history, d.case.id)
    audit = call(client.case_audit, d.case.id)

    st.markdown("#### Poprawki człowieka")
    if history.corrections:
        st.dataframe(pd.DataFrame([
            {
                "Czas": fmt_dt(c.created_at),
                "Pole": label(FIELD_LABELS, c.target) if c.target != "category" else "Kategoria",
                "Akcja": label(ACTION_LABELS, c.action),
                "Przed": "; ".join(c.previous_values) or "—",
                "Po": "; ".join(c.values) or "—",
                "Wersja": c.version_after,
            } for c in history.corrections
        ]), hide_index=True, width="stretch")
    else:
        st.caption("Brak poprawek człowieka.")

    st.markdown("#### Decyzje i notatki")
    if history.reviews:
        st.dataframe(pd.DataFrame([
            {
                "Czas": fmt_dt(r.created_at),
                "Akcja": label(ACTION_LABELS, r.action),
                "Status": f"{label(STATUS_LABELS, r.from_status)} → {label(STATUS_LABELS, r.to_status)}",
                "Notatka / powód": r.note or "",
                "Zatwierdzono mimo ostrzeżeń": r.override_reason or "",
            } for r in history.reviews
        ]), hide_index=True, width="stretch")
    else:
        st.caption("Brak decyzji ani notatek.")

    st.markdown("#### Historia audytu")
    st.caption("Kto, co i kiedy. Zdarzenia „system” wykonuje aplikacja, „human” człowiek "
               "(etykieta local_demo_user, bez uwierzytelniania).")
    st.dataframe(pd.DataFrame([
        {
            "Nr": e.id,
            "Czas": fmt_dt(e.timestamp),
            "Zdarzenie": label(EVENT_LABELS, e.event_type),
            "Aktor": e.actor + (f" ({e.details['reviewer']})" if e.details.get("reviewer") else ""),
            "Szczegóły": clip(json.dumps(e.details, ensure_ascii=False), 140),
        } for e in audit
    ]), hide_index=True, width="stretch")
    with st.expander("Pełne dane zdarzeń audytu"):
        st.json([e.model_dump(mode="json") for e in audit])


# --- decisions -------------------------------------------------------------------------------------


def _start_decision(action: str) -> None:
    ss = st.session_state
    view = ss["view"]
    for key in ("force_override", "errors", *CONFIRM_KEYS):
        ss.pop(key, None)
    ss["pending"] = {"case_id": view["case_id"], "action": action, "version": view["version"]}


def _cancel_decision() -> None:
    for key in ("pending", "force_override", "errors", *CONFIRM_KEYS):
        st.session_state.pop(key, None)


def _decision_section(client: ApiClient, d: m.CaseDetail) -> None:
    st.markdown("### Decyzja człowieka")
    allowed = set(d.allowed_actions)
    pend = st.session_state.get("pending")
    if pend and pend["case_id"] == d.case.id:
        _confirm_panel(client, d, pend)
        return
    st.caption("Żadna decyzja nie zapada automatycznie. Każda wymaga Twojego potwierdzenia.")
    cols = st.columns(3)
    if allowed & {"approve", "approve_with_override"}:
        cols[0].button("Zatwierdź…", key="btn_approve", on_click=_start_decision, args=("approve",))
    if "reject" in allowed:
        cols[1].button("Odrzuć…", key="btn_reject", on_click=_start_decision, args=("reject",))
    if "reopen" in allowed:
        cols[2].button("Otwórz ponownie…", key="btn_reopen", on_click=_start_decision, args=("reopen",))


def _effective_summary(d: m.CaseDetail) -> None:
    st.text(f"Kategoria: {label(CATEGORY_LABELS, d.effective_category)}")
    for f in d.fields:
        if f.values:
            st.text(f"{label(FIELD_LABELS, f.name)}: " + "; ".join(v.value for v in f.values))


def _confirm_panel(client: ApiClient, d: m.CaseDetail, pend: dict) -> None:
    action, version, cid = pend["action"], pend["version"], d.case.id
    with st.container(border=True):
        if action == "approve":
            st.markdown(f"#### Potwierdź zatwierdzenie przypadku #{cid}")
            st.write(f"Zatwierdzasz dane w wersji {version}, dokładnie w tej postaci:")
            _effective_summary(d)
            need_override = bool(d.problems) or st.session_state.get("force_override", False)
            if need_override:
                st.warning("**Dane mają nierozwiązane problemy.** Zatwierdzenie jest możliwe tylko z pisemnym "
                           "uzasadnieniem, które zostanie zapisane razem z decyzją:")
                for p in d.problems:
                    st.text(f"• {p.message}")
                st.text_area(f"Uzasadnienie (min. {MIN_OVERRIDE_CHARS} znaków)", key="confirm_override", height=90)
                st.checkbox("Rozumiem, że zatwierdzam mimo ostrzeżeń", key="confirm_ack")
            st.text_area("Notatka do decyzji (opcjonalnie)", key="confirm_note", height=70)
            reason = st.session_state.get("confirm_override", "").strip()
            ready = (not need_override) or (
                len(reason) >= MIN_OVERRIDE_CHARS and st.session_state.get("confirm_ack", False)
            )
            _show_errors()
            c1, c2 = st.columns(2)
            if c1.button("Tak, zatwierdzam", type="primary", disabled=not ready, key="btn_confirm_approve"):
                note = st.session_state.get("confirm_note", "").strip() or None
                _run(
                    lambda: client.approve(cid, version, note, reason if need_override else None),
                    f"Przypadek #{cid} został zatwierdzony. Nic nie zostało wysłane ani wykonane na zewnątrz.",
                )
            c2.button("Anuluj", key="btn_cancel_decision", on_click=_cancel_decision)
            return

        verb = {"reject": "odrzucenie", "reopen": "ponowne otwarcie"}[action]
        st.markdown(f"#### Potwierdź {verb} przypadku #{cid}")
        reason_text = st.text_area("Powód (wymagany)", key="confirm_reason", height=90)
        _show_errors()
        c1, c2 = st.columns(2)
        label_ok = "Tak, odrzucam" if action == "reject" else "Tak, otwieram ponownie"
        if c1.button(label_ok, type="primary", disabled=len(reason_text.strip()) < 3, key=f"btn_confirm_{action}"):
            fn = client.reject if action == "reject" else client.reopen
            _run(
                lambda: fn(cid, version, reason_text.strip()),
                f"Przypadek #{cid}: {verb} zapisane.",
            )
        c2.button("Anuluj", key="btn_cancel_decision", on_click=_cancel_decision)


# --- screen ------------------------------------------------------------------------------------------


def _picker(client: ApiClient) -> None:
    st.header("Szczegóły przypadku")
    st.info("Wybierz przypadek z listy (ekran **Przypadki**) albo wpisz jego numer.")
    number = st.number_input("Numer przypadku", min_value=1, step=1, value=1, key="open_by_id")
    st.button("Otwórz", key="btn_open_by_id", on_click=go, args=("Szczegóły przypadku", int(number)))


def render(client: ApiClient) -> None:
    ss = st.session_state
    case_id = ss.get("selected_case")
    if case_id is None:
        _picker(client)
        return

    try:
        detail = client.get_case(case_id)
    except ApiError as exc:
        st.header("Szczegóły przypadku")
        st.error(f"Nie można otworzyć przypadku #{case_id}: {esc(exc.message)} ({esc(exc.code)})")
        if exc.status == 404:
            ss.pop("selected_case", None)
        st.button("Wróć do listy", on_click=go, args=("Przypadki",), key="btn_back_error")
        return
    except BackendUnavailable as exc:
        show_backend_down(exc)
        st.stop()
    except InvalidResponse as exc:
        st.error(f"Nieoczekiwana odpowiedź API: {esc(exc)}")
        st.stop()

    view = _sync_view(detail)
    case = detail.case

    top = st.columns([4, 1])
    top[0].header(f"Przypadek #{case.id}")
    top[1].button("← Lista", on_click=go, args=("Przypadki",), key="btn_back_list")
    st.text(case.subject or "(bez tematu)")  # untrusted -> plain text

    m1, m2, m3, m4 = st.columns([4, 1, 3, 3])  # status text is long: give it room
    m1.metric("Status", label(STATUS_LABELS, case.status))
    m2.metric("Wersja", case.version)
    m3.metric("Kategoria", label(CATEGORY_LABELS, detail.effective_category))
    m4.metric("Metoda analizy", detail.analysis.analysis_method)
    st.caption("Analiza regułowa (rule_based): dopasowanie wzorców, nie model AI. "
               f"Recenzent: {esc(detail.reviewer_label)} (etykieta, bez uwierzytelniania).")

    _flash()
    if view["version"] != case.version:
        st.warning(
            f"Przypadek zmienił się od chwili, gdy go otworzyłeś (widziana wersja {view['version']}, "
            f"aktualna {case.version}). Zapis na starej wersji zostanie odrzucony przez serwer."
        )
        st.button("Wczytaj aktualny stan", key="btn_accept_current",
                  on_click=lambda: st.session_state.__setitem__("accept_current", True))

    if case.status == "approved":
        st.success("Przypadek został ZATWIERDZONY przez człowieka.")
    elif case.status == "rejected":
        st.error("Przypadek został ODRZUCONY przez człowieka.")
    if detail.problems and case.status in ("needs_review", "ready_for_review"):
        st.warning("**Do rozwiązania przed zatwierdzeniem:**\n\n" + "\n".join(f"- {esc(p.message)}" for p in detail.problems))
    if not ss.get("pending"):
        _show_errors()

    # A radio instead of st.tabs: only the chosen section is rendered (and fetched), and tables
    # drawn inside a hidden tab collapse to zero width in the browser until the window is resized.
    ss.setdefault("detail_section", SECTIONS[1])
    section = st.radio("Sekcja", SECTIONS, key="detail_section", horizontal=True, label_visibility="collapsed")
    if section == SECTIONS[0]:
        _tab_original(detail)
    elif section == SECTIONS[1]:
        _tab_analysis(client, detail, view)
    else:
        _tab_history(client, detail)

    st.divider()
    _decision_section(client, detail)
