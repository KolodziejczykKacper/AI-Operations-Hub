"""Small shared helpers for the screens (no business logic)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import streamlit as st

from ai_ops_hub.ui.client import ApiClient, ApiError, BackendUnavailable, ClientError, InvalidResponse
from ai_ops_hub.ui.safe import esc

PAGES = ("Panel", "Import", "Przypadki", "Szczegóły przypadku", "Wiadomości", "Eksport do Excela", "Analiza Claude")

START_BACKEND_HELP = (
    ".venv\\Scripts\\python -m ai_ops_hub.api --db data/local/demo.db"
)


def go(page: str, case_id: int | None = None) -> None:
    """Button callback: switch screen (and optionally the selected case)."""
    st.session_state["page"] = page
    if case_id is not None:
        st.session_state["selected_case"] = case_id


def show_backend_down(error: ClientError) -> None:
    st.error(f"**Backend jest niedostępny.** {esc(error)}")
    st.write("Uruchom API w osobnym oknie terminala, a potem odśwież tę stronę:")
    st.code(START_BACKEND_HELP, language="bash")
    st.button("Spróbuj ponownie", key="retry_backend")


def call(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run a READ call; on failure show a clear message and stop this screen."""
    try:
        return fn(*args, **kwargs)
    except BackendUnavailable as exc:
        show_backend_down(exc)
    except InvalidResponse as exc:
        st.error(f"Nieoczekiwana odpowiedź API: {esc(exc)}")
    except ApiError as exc:
        st.error(f"Błąd API ({esc(exc.code)}): {esc(exc.message)}")
    st.stop()


def get_client() -> ApiClient:
    client = st.session_state.get("client")
    if client is None:
        import os

        from ai_ops_hub.ui.client import DEFAULT_BASE_URL

        client = ApiClient(os.environ.get("AIOPS_API_URL", DEFAULT_BASE_URL))
        st.session_state["client"] = client
    return client


def pager(total: int, size: int, page_key: str, name: str) -> tuple[int, int]:
    """Prev / next controls for a paged list. Returns (page, pages); page is 1-based."""
    pages = max(1, -(-total // size))
    page = min(max(int(st.session_state.get(page_key, 1)), 1), pages)
    st.session_state[page_key] = page

    def step(delta: int) -> None:
        st.session_state[page_key] = min(max(st.session_state[page_key] + delta, 1), pages)

    c1, c2, c3 = st.columns([1, 2, 1])
    c1.button("← Poprzednia", key=f"{name}_prev", disabled=page <= 1, on_click=step, args=(-1,))
    c2.caption(f"Strona {page} z {pages}  ·  razem: {total}")
    c3.button("Następna →", key=f"{name}_next", disabled=page >= pages, on_click=step, args=(1,))
    return page, pages
