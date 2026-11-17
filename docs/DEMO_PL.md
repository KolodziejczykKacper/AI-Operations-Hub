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

## 7. Przykład E: opcjonalna analiza Claude i porównanie (Etap 7)

Domyślnie aplikacja działa **offline** (`rule_based`) i nic nie wysyła. Analiza modelem Claude jest **wyłączona**,
płatna i uruchamiana wyłącznie jawnie. Nigdy nie dzieje się to przy imporcie, odświeżeniu strony ani zwykłej analizie.

**E1. Tryb offline (bez konfiguracji).** Ekran **Analiza Claude** pokaże, że funkcja jest wyłączona i jak ją włączyć.
Porównanie istniejących analiz działa bez klucza. Przykłady A–D działają jak dotychczas.

**E2. Włączenie (opcjonalnie).** Najpierw ustaw **limit wydatków w koncie Anthropic** (lokalne limity to tylko
szacunki). W pliku `.env` w katalogu projektu (jest ignorowany przez Git) wpisz:

```
AIOPS_CLAUDE_ENABLED=true
ANTHROPIC_API_KEY=sk-ant-...twój klucz...
```

Nigdy nie wpisuj klucza do kodu ani do czatu. Uruchom ponownie backend. Domyślny model to najtańszy `claude-haiku-5-5`.

**E3. Co jest wysyłane i ile to kosztuje.** Wysyłany jest **temat i treść jednego e-maila** (bez adresu nadawcy,
odbiorców i nagłówków) do Anthropic. Używaj wyłącznie fikcyjnych maili. Typowy mail to około 0,0003 USD (Haiku),
0,006 USD (Sonnet) lub 0,012 USD (Opus). Przed wysłaniem zobaczysz model, szacowane tokeny i **maksymalny** koszt;
po wysłaniu rzeczywisty koszt wyliczony z użycia zwróconego przez API (to nie rachunek: źródłem prawdy jest konto Anthropic).

**E4. Analiza za zgodą.** Ekran **Analiza Claude**, numer wiadomości (zaimportowanej, najlepiej już przeanalizowanej regułowo),
przeczytaj podgląd i ostrzeżenie, zaznacz „Rozumiem, że treść e-maila zostanie wysłana do Anthropic…” i kliknij
**Wyślij do Claude (płatne)**. Pole zgody jest po każdym kliknięciu puste: kolejne żądanie wymaga nowej zgody. Zgoda dotyczy dokładnie tej wiadomości, jej niezmienionej treści (odcisk), wybranego modelu i
zaakceptowanego kosztu. Zmiana któregokolwiek wymaga nowego podglądu. Powtórne wysłanie tej samej wiadomości tym samym modelem
niczego nie wysyła (pokazuje zapisaną analizę), a drugie identyczne żądanie w trakcie pierwszego jest odrzucane.

**E5. Porównanie.** Gdy istnieją obie analizy, zobaczysz tabelę: pole, wartość z `rule_based`, wartość z Claude, wynik
(zgodne / różne / tylko w jednej) i statusy. Wartości zaproponowane przez Claude, których nie potwierdza tekst e-maila
(zmyślone), są **odrzucane lokalnie** i wymienione osobno. Wyniki punktowe reguł nie są prawdopodobieństwami. Porównanie jest
tylko do odczytu: **istniejący przypadek zachowuje swoją analizę i poprawki**.

**E6. Przypadek z wybranej analizy.** Dla wiadomości **bez** przypadku możesz wybrać analizę (`rule_based` lub Claude) i utworzyć z niej
przypadek. Przypadek oparty o Claude jest wszędzie oznaczony jako `llm` (ekran szczegółów, audyt, kolumna „Metoda analizy” w Excelu),
nigdy nie jest zatwierdzany automatycznie i wymaga decyzji człowieka jak każdy inny.

**E7. Awarie.** Przekroczenie czasu, limit zapytań, błąd klucza albo niepoprawna odpowiedź kończą się komunikatem i **bez zapisanej
analizy**. Aplikacja **nie ponawia** płatnych żądań automatycznie. Nieudane żądanie widać w tabeli „Zapisane żądania do Claude”
(mogło zostać naliczone).

**E8. Jednorazowy autoryzowany test na żywo (tylko na Twoje polecenie).** Kroki są w README (Stage 7, „One authorized low-cost
live test”). Zaleca się jeden fikcyjny mail i model Haiku, a opcjonalnie benchmark na zbiorze `challenge` z `--max-cases 5`.

**E9. Ograniczenia.** Lokalny budżet 24 h rezerwuje koszt przed wysłaniem, ale **nie jest gwarantowanym limitem rachunku Anthropic**. Jeśli ten sam cytat (kwota, ilość, data) występuje w mailu kilka razy, pole jest oznaczane jako niejednoznaczne. Jakość modelu nie była sprawdzona na prawdziwym API (to dopiero Twój test). Szacunki kosztów są lokalne i
przybliżone. Brak logowania i budżetów per użytkownik. Istniejącemu przypadkowi nie można podmienić analizy (tylko porównanie).

## Ekrany w skrócie

| Ekran | Do czego służy |
|---|---|
| Panel | dokładne liczniki przypadków (z API, niezależne od stronicowania) |
| Import | plik `.eml` lub wklejony tekst; opcjonalna analiza; komunikaty: nowa / duplikat / konflikt |
| Przypadki | lista z filtrem statusu, wyszukiwaniem (temat lub nadawca) i stronicowaniem |
| Szczegóły przypadku | oryginał, analiza, poprawki, decyzje, audyt |
| Wiadomości | wszystkie zaimportowane wiadomości; analiza tych bez przypadku |
| Eksport do Excela | plik .xlsx z zatwierdzonymi przypadkami (tylko odczyt, niczego nie zmienia) |
| Analiza Claude | opcjonalna, płatna analiza modelem Claude, zgoda przy każdym żądaniu, porównanie z rule_based |
