"""Streamlit application shell: page setup, sidebar navigation, backend health, routing."""

from __future__ import annotations

import streamlit as st

from ai_ops_hub.ui.client import ApiClient, BackendUnavailable, ClientError
from ai_ops_hub.ui.common import PAGES, get_client, show_backend_down
from ai_ops_hub.ui.safe import esc
from ai_ops_hub.ui.views import cases, dashboard, detail, import_page, messages

# Widget state is dropped by Streamlit when a widget is not drawn in a run (e.g. while another
# screen is open). Re-assigning these keys keeps filters and unsaved drafts across navigation.
_KEEP_PREFIXES = ("edit_", "f_", "m_", "confirm_", "detail_")

_SCREENS = {
    "Panel": dashboard.render,
    "Import": import_page.render,
    "Przypadki": cases.render,
    "Szczegóły przypadku": detail.render,
    "Wiadomości": messages.render,
}


def _keep_widget_state() -> None:
    ss = st.session_state
    for key in [k for k in ss.keys() if isinstance(k, str) and k.startswith(_KEEP_PREFIXES)]:
        ss[key] = ss[key]


def _sidebar(client: ApiClient) -> bool:
    """Navigation + backend status. Returns False when the backend can not be used."""
    with st.sidebar:
        st.title("AI Operations Hub")
        st.radio("Nawigacja", PAGES, key="page")
        st.divider()
        try:
            client.health()
        except BackendUnavailable as exc:
            st.error("Backend: niedostępny")
            st.session_state["_backend_error"] = exc
            ok = False
        except ClientError as exc:
            st.error(f"Backend: błąd ({esc(exc)})")
            ok = False
        else:
            st.success("Backend: działa")
            st.session_state.pop("_backend_error", None)
            ok = True
        st.caption(f"API: {esc(client.base_url)}")
        st.caption(
            "Analiza wiadomości jest **regułowa** (rule_based): dopasowanie wzorców, nie model AI. "
            "Decyzje zawsze podejmuje człowiek."
        )
        st.caption("Brak logowania: zmiany są podpisane etykietą local_demo_user. Używaj tylko lokalnie.")
    return ok


def render() -> None:
    st.set_page_config(page_title="AI Operations Hub", page_icon="📨", layout="wide")
    st.session_state.setdefault("page", PAGES[0])
    _keep_widget_state()

    try:
        client = get_client()
    except ValueError as exc:  # e.g. AIOPS_API_URL points to a non-local host
        st.error(f"Nieprawidłowy adres API: {esc(exc)}")
        st.stop()

    if not _sidebar(client):
        error = st.session_state.get("_backend_error")
        if error is not None:
            show_backend_down(error)
        st.stop()

    _SCREENS[st.session_state["page"]](client)
