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

## Uruchomienie lokalne (tylko do uruchomienia kodu, nie jako wynik pilotażu)

```bash
BOOKING_MONITOR_CHROMIUM=/ścieżka/do/chrome uv run python spike/probe.py --competitor all --sample 4 --nights 2 3 4 --out spike/output
```

Wynik lokalny nie potwierdza działania na runnerze (inny adres IP, A§10).

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
