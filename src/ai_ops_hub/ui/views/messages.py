"""Message list: shows import status and lets the user analyze messages that have no case yet."""

from __future__ import annotations

import streamlit as st

from ai_ops_hub.ui.client import ApiClient, ApiError, BackendUnavailable, InvalidResponse
from ai_ops_hub.ui.common import call, go, pager
from ai_ops_hub.ui.labels import IMPORT_STATUS_LABELS, STATUS_LABELS, fmt_dt, label
from ai_ops_hub.ui.safe import clip, esc

SIZE = 15


def _analyze(client: ApiClient, message_id: int) -> None:
    try:
        result = client.analyze(message_id)
    except (ApiError, BackendUnavailable, InvalidResponse) as exc:
        st.session_state["msg_flash"] = ("error", f"Analiza wiadomości #{message_id} nie powiodła się: {exc}")
        return
    st.session_state["msg_flash"] = (
        "success", f"Wiadomość #{message_id} przeanalizowana (rule_based). Przypadek #{result.case.id}."
    )


def render(client: ApiClient) -> None:
    st.header("Wiadomości")
    ss = st.session_state
    ss.setdefault("m_page", 1)

    flash = ss.pop("msg_flash", None)
    if flash:
        (st.success if flash[0] == "success" else st.error)(esc(flash[1]))

    page = call(client.list_messages, limit=SIZE, offset=(ss["m_page"] - 1) * SIZE)
    if page.total > 0 and not page.items:
        ss["m_page"] = max(1, -(-page.total // SIZE))
        page = call(client.list_messages, limit=SIZE, offset=(ss["m_page"] - 1) * SIZE)
    if page.total == 0:
        st.info("Nie zaimportowano jeszcze żadnych wiadomości.")
        return

    header = st.columns([1, 4, 3, 2, 2])
    for col, text in zip(header, ("ID", "Temat", "Import", "Przypadek", "Akcja")):
        col.markdown(f"**{text}**")
    for msg in page.items:
        row = st.columns([1, 4, 3, 2, 2])
        row[0].text(f"#{msg.id}")
        row[1].text(clip(msg.subject or "(bez tematu)", 70))
        row[2].text(f"{label(IMPORT_STATUS_LABELS, msg.import_status)}\n{fmt_dt(msg.imported_at)}")
        row[3].text(label(STATUS_LABELS, msg.case_status) if msg.case_id else "—")
        if msg.case_id:
            row[4].button("Otwórz przypadek", key=f"msg_open_{msg.id}", on_click=go,
                          args=("Szczegóły przypadku", msg.case_id))
        elif msg.import_status == "imported":
            if row[4].button("Analizuj", key=f"msg_analyze_{msg.id}"):
                _analyze(client, msg.id)
                st.rerun()
        else:
            row[4].caption("konflikt: bez analizy")
    pager(page.total, SIZE, "m_page", "msgs")
