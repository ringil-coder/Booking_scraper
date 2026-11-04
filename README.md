# Booking monitor

Prywatny monitor cen konkurencji z Booking.com dla jednego apartamentu. Co 3 dni
sprawdza wszystkie daty przyjazdu w kolejnych 3 miesiącach dla pobytów 2/3/4-nocnych
i publikuje raport HTML/CSV w GitHub Actions.

- Architektura: [ARCHITEKTURA.md](ARCHITEKTURA.md)
- Plan wdrożenia, decyzje i ryzyka: [PLAN_DZIALANIA.md](PLAN_DZIALANIA.md)

> **Repozytorium musi być prywatne**, zanim trafi tu lista konkurentów albo zostanie
> zainicjalizowana gałąź `monitor-data` (PLAN_DZIALANIA.md, B1). Workflow zbierające
> same się zatrzymają, jeśli repozytorium jest publiczne.

## Jak to działa

```text
codziennie 05:17 UTC   gate      → czy wypada nowy cykl (co 72 h od daty bazowej) albo wznowienie?
gdy trzeba             prepare   → zamrożony manifest cyklu, liczba okien roboczych
                       collect   → okna po ≤ 240 min, jedna karta Chromium, checkpoint co 15 min / 100 prób
                       finalize  → status cyklu, raport HTML + CSV + kopia SQLite (artefakt na 30 dni)
```

Źródłem prawdy jest gałąź `monitor-data` (manifesty, postęp i niezmienne partie
zdarzeń JSONL.gz). Baza SQLite jest odtwarzana z niej przy każdym uruchomieniu.

## Struktura kodu

| Moduł | Rola |
|---|---|
| `config.py` | walidacja `config/settings.yaml` i `config/competitors.csv` |
| `dates.py`, `schedule.py` | okno dat cyklu, sloty co 72 h, decyzja kontroli terminu |
| `planner.py`, `queue.py`, `runner.py` | manifest i plan zapytań, kolejka, okno robocze z checkpointami |
| `providers/`, `parsing/` | Playwright + Chromium, parser strony oferty |
| `prices.py`, `normalization.py`, `observation.py` | pełna cena pobytu, wybór taryf, interpretacja odczytu |
| `storage/`, `github_state.py` | partie zdarzeń, pliki stanu, SQLite, zapis na gałąź danych |
| `analytics.py`, `reporting/` | statystyki i raporty |
| `workflow.py`, `cli.py` | operacje gate/prepare/collect/finalize i komendy `booking-monitor` |

## Praca lokalna

```bash
uv sync
```

```bash
uv run pytest
```

```bash
uv run ruff check . && uv run ruff format --check .
```

Testy są offline: nie łączą się z Booking. Testy wymagające przeglądarki mają znacznik
`browser` i są domyślnie pomijane.

## Wdrożenie (PLAN_DZIALANIA.md, E0–E7)

1. Zmień widoczność repozytorium na **prywatną** i ustaw limit wydatków na Actions.
2. Uzupełnij `config/settings.yaml` (profil gości) i `config/competitors.csv`
   (adresy ofert `https://www.booking.com/hotel/...`). Sprawdź konfigurację:

   ```bash
   uv run booking-monitor validate-config
   ```

3. Oszacuj skalę i minuty Actions dla swojej listy:

   ```bash
   uv run booking-monitor estimate --seconds 15
   ```

4. Pilotaż techniczny (E1): workflow **Pilotaż Booking** (`spike.yml`) uruchamiany ręcznie,
   opis w `spike/README.md`. Wynik decyduje o dalszym zakresie (GO/NO-GO).
5. Inicjalizacja gałęzi danych — jednorazowo, z komputera z dostępem do repozytorium:

   ```bash
   uv run booking-monitor init-data
   ```

6. Ustaw `schedule.anchor_at_utc` (kilka minut przed godziną crona) i `schedule.enabled: true`.
   Od tej chwili codzienna kontrola rozpoczyna cykle co 72 h.

## Obsługa

- **Raport:** zakładka *Actions* → ostatnie uruchomienie „Monitor konkurencji” →
  podsumowanie oraz artefakt `raport-<cykl>-run<id>` (`report.html`, `prices.csv`,
  `manifest.json`, `monitor.sqlite3`). Raport otwiera się offline w przeglądarce.
- **Wznowienie** niedokończonego cyklu poza harmonogramem: *Run workflow* → `mode: resume`.
- **Blokada (CAPTCHA, seria błędów parsera):** kontrola kończy się błędem i GitHub wysyła
  e-mail. Po sprawdzeniu przyczyny: *Run workflow* → `mode: unblock` i zaznacz
  `confirm_unblock`. Monitor nie próbuje obchodzić blokad.
- **Zmiana listy konkurentów lub profilu** obowiązuje od następnego cyklu.
- **Lokalna kopia bazy** (np. do własnych analiz):

  ```bash
  uv run booking-monitor restore --db monitor.sqlite3
  ```
