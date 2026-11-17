"""Case list: filter by status, search, pagination. Only the visible page is fetched."""

from __future__ import annotations

import streamlit as st

from ai_ops_hub.ui.client import ApiClient
from ai_ops_hub.ui.common import call, go, pager
from ai_ops_hub.ui.labels import CATEGORY_LABELS, STATUS_LABELS, STATUS_ORDER, fmt_dt, label
from ai_ops_hub.ui.safe import clip

PAGE_SIZES = (10, 15, 25, 50)


def _reset_page() -> None:
    st.session_state["f_page"] = 1


def render(client: ApiClient) -> None:
    st.header("Przypadki")
    ss = st.session_state
    ss.setdefault("f_status", "all")
    ss.setdefault("f_q", "")
    ss.setdefault("f_size", 15)
    ss.setdefault("f_page", 1)

    c1, c2, c3 = st.columns([2, 3, 1])
    c1.selectbox(
        "Status", ("all", *STATUS_ORDER), key="f_status", on_change=_reset_page,
        format_func=lambda s: "Wszystkie" if s == "all" else STATUS_LABELS[s],
    )
    c2.text_input("Szukaj (temat lub nadawca)", key="f_q", on_change=_reset_page, max_chars=100)
    c3.selectbox("Na stronie", PAGE_SIZES, key="f_size", on_change=_reset_page)

    status = None if ss["f_status"] == "all" else ss["f_status"]
    q = ss["f_q"].strip() or None
    size = int(ss["f_size"])

    page = call(client.list_cases, status=status, q=q, limit=size, offset=(ss["f_page"] - 1) * size)
    if page.total > 0 and not page.items:  # page number beyond the end (data changed): go to the last page
        ss["f_page"] = max(1, -(-page.total // size))
        page = call(client.list_cases, status=status, q=q, limit=size, offset=(ss["f_page"] - 1) * size)

    if page.total == 0:
        st.info("Brak przypadków dla wybranych filtrów." if (status or q) else
                "Nie ma jeszcze żadnych przypadków. Zaimportuj i przeanalizuj wiadomość.")
        return

    header = st.columns([1, 4, 2, 2, 2, 2])
    for col, text in zip(header, ("ID", "Temat", "Kategoria", "Status", "Zaimportowano", "Akcja")):
        col.markdown(f"**{text}**")
    for case in page.items:
        row = st.columns([1, 4, 2, 2, 2, 2])
        row[0].text(f"#{case.id}")
        row[1].text(clip(case.subject or "(bez tematu)", 70))  # untrusted: plain text, never markdown
        row[2].text(label(CATEGORY_LABELS, case.effective_category))
        row[3].text(label(STATUS_LABELS, case.status))
        row[4].text(fmt_dt(case.imported_at))
        row[5].button("Otwórz", key=f"open_case_{case.id}", on_click=go, args=("Szczegóły przypadku", case.id))
    pager(page.total, size, "f_page", "cases")
