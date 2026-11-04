# Booking monitor

Prywatny monitor cen konkurencji z Booking.com dla jednego apartamentu.
Architektura: [ARCHITEKTURA.md](ARCHITEKTURA.md). Plan wdrożenia: [PLAN_DZIALANIA.md](PLAN_DZIALANIA.md).

> **Uwaga:** repozytorium musi być prywatne, zanim trafi tu lista konkurentów
> albo zostanie zainicjalizowana gałąź `monitor-data` (PLAN_DZIALANIA.md, B1).

## Szybki start (deweloperski)

```bash
uv sync
```

```bash
uv run pytest
```

```bash
uv run ruff check .
```
