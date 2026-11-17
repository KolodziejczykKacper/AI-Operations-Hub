"""Import screen: .eml upload and pasted text.

Imports happen ONLY inside the ``if st.button(...)`` branches. Streamlit re-runs this script on
every interaction, but a button is True for exactly one run (the click), so a rerun never
re-imports a file that is merely still selected in the uploader.
"""

from __future__ import annotations

import streamlit as st

from ai_ops_hub.ui.client import (
    ApiClient,
    ApiError,
    BackendUnavailable,
    InvalidResponse,
)
from ai_ops_hub.ui.common import go
from ai_ops_hub.ui.labels import STATUS_LABELS, label
from ai_ops_hub.ui.safe import esc

_ERROR_TEXT = {
    "empty_input": "Wiadomość jest pusta.",
    "malformed_message": "To nie wygląda na poprawną wiadomość e-mail (brak nagłówków).",
    "input_too_large": "Wiadomość przekracza dopuszczalny rozmiar.",
    "payload_too_large": "Wiadomość przekracza dopuszczalny rozmiar.",
    "unsupported_media_type": "Nieobsługiwany typ pliku.",
    "validation_error": "Nieprawidłowe dane wejściowe.",
}


def _feedback(kind: str, text: str, *, case_id: int | None = None, message_id: int | None = None) -> None:
    st.session_state["import_feedback"] = {
        "kind": kind, "text": text, "case_id": case_id, "message_id": message_id,
    }


def _run_import(client: ApiClient, source: str, payload: bytes | str, analyze: bool) -> bool:
    """Perform one import (+ optional analysis) and store what to tell the user.

    Returns False when the import itself failed (the input is then kept so it can be fixed)."""
    try:
        result = client.import_eml(payload) if source == "eml" else client.import_text(payload)  # type: ignore[arg-type]
    except ApiError as exc:
        _feedback("error", _ERROR_TEXT.get(exc.code, "Import nie powiódł się.") + f" ({exc.code}: {exc.message})")
        return False
    except BackendUnavailable as exc:
        extra = " Nie wiadomo, czy wiadomość została zapisana: sprawdź listę wiadomości." if exc.outcome_unknown else ""
        _feedback("error", f"{exc}{extra}")
        return False
    except InvalidResponse as exc:
        _feedback("error", str(exc))
        return False

    mid = result.message.id
    if result.decision == "conflict":
        canon = result.existing_message.id if result.existing_message else "?"
        _feedback(
            "conflict",
            f"KONFLIKT: wiadomość o tym samym Message-ID, ale innej treści niż #{canon}. "
            f"Zapisano ją osobno jako #{mid} (status „konflikt”). Oryginał #{canon} pozostał "
            "bez zmian. Konfliktu nie można analizować ani zatwierdzić.",
            message_id=mid,
        )
        return True

    if result.decision == "duplicate":
        kind, head = "duplicate", f"DUPLIKAT: identyczna wiadomość jest już zapisana jako #{mid}. Nic nie dodano."
    else:
        kind, head = "success", f"Zaimportowano nową wiadomość #{mid}."

    case_id = None
    if analyze:
        try:
            analysis = client.analyze(mid)
            case_id = analysis.case.id
            head += (
                f" Analiza regułowa (rule_based) wykonana, przypadek #{case_id} "
                f"({label(STATUS_LABELS, analysis.case.status)})."
                if analysis.analysis_created
                else f" Analiza już istniała, przypadek #{case_id}."
            )
        except (ApiError, BackendUnavailable, InvalidResponse) as exc:
            head += f" Analiza nie powiodła się: {exc}"
            kind = "error" if kind == "success" else kind
    _feedback(kind, head, case_id=case_id, message_id=mid)
    return True


def _show_feedback() -> None:
    fb = st.session_state.get("import_feedback")
    if not fb:
        return
    text = esc(fb["text"])
    {"success": st.success, "duplicate": st.info, "conflict": st.warning, "error": st.error}[fb["kind"]](text)
    if fb.get("case_id"):
        st.button(
            f"Otwórz przypadek #{fb['case_id']}", key="fb_open_case", on_click=go,
            args=("Szczegóły przypadku", fb["case_id"]),
        )
    elif fb.get("message_id") and fb["kind"] in ("success", "duplicate"):
        st.caption("Wiadomość nie została jeszcze przeanalizowana. Możesz to zrobić na ekranie **Wiadomości**.")


def render(client: ApiClient) -> None:
    st.header("Import wiadomości")
    st.caption("Wiadomość zostanie zapisana dopiero po kliknięciu przycisku importu.")
    _show_feedback()

    nonce = st.session_state.setdefault("upload_nonce", 0)
    tab_file, tab_text = st.tabs(["Plik .eml", "Wklej tekst"])

    with tab_file:
        upload = st.file_uploader("Wybierz plik .eml", type=["eml"], key=f"eml_upload_{nonce}")
        analyze_file = st.checkbox("Od razu uruchom analizę regułową", value=True, key="analyze_after_file")
        if st.button("Importuj plik", type="primary", disabled=upload is None, key="btn_import_file"):
            if _run_import(client, "eml", upload.getvalue(), analyze_file):
                st.session_state["upload_nonce"] = nonce + 1  # empty uploader: no accidental second import
            st.rerun()

    with tab_text:
        st.caption(
            "Opcjonalne nagłówki na początku (From:, Subject:, Message-ID:, Date:), "
            "pusta linia, potem treść wiadomości."
        )
        text_key = f"import_text_{nonce}"
        text = st.text_area("Treść wiadomości", height=260, key=text_key)
        analyze_text = st.checkbox("Od razu uruchom analizę regułową", value=True, key="analyze_after_text")
        if st.button("Importuj tekst", type="primary", disabled=not text.strip(), key="btn_import_text"):
            if _run_import(client, "text", text, analyze_text):
                st.session_state["upload_nonce"] = nonce + 1  # empty text box; on failure the text is kept
            st.rerun()
