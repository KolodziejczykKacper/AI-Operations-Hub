"""Optional Claude analysis and read-only comparison with the rule-based analysis.

Rules of this screen
* NOTHING is sent to Anthropic unless the user ticks the consent box and clicks the paid button
  in THIS run. Reruns, navigation and imports never call Claude (the preview is computed
  locally by the API: no network, no cost).
* The consent box is keyed with a counter that grows after every paid click, so every request
  needs a fresh, explicit consent.
* A failed request shows an error and creates no analysis.
* An existing case keeps its analysis; the comparison is read-only.
"""

from __future__ import annotations

import streamlit as st

from ai_ops_hub.ui.client import ApiClient, ApiError, BackendUnavailable, ClientError
from ai_ops_hub.ui.common import call, go
from ai_ops_hub.ui.labels import CATEGORY_LABELS, STATUS_LABELS, VALIDITY_LABELS, FIELD_LABELS, fmt_dt, label
from ai_ops_hub.ui.models import Analysis, ClaudePreview, ClaudeStatus
from ai_ops_hub.ui.safe import clip, esc

METHOD_LABELS = {
    "rule_based": "rule_based (reguły, offline, bez AI)",
    "llm": "Claude API (model językowy, płatne)",
    "human": "człowiek",
}
PRIVACY_PL = (
    "Treść tego e-maila (temat i treść) zostanie przesłana do Anthropic, zewnętrznej usługi w chmurze, i będzie przetwarzana "
    "na jej zasadach. Nie używaj prawdziwych, osobistych ani poufnych e-maili do demonstracji."
)
NOT_SENT_PL = "adres nadawcy, odbiorcy i nagłówki"
COST_PL = (
    "Koszty to lokalne szacunki z tabeli cen, a nie gwarantowany limit wydatków. Źródłem prawdy jest konto Anthropic: "
    "ustaw tam limit rozliczeń."
)
VERDICT_LABELS = {
    "same": "✅ zgodne",
    "different": "⚠️ różne",
    "only_a": "tylko rule_based",
    "only_b": "tylko Claude",
}


def analysis_title(a: Analysis) -> str:
    version = a.analyzer_version if a.analysis_method == "llm" else ""
    return f"#{a.id} · {METHOD_LABELS.get(a.analysis_method, a.analysis_method)} {version}".strip()


def money(value: float | None) -> str:
    return "—" if value is None else f"${value:.5f}"


def _error_text(exc: ClientError) -> str:
    if isinstance(exc, ApiError):
        return f"{exc.message} ({exc.code})"
    if isinstance(exc, BackendUnavailable) and exc.outcome_unknown:
        return (
            "Nie wiadomo, czy żądanie dotarło do Claude (połączenie przerwano w trakcie). Mogło zostać naliczone. "
            "Sprawdź tabelę użycia przed ponowną próbą. " + str(exc)
        )
    return str(exc)


def _blocked_pl(status: ClaudeStatus) -> str:
    """Polish reason why the paid analysis is unavailable (the API's own texts are English)."""
    if not status.enabled:
        return "Analiza Claude jest wyłączona (ustawienie domyślne)."
    if not status.key_configured:
        return "Analiza Claude jest włączona, ale brakuje klucza API (zmienna `ANTHROPIC_API_KEY`)."
    if not status.model_has_known_price:
        return f"Model {esc(status.model)} nie ma znanej ceny, więc nie można oszacować kosztu."
    return esc(status.blocked_reason or "Analiza Claude jest niedostępna.")


def _status_panel(status: ClaudeStatus) -> None:
    with st.expander("Stan i limity Claude API", expanded=not status.can_analyze):
        if status.can_analyze:
            st.success("Analiza Claude jest włączona w konfiguracji. Nadal nic nie jest wysyłane bez Twojej zgody.")
        else:
            st.info(_blocked_pl(status))
            st.markdown(
                "Aby ją włączyć (opcjonalnie): ustaw zmienną `AIOPS_CLAUDE_ENABLED=true` i `ANTHROPIC_API_KEY` "
                "w środowisku lub w ignorowanym przez Git pliku `.env`, a potem uruchom ponownie backend. "
                "Szczegóły: README i docs/TECHNICAL_NOTES.md. Tryb offline (rule_based) działa zawsze."
            )
        c = st.columns([2, 1, 1, 1])
        c[0].metric("Model", status.model)
        c[1].metric("Limit na żądanie", money(status.max_cost_usd_per_request))
        c[2].metric("Budżet 24 h (lokalny)", money(status.daily_budget_usd))
        c[3].metric("Wydano 24 h (szac.)", money(status.spent_last_24h_usd))
        st.caption(f"Adres API: {esc(status.endpoint_host)}" + (" (niestandardowy!)" if status.custom_endpoint else " (oficjalny)"))
        st.caption(COST_PL)


