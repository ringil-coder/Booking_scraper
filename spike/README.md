# Pilotaż E1 — odczyty próbne z Booking.com

Jednorazowe narzędzie do sprawdzenia wykonalności (PLAN_DZIALANIA.md, etap E1,
bramka GO/NO-GO). Nie jest częścią cyklu produkcyjnego i nie działa w CI.

## Warunki wstępne

1. Repozytorium jest **publiczne** (D1), więc artefakt pilotażu jest jawny. HTML stron
   zapisujemy bez identyfikatora sesji i tokenów (`redaction.scrub_html`); zrzuty ekranu
   pokazują stronę tak, jak widzi ją każdy niezalogowany gość.
2. Podjęta decyzja D9 (ryzyko regulaminowe).
3. `config/competitors.csv` zawiera 2–3 prawdziwe adresy ofert (bez placeholderów),
   a `config/settings.yaml` — właściwy profil gości (D3).

Narzędzie **nie obchodzi zabezpieczeń**. Przy pierwszej blokadzie lub CAPTCHA kończy
serię i zwraca kod wyjścia 3 — to wynik pilotażu, nie błąd do obejścia.

## Uruchomienie na runnerze GitHub (docelowe)

Actions → „Pilotaż (E1)” → Run workflow. Parametry:

- `competitors` — klucze oddzielone spacją albo `all`,
- `sample` — liczba dat przyjazdu rozłożonych równomiernie na horyzoncie (np. 4),
- `nights` — długości pobytu (np. `2 3 4`).

Domyślnie 4 daty × 3 długości × 2–3 oferty = 24–36 odczytów, czyli przy odstępie 10 s
kilka–kilkanaście minut. Wynik: artefakt `pilotaz-<run_id>` (7 dni).

## Uruchomienie na własnym komputerze (pilotaż po NO-GO runnera)

Na runnerze GitHub (04.11.2026) Booking przekierowywał każdy adres z datami na stronę
bez dat (PLAN_DZIALANIA.md, E1). Ten test sprawdza, czy z domowego łącza jest inaczej.
Dwa krótkie przebiegi po 4 odczyty (2 oferty × 2 daty, pobyt 2-nocny), każdy około minuty.

Wymagania: [uv](https://docs.astral.sh/uv/) i Git (albo pobrany ZIP gałęzi).

Instalacja uv — Windows (PowerShell):

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Instalacja uv — macOS / Linux:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Kod i przeglądarka (te same polecenia na każdym systemie; uv sam pobierze Pythona 3.12):

```bash
git clone -b claude/relaxed-knuth-y8utuf https://github.com/ringil-coder/Booking_scraper.git
```

```bash
cd Booking_scraper
```

```bash
uv sync
```

```bash
uv run playwright install chromium
```

Przebieg 1 — bez okna, jak na serwerze:

```bash
uv run python spike/probe.py --competitor oxygen_residence --competitor smart_apartment_ogrodowa --sample 2 --nights 2 --out spike/output/bez-okna
```

Przebieg 2 — z widocznym oknem przeglądarki (nie klikaj w nie w trakcie):

```bash
uv run python spike/probe.py --competitor oxygen_residence --competitor smart_apartment_ogrodowa --sample 2 --nights 2 --headed --out spike/output/z-oknem
```

Każda linia wyniku mówi, czy strona zachowała daty, np.
`[1/4] oxygen_residence 2026-11-06 2n → available (9.8 s) | daty na stronie: tak | waluta: PLN | ofert: 3, od 812.00 PLN`
albo `… → parse_error … | daty na stronie: NIE | …`. Zrzuty stron są w `spike/output/*/pages/`.

Kod CLI nie zależy od Actions (PLAN_DZIALANIA.md, P1), więc przy wyniku pozytywnym ten sam
monitor można uruchamiać z harmonogramu systemu (Harmonogram zadań Windows, cron, launchd).

## Zawartość wyniku

| Plik | Opis |
|---|---|
| `observations.jsonl` | Jedna linia na odczyt: czasy, status HTTP, wynik parsera, kontekst ze strony, niezgodności, oferty |
| `pages/*.html`, `pages/*.png` | Wyrenderowany HTML i zrzut każdej strony |
| `diagnostics/` | HTML i zrzut przy błędach technicznych |
| `summary.json` | Odsetek sukcesów, blokady, mediana i p95 czasu, szacunek minut na cykl i na 30 dni |
| `manual_check.csv` | Szablon ręcznej kontroli cen (UTF-8 z BOM, separator `;`) |

## Ręczna kontrola ~20 cen

1. Otwórz `manual_check.csv` w arkuszu.
2. Dla około 20 wierszy otwórz tę samą ofertę w swojej przeglądarce z tymi samymi
   datami, liczbą gości i walutą (adres jest w `observations.jsonl`, pole `url`).
3. Wpisz `cena_wlasciciela` (pełna kwota pobytu) i `warunki_wlasciciela`
   (np. „bezzwrotna, ze śniadaniem”).
4. W `zgodne_tak_nie` wpisz `tak` albo `nie`; w `uwagi` — przyczynę różnicy
   (np. podatek niewliczony, inna taryfa, inny lokal, inna waluta).

## Ocena GO/NO-GO (PLAN_DZIALANIA.md, E1)

- brak CAPTCHA i blokad w serii (`blocked = 0`, `stopped_by_block = false`);
- co najmniej 95% odczytów bez błędu technicznego (`success_rate`);
- ceny zgodne z kontrolą ręczną lub różnice wyjaśnione;
- `estimate.hours_per_cycle_p95` ≤ 48 (`cycle_fits_48h_p95 = true`);
- `estimate.minutes_per_30_days_p95` — tylko informacyjnie: w repozytorium publicznym
  standardowe runnery są bezpłatne (PLAN_DZIALANIA.md, B2).

Zanonimizowane próbki HTML (bez cookies i identyfikatorów sesji) z pilotażu należy
dodać do `tests/fixtures/booking/` i dopasować `src/booking_monitor/parsing/selectors.py`.
