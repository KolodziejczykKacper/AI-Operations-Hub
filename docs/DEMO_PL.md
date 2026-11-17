# Demo: od maila do zatwierdzonego przypadku (Swagger UI)

Wszystkie dane są fikcyjne. API nie ma uwierzytelniania, więc każda zmiana jest podpisana stałą
etykietą `local_demo_user` (to etykieta, a nie dowód tożsamości). Analiza jest **regułowa**
(dopasowywanie wzorców), a **nie** sztuczną inteligencją, i niczego nie zatwierdza ani nie wysyła.

## 0. Start

W katalogu projektu (PowerShell lub cmd):

```
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\python -m ai_ops_hub.api --db data/local/demo.db
```

Otwórz w przeglądarce **http://127.0.0.1:8000/docs**. Serwer nasłuchuje tylko na `127.0.0.1`.
Zatrzymanie: `Ctrl+C`. Baza `data/local/demo.db` jest ignorowana przez Git.

W Swagger UI każdy endpoint rozwijasz kliknięciem, potem **Try it out**, wpisujesz dane i **Execute**.

## A. Import fikcyjnego maila

Endpoint **POST /messages/text**, w polu *Request body* wklej:

```json
{
  "text": "From: Jan Kowalski <jan.kowalski@zielonadolina.example>\nSubject: Zamówienie pomp\nMessage-ID: <demo-order-1@zielonadolina.example>\n\nDzień dobry,\n\nchcielibyśmy zamówić 10 szt. Pompa Alfa. Prosimy o dostawę do 15/12.\n\nZ poważaniem,\nJan Kowalski\nZielona Dolina Sp. z o.o."
}
```

Odpowiedź: **201**, `"decision": "new"`, `message.id` = 1, `import_status` = `imported`.
Powtórzenie tego samego żądania da **200** (`duplicate`): nic nie zostanie zapisane drugi raz.

Plik `.eml` zaimportujesz przez **POST /messages/eml**: w Swagger UI wybierz plik (np.
`tests/data/samples/01_invoice_dispute_pl.eml`) w polu *Request body*.

## B. Analiza

**POST /messages/{message_id}/analyze**, `message_id` = `1`.

Odpowiedź **201**: `analysis.analysis_method` = `rule_based`, `analysis.category` = `order`,
a w `case` powstał przypadek (`case.id` = 1) ze statusem `needs_review` i `version` = 1.
Powtórne wywołanie zwraca **200** z tą samą analizą (operacja jest idempotentna).

## C. Kategoria, pola i ostrzeżenia

* **GET /cases/{case_id}** (`1`): oryginalny mail (`message.body_text`), kategoria, pola z
  fragmentem źródłowym (`values[].span.text`), status pola (`valid`/`ambiguous`/`invalid`)
  oraz `problems`, czyli to, co blokuje zatwierdzenie. Tu:
  * brak wymaganego pola `product` (analizator nie wyciągnął produktu z „10 szt. Pompa Alfa"),
  * `deadline` = `--12-15` ze statusem `ambiguous` (z „15/12" nie wynika rok).
* **GET /messages/{message_id}/analysis** (`1`): najnowsza analiza razem z ostrzeżeniami (`result.warnings`).

## D. Poprawka błędnych danych

Spróbuj najpierw zatwierdzić: **POST /cases/{case_id}/approve** z ciałem
`{"expected_version": 1}`. Dostaniesz **422** `approval_blocked` z listą problemów.
Przypadek się nie zmienił.

Popraw dane: **PATCH /cases/{case_id}**:

```json
{
  "expected_version": 1,
  "corrections": {
    "deadline": ["2026-12-15"],
    "product": ["Pompa Alfa"]
  },
  "note": "Termin potwierdzony telefonicznie."
}
```

Odpowiedź **200**: `case.version` = 2, `case.status` = `ready_for_review`, `problems` puste.
Oryginalny mail i oryginalna analiza **nie zostały zmienione**. Przy każdym polu widać
zarówno wartość człowieka (`origin: human`), jak i to, co powiedział analizator
(`analyzer_values`). Historię poprawek zobaczysz w **GET /cases/{case_id}/history**.

`expected_version` to wersja, którą widziałeś. Jeśli ktoś zmienił przypadek w międzyczasie,
dostaniesz **409** `version_conflict` i nic nie zostanie nadpisane. Wtedy wczytaj przypadek
ponownie i powtórz zmianę.

## E. Zatwierdzenie

**POST /cases/{case_id}/approve**:

```json
{ "expected_version": 2, "note": "Zgodne z zamówieniem w ERP." }
```

Odpowiedź **200**: `case.status` = `approved`, `decided_at` ustawione. Nic nie jest wysyłane
ani wykonywane. Jeśli zatwierdzisz drugi raz, dostaniesz **409**.

Gdyby dane nadal miały problemy, a mimo to chciałbyś zatwierdzić, trzeba podać
`"override_reason"` (min. 10 znaków). Powód trafia do decyzji i do audytu.

Odrzucenie: **POST /cases/{case_id}/reject** z `{"expected_version": N, "reason": "..."}`.
Ponowne otwarcie zamkniętego przypadku: **POST /cases/{case_id}/reopen** z tym samym ciałem.

## F. Historia audytu

**GET /cases/{case_id}/audit** zwraca kolejno:

| zdarzenie | kto |
|---|---|
| `message_imported` | system |
| `analysis_completed` | system |
| `case_created` | system |
| `case_updated` (z listą zmian: przed i po) | human (`local_demo_user`) |
| `status_changed` (needs_review do ready_for_review, automatyczny triage) | system |
| `case_note_added` | human (`local_demo_user`) |
| `case_approved` (z dokładnym zestawem zatwierdzonych danych) | human (`local_demo_user`) |

## G. Konflikt (opcjonalnie)

Zaimportuj `01_invoice_dispute_pl.eml`, a potem `02_invoice_dispute_pl_conflict.eml` (to samo
Message-ID, inna treść). Drugi import zwróci **409** `conflict`: wiadomość jest zapisana, ale
**POST /messages/{id}/analyze** dla niej da **409** `conflict_not_analyzable`. Konflikt nie
dostaje przypadku i nie może zostać zatwierdzony.