def _send(client: ApiClient, preview: ClaudePreview) -> None:
    ss = st.session_state
    ss.pop("cl_error", None)
    ss.pop("cl_result", None)
    ss["cl_n"] = ss.get("cl_n", 0) + 1  # the consent checkbox is new next time: consent never carries over
    try:
        result = client.claude_analyze(
            preview.message_id,
            model=preview.model,
            accepted_max_cost_usd=preview.estimated_max_cost_usd,
            content_fingerprint=preview.content_fingerprint,
        )
    except ClientError as exc:
        ss["cl_error"] = (preview.message_id, _error_text(exc))
        return
    ss["cl_result"] = (preview.message_id, result)


def _claude_section(client: ApiClient, status: ClaudeStatus, mid: int) -> None:
    ss = st.session_state
    st.subheader("Analiza Claude (płatna, opcjonalna)")
    preview = call(client.claude_preview, mid)

    if preview.already_analyzed:
        st.info(
            f"Ta wiadomość ma już analizę Claude dla modelu {esc(preview.model)} (analiza #{preview.existing_analysis_id}). "
            "Ponowne wysyłanie nie jest potrzebne i nic nie zostanie wysłane."
        )
    elif not preview.can_send:
        st.error(esc(preview.blocked_reason or "Analiza Claude jest niedostępna."))
    else:
        c = st.columns([2, 1, 1, 1])
        c[0].metric("Model", preview.model)
        c[1].metric("Wysyłane znaki", preview.subject_chars + preview.body_chars)
        c[2].metric("Szac. tokeny wejścia", preview.estimated_input_tokens)
        c[3].metric("Maks. koszt (szac.)", money(preview.estimated_max_cost_usd))
        st.warning(f"**{PRIVACY_PL}** Adres docelowy: `{esc(preview.endpoint_host)}`. Nie są wysyłane: {NOT_SENT_PL}.")
        if preview.custom_endpoint:
            st.error(
                f"UWAGA: skonfigurowano niestandardowy adres API (`{esc(preview.endpoint_host)}`). Twój klucz API i treść "
                "e-maila trafią tam, a nie do api.anthropic.com. Wysyłaj tylko, jeśli to zamierzone."
            )
        st.caption(COST_PL)
        ticked = st.checkbox(
            f"Rozumiem, że treść e-maila zostanie wysłana do Anthropic i że to żądanie jest płatne "
            f"(szacunkowo do ${preview.estimated_max_cost_usd:.4f}).",
            key=f"cl_confirm_{mid}_{ss.get('cl_n', 0)}",
        )
        if st.button("Wyślij do Claude (płatne)", key="claude_send", type="primary", disabled=not ticked) and ticked:
            _send(client, preview)
            st.rerun()  # redraw with a NEW, unticked consent box: a stale click can never resend

    error = ss.get("cl_error")
    if error and error[0] == mid:
        st.error("Żądanie do Claude nie powiodło się. Żadna analiza nie została zapisana. " + esc(error[1]))
    result = ss.get("cl_result")
    if result and result[0] == mid:
        r = result[1]
        note = (
            f"Istniejący przypadek #{r.case_id} zachowuje swoją analizę; porównaj je tylko do odczytu."
            if r.case_id
            else "Nie utworzono przypadku. Utwórz go jawnie z wybranej analizy."
        )
        st.success(f"Zapisano analizę Claude #{r.analysis.id} (wersja: {esc(r.analysis.analyzer_version)}). {note}")
        if r.usage:
            u = r.usage
            c = st.columns(4)
            c[0].metric("Tokeny wejścia / wyjścia", f"{u.input_tokens} / {u.output_tokens}")
            c[1].metric("Koszt rzeczywisty (z użycia)", money(u.actual_cost_usd))
            c[2].metric("Opóźnienie", f"{u.latency_ms} ms")
            c[3].metric("Odrzucone wartości", f"{u.values_rejected} z {u.values_proposed}")


