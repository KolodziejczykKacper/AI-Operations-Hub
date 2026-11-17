# Demo: od maila do zatwierdzonego przypadku

Wszystkie dane są fikcyjne. Aplikacja nie ma logowania: każda zmiana jest podpisana stałą
etykietą `local_demo_user` (to etykieta, a nie dowód tożsamości). Analiza jest **regułowa**
(dopasowywanie wzorców, `rule_based`), a **nie** sztuczną inteligencją, i niczego nie
zatwierdza ani nie wysyła. Zatwierdza wyłącznie człowiek, jednym jawnym kliknięciem.

Aplikacja składa się z dwóch procesów: **backend** (FastAPI, dane i reguły) oraz **interfejs**
(Streamlit), który rozmawia z backendem wyłącznie przez HTTP.

## 0. Instalacja (raz)

W katalogu projektu (PowerShell lub cmd):

```
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
```

## 1. Uruchomienie: dwa okna terminala

**Okno 1: backend** (zostaw uruchomiony):

```
.venv\Scripts\python -m ai_ops_hub.api --db data/local/demo.db
```

Powinieneś zobaczyć `Uvicorn running on http://127.0.0.1:8000`.
(Swagger UI nadal działa pod http://127.0.0.1:8000/docs, ale do demo nie jest potrzebny.)

**Okno 2: interfejs:**

```
.venv\Scripts\python -m ai_ops_hub.ui
```

Przeglądarka otworzy się pod **http://127.0.0.1:8501** (jeśli nie, wpisz ten adres ręcznie).
W lewym panelu musi być zielone **„Backend: działa”**. Jeśli widzisz czerwone „niedostępny”,
uruchom najpierw okno 1 i odśwież stronę. Zatrzymanie: `Ctrl+C` w obu oknach.

Inny adres backendu: `python -m ai_ops_hub.ui --api-url http://127.0.0.1:8123` (tylko adresy
lokalne są akceptowane).

## 2. Przykład A: import, analiza, poprawka, zatwierdzenie

**A1. Import.** Ekran **Import**, zakładka **Wklej tekst**. Wklej:

```
From: Jan Kowalski <jan.kowalski@zielonadolina.example>
Subject: Zamówienie pomp
Message-ID: <demo-order-1@zielonadolina.example>

Dzień dobry,

chcielibyśmy zamówić 10 szt. Pompa Alfa. Prosimy o dostawę do 15/12.

Z poważaniem,
Jan Kowalski
Zielona Dolina Sp. z o.o.
```

Zostaw zaznaczone „Od razu uruchom analizę regułową” i kliknij **Importuj tekst**.
Zobaczysz zielony komunikat o nowej wiadomości #1 i przypadku #1. Kliknij **Otwórz przypadek #1**.
(Ten sam tekst wklejony drugi raz da niebieski komunikat „DUPLIKAT” i nic nie doda.)

**A2. Przegląd wyników.** Na ekranie szczegółów widzisz u góry status (🟠 Do poprawy), wersję (1),
kategorię (Zamówienie) i metodę analizy `rule_based`. Pomarańczowe ostrzeżenie wymienia, co blokuje
zatwierdzenie: brak produktu i niejednoznaczny termin. Zakładki:

* **1 · Oryginalny mail** – oryginalna treść, tylko do odczytu, jako zwykły tekst.
* **2 · Analiza i poprawki** – tabela: *wartość z analizy (rule_based)*, *poprawka człowieka*,
  *status* i *fragment źródłowy* w kontekście (`[[15/12]]`). Niżej formularz poprawek.
* **3 · Historia i audyt** – poprawki, decyzje i pełny dziennik zdarzeń.

**A3. Próba zatwierdzenia przed poprawką.** Na dole kliknij **Zatwierdź…**. Panel potwierdzenia
pokaże ostrzeżenie i zażąda pisemnego uzasadnienia oraz zaznaczenia zgody. Nic nie zostało
wysłane. Kliknij **Anuluj** (zatwierdzanie „mimo ostrzeżeń” jest możliwe, ale tylko świadomie).

**A4. Poprawka.** W zakładce **2 · Analiza i poprawki**, w formularzu wpisz:

* **Produkt:** `Pompa Alfa`
* **Termin (RRRR-MM-DD):** `2026-12-15`
* **Notatka przeglądu:** `Termin potwierdzony telefonicznie.`

i kliknij **Zapisz poprawki**. Zobaczysz komunikat „Zapisano poprawki”, wersję 2 i status
🔵 Gotowy do przeglądu. W tabeli kolumna *wartość z analizy* nadal pokazuje `--12-15`, a obok
*poprawka człowieka* `2026-12-15`. Oryginalny mail się nie zmienił. Wpisanie błędnej wartości
(np. `15 grudnia`) pokaże komunikat serwera, a Twój tekst zostanie w polu.

**A5. Zatwierdzenie.** Kliknij **Zatwierdź…**. Panel pokazuje dokładnie to, co zatwierdzasz.
Opcjonalnie dopisz notatkę i kliknij **Tak, zatwierdzam**. Status: 🟢 Zatwierdzony, wersja 3.
Nic nie jest wysyłane ani wykonywane na zewnątrz.

**A6. Audyt.** Zakładka **3 · Historia i audyt**: import, analiza, utworzenie przypadku,
poprawki człowieka, zmiana statusu (automatyczna, aktor „system”), notatka i `ZATWIERDZONY`
(aktor „human”, etykieta `local_demo_user`).

## 3. Przykład B: odrzucenie i ponowne otwarcie

1. **Import** → wklej drugi, inny mail (zmień `Message-ID` i treść), z analizą.
2. W szczegółach kliknij **Odrzuć…**, wpisz powód (wymagany) i **Tak, odrzucam**.
   Status: 🔴 Odrzucony.
3. Kliknij **Otwórz ponownie…**, wpisz powód i potwierdź. Przypadek wraca do stanu otwartego
   i znów można go edytować. Poprzednia decyzja zostaje w historii.

## 4. Przykład C: konflikt i duplikat (pliki .eml)

Ekran **Import → Plik .eml**: wybierz `tests/data/samples/01_invoice_dispute_pl.eml` i kliknij
**Importuj plik**, potem ten sam plik jeszcze raz (**DUPLIKAT**), a na końcu
`02_invoice_dispute_pl_conflict.eml` (to samo Message-ID, inna treść): żółty komunikat
**KONFLIKT**. Wiadomość zostaje zapisana osobno, ale nie można jej analizować ani zatwierdzić
(ekran **Wiadomości** pokazuje ją jako „KONFLIKT”).

## 5. Dwóch recenzentów naraz (konflikt wersji)

Otwórz ten sam przypadek w dwóch kartach przeglądarki. W pierwszej zapisz poprawkę. W drugiej, bez
odświeżania, spróbuj zapisać inną: aplikacja nie nadpisze nowszych danych, pokaże ostrzeżenie
o zmianie wersji, wczyta aktualny stan i **zachowa Twoje niezapisane zmiany** w formularzu,
żebyś mógł je sprawdzić i zapisać ponownie.

## 6. Przykład D: eksport zatwierdzonych przypadków do Excela

Eksport zawiera **wyłącznie przypadki zatwierdzone** i dane **zatwierdzone przez człowieka** (poprawki
mają pierwszeństwo przed wynikiem analizy regułowej). Niczego nie wysyła i niczego nie zmienia.

**D1. Przygotuj dane.** Zaimportuj kilka fikcyjnych maili (Import → Wklej tekst, z analizą), np. mail z
przykładu A, jego odmiany z innym `Message-ID`, oraz fakturę z terminem „do 20 listopada 2026”.
Następnie na ekranie szczegółów:

* jeden przypadek **zatwierdź** (np. gotowy, bez problemów),
* jeden **popraw** (produkt `Pompa Alfa`, termin `2026-12-15`, notatka) i zatwierdź,
* jeden **zatwierdź mimo ostrzeżeń** (z pisemnym uzasadnieniem, min. 10 znaków),
* jeden **odrzuć**, a jeden zostaw otwarty,
* jeden zatwierdzony **otwórz ponownie** (zniknie z eksportu do czasu ponownego zatwierdzenia).

**D2. Eksport.** Ekran **Eksport do Excela**. Zobaczysz liczbę przypadków gotowych do eksportu
(tylko zatwierdzone). Opcjonalnie zaznacz „Ogranicz do zakresu dat zatwierdzenia”. Kliknij
**Wygeneruj plik Excel**, a potem **Pobierz plik Excel (.xlsx)**. Komunikat „Plik jest gotowy” oznacza,
że serwer wygenerował plik; aplikacja nie twierdzi, że został zapisany na dysku. Odświeżenie strony
ani kliknięcie pobrania nie generuje pliku ponownie.

**D3. Jak czytać plik.**

* Arkusz **Zatwierdzone przypadki**: jeden wiersz na przypadek, stała kolejność kolumn (ID, kategoria,
  nadawca, temat, daty importu i zatwierdzenia w UTC, produkt, ilość, jednostka, kwota, waluta, termin,
  żądana data, numer referencyjny, kontakt, e-mail, telefon, notatki, override, poprawki człowieka, wersja).
  Brak wartości to **pusta komórka**: nic nie jest zgadywane.
* Wartość z `[niepotwierdzone]` i żółtym tłem była niejednoznaczna w analizie i nie została potwierdzona
  przez człowieka (np. termin `15/12` bez roku). Nie traktuj jej jako faktu. Takie pola są też wymienione
  w kolumnie „Pola do weryfikacji”.
* **Zatwierdzono mimo problemów = TAK**: kolumny obok pokazują uzasadnienie i zastąpione problemy.
* Kolumna „Poprawki człowieka” pokazuje: `Termin realizacji: analiza „--12-15” → człowiek „2026-12-15”`.
* Arkusz **Informacje**: kiedy wygenerowano plik, zakres, liczby, legenda; analiza opisana jako regułowa
  (nie AI).

**D4. Bezpieczeństwo.** Tekst z maila zaczynający się od `=`, `+`, `-` lub `@` (np. temat
`=HYPERLINK("http://...")`) jest zapisany jako zwykły tekst z apostrofem na początku i nigdy nie
zostanie uruchomiony jako formuła. W pliku nie ma hiperłączy ani makr.

**D5. Ograniczenia.** Jeden plik to maksymalnie 500 przypadków (powyżej: komunikat z prośbą o zawężenie
zakresu dat). Plik jest kopią z chwili wygenerowania: późniejsze zmiany w aplikacji wymagają ponownego
wygenerowania. Eksport nie zapisuje zdarzenia w dzienniku audytu (serwer nie wie, czy plik został
pobrany). Brak logowania: eksportować może każdy, kto otworzy stronę.

## Ekrany w skrócie

| Ekran | Do czego służy |
|---|---|
| Panel | dokładne liczniki przypadków (z API, niezależne od stronicowania) |
| Import | plik `.eml` lub wklejony tekst; opcjonalna analiza; komunikaty: nowa / duplikat / konflikt |
| Przypadki | lista z filtrem statusu, wyszukiwaniem (temat lub nadawca) i stronicowaniem |
| Szczegóły przypadku | oryginał, analiza, poprawki, decyzje, audyt |
| Wiadomości | wszystkie zaimportowane wiadomości; analiza tych bez przypadku |
| Eksport do Excela | plik .xlsx z zatwierdzonymi przypadkami (tylko odczyt, niczego nie zmienia) |
