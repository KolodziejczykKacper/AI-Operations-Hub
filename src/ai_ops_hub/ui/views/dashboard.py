"""Dashboard: exact counts straight from the API (``GET /cases/stats``)."""

from __future__ import annotations

import streamlit as st

from ai_ops_hub.ui.client import ApiClient
from ai_ops_hub.ui.common import call, go
from ai_ops_hub.ui.labels import STATUS_LABELS


def _open_list(status: str | None) -> None:
    st.session_state["f_status"] = status or "all"
    st.session_state["f_page"] = 1
    go("Przypadki")


def render(client: ApiClient) -> None:
    st.header("Panel")
    stats = call(client.stats)

    cols = st.columns(5)
    cols[0].metric("Wszystkie przypadki", stats.total)
    for col, status in zip(cols[1:], ("needs_review", "ready_for_review", "approved", "rejected")):
        col.metric(STATUS_LABELS[status], stats.by_status.get(status, 0))

    st.caption(
        "Liczby pochodzą bezpośrednio z API i dotyczą wszystkich przypadków, "
        "a nie tylko aktualnie wyświetlanej strony listy."
    )
    if stats.total == 0:
        st.info("Nie ma jeszcze żadnych przypadków. Zacznij od ekranu **Import**.")
        st.button("Przejdź do importu", on_click=go, args=("Import",), key="dash_to_import")
        return

    st.subheader("Co dalej?")
    c1, c2 = st.columns(2)
    c1.button(
        f"Pokaż przypadki do poprawy ({stats.by_status.get('needs_review', 0)})",
        on_click=_open_list, args=("needs_review",), key="dash_needs",
    )
    c2.button(
        f"Pokaż przypadki gotowe do przeglądu ({stats.by_status.get('ready_for_review', 0)})",
        on_click=_open_list, args=("ready_for_review",), key="dash_ready",
    )
    st.info(
        "Status „Gotowy do przeglądu” nie oznacza zatwierdzenia: każdy przypadek "
        "musi zostać zatwierdzony lub odrzucony przez człowieka."
    )