def _usage_table(client: ApiClient, mid: int) -> None:
    usage = call(client.claude_usage, mid)
    if not usage:
        return
    with st.expander(f"Zapisane żądania do Claude ({len(usage)})"):
        st.dataframe(
            [
                {
                    "Status": {"ok": "ok", "reserved": "w toku / niedokończone"}.get(u.status, f"błąd: {u.error_type}"),
                    "Model": u.model,
                    "Tokeny we/wy": f"{u.input_tokens if u.input_tokens is not None else '—'} / "
                    f"{u.output_tokens if u.output_tokens is not None else '—'}",
                    "Koszt rzeczywisty": money(u.actual_cost_usd),
                    "Maks. szacunek": money(u.estimated_max_cost_usd),
                    "Opóźnienie (ms)": u.latency_ms,
                    "Odrzucone/zaproponowane": f"{u.values_rejected}/{u.values_proposed}",
                    "Analiza": u.analysis_id,
                }
                for u in usage
            ],
            hide_index=True,
        )
        st.caption("Nie zapisujemy treści e-maili ani odpowiedzi modelu, tylko liczniki. Źródłem prawdy o kosztach jest konto Anthropic.")


def _comparison(client: ApiClient, mid: int, analyses: list[Analysis]) -> None:
    st.subheader("Porównanie analiz (tylko do odczytu)")
    methods = {a.analysis_method for a in analyses}
    if not {"rule_based", "llm"} <= methods:
        st.info("Porównanie wymaga obu analiz: rule_based i Claude. Brakującą uzupełnij (analiza rule_based: ekran Wiadomości).")
        return
    cmp = call(client.compare_analyses, mid)
    c = st.columns(5)
    c[0].metric("Kategoria rule_based", label(CATEGORY_LABELS, cmp.category_a))
    c[1].metric("Kategoria Claude", label(CATEGORY_LABELS, cmp.category_b))
    c[2].metric("Pola zgodne", cmp.same)
    c[3].metric("Pola różne", cmp.different)
    c[4].metric("Tylko w jednej", cmp.only_a + cmp.only_b)
    if not cmp.category_match:
        st.warning("Analizatory wybrały różne kategorie. Decyzję podejmuje człowiek; żadna analiza nie jest „prawdziwsza”.")
    rows = [
        {
            "Pole": label(FIELD_LABELS, d.name),
            "rule_based": "; ".join(d.values_a) or "—",
            "Claude": "; ".join(d.values_b) or "—",
            "Wynik": VERDICT_LABELS.get(d.verdict, d.verdict),
            "Status rule_based": ", ".join(label(VALIDITY_LABELS, s) for s in d.statuses_a) or "—",
            "Status Claude": ", ".join(label(VALIDITY_LABELS, s) for s in d.statuses_b) or "—",
        }
        for d in cmp.fields
    ]
    st.dataframe(rows, hide_index=True) if rows else st.caption("Żadna z analiz nie wyodrębniła pól.")
    st.caption(
        f"Przegląd wymagany: rule_based {'tak' if cmp.requires_review_a else 'nie'}, Claude "
        f"{'tak' if cmp.requires_review_b else 'nie'}. Ostrzeżenia: {cmp.warning_count_a} / {cmp.warning_count_b}. "
        "Nie pokazujemy wyników punktowych reguł jako prawdopodobieństw: nie są skalibrowane."
    )
    llm = next((a for a in reversed(analyses) if a.analysis_method == "llm"), None)
    if llm:
        rejected = [w for w in llm.result.warnings if w.code == "llm_value_rejected"]
        if rejected:
            with st.expander(f"Wartości zaproponowane przez Claude i odrzucone lokalnie ({len(rejected)})"):
                for w in rejected:
                    st.text(clip(w.message, 200))
                st.caption("Odrzucamy wartości, których nie potwierdza tekst e-maila (zmyślone lub niepoparte źródłem).")


def _create_case(client: ApiClient, message_id: int, analysis_id: int) -> None:
    """Button callback (runs before the rerun, so it may change the navigation state)."""
    try:
        case = client.create_case(message_id, analysis_id)
    except ClientError as exc:
        st.session_state["cl_case_error"] = _error_text(exc)
        return
    st.session_state.pop("cl_case_error", None)
    go("Szczegóły przypadku", case.id)


def _case_section(client: ApiClient, mid: int, case_id: int | None, case_status: str | None, analyses: list[Analysis]) -> None:
    st.subheader("Przypadek (przegląd ręczny)")
    if case_id is not None:
        detail = call(client.get_case, case_id)
        st.info(
            f"Przypadek #{case_id} ({label(STATUS_LABELS, case_status)}) przegląda analizę "
            f"{esc(analysis_title(detail.analysis))}. Istniejący przypadek zachowuje swoją analizę i poprawki; "
            "analiza Claude go nie zmienia (tylko porównanie)."
        )
        st.button("Otwórz przypadek", key="claude_open_case", on_click=go, args=("Szczegóły przypadku", case_id))
        return
    if not analyses:
        st.caption("Najpierw wykonaj analizę (rule_based na ekranie Wiadomości albo Claude powyżej).")
        return
    by_id = {a.id: a for a in analyses}
    pick = st.selectbox(
        "Utwórz przypadek z analizy", list(by_id), key=f"cl_case_pick_{mid}",
        format_func=lambda i: analysis_title(by_id[i]),
    )
    st.caption("Przypadek nigdy nie jest tworzony automatycznie, a zatwierdza go wyłącznie człowiek.")
    st.button("Utwórz przypadek z wybranej analizy", key="claude_create_case", on_click=_create_case,
              args=(client, mid, pick))
    if st.session_state.get("cl_case_error"):
        st.error(esc(st.session_state["cl_case_error"]))


def render(client: ApiClient) -> None:
    ss = st.session_state
    st.header("Analiza Claude (opcjonalna)")
    st.write(
        "Domyślnie analiza jest **regułowa (rule_based)**: offline, darmowa, bez AI. Tutaj możesz **jawnie** "
        "zlecić też analizę modelem Claude (płatne, wymaga klucza API i Twojej zgody dla każdego żądania) "
        "i porównać wyniki. Import ani odświeżenie strony nigdy nie wysyłają danych do Claude."
    )
    status = call(client.claude_status)
    _status_panel(status)

    ss.setdefault("cl_message", 1)
    st.number_input("Numer wiadomości", min_value=1, step=1, key="cl_message")
    mid = int(ss["cl_message"])
    try:
        view = client.get_message(mid)
    except ApiError as exc:
        st.warning(f"Nie ma wiadomości #{mid} ({esc(exc.code)}).")
        return
    except ClientError as exc:
        st.error(esc(exc))
        return

    msg = view.message
    st.text(f"Temat: {clip(msg.subject or '(bez tematu)', 100)}")
    if msg.import_status != "imported":
        st.error("Ta wiadomość została zaimportowana jako KONFLIKT: nie jest analizowana, także przez Claude.")
        return

    analyses = call(client.list_analyses, mid)
    if analyses:
        st.dataframe(
            [
                {
                    "Analiza": f"#{a.id}",
                    "Metoda": METHOD_LABELS.get(a.analysis_method, a.analysis_method),
                    "Wersja": a.analyzer_version,
                    "Kategoria": label(CATEGORY_LABELS, a.category),
                    "Pola": len(a.result.fields),
                    "Wymaga przeglądu": "tak" if a.requires_review else "nie",
                    "Utworzono": fmt_dt(a.created_at),
                }
                for a in analyses
            ],
            hide_index=True,
        )
    else:
        st.caption("Ta wiadomość nie ma jeszcze żadnej analizy.")

    _claude_section(client, status, mid)
    _usage_table(client, mid)
    _comparison(client, mid, analyses)
    _case_section(client, mid, view.case_id, view.case_status, analyses)
