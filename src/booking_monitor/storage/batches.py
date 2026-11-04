"""Partie zdarzeń JSONL.gz (A§8).

Partia jest niezmienna: zapisujemy ją raz, deterministycznie (gzip bez znacznika
czasu i nazwy pliku), a sumę SHA-256 liczymy z nieskompresowanej treści JSONL.
Suma i liczba zdarzeń trafiają do `progress.json` w tym samym commicie co plik.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import secrets
import zlib
from collections.abc import Iterable
from datetime import UTC, datetime

from pydantic import ValidationError

from booking_monitor.storage.events import (
    AttemptEvent,
    JobExpiredEvent,
    RunContext,
    event_from_json,
    event_to_json,
)
from booking_monitor.storage.files import write_bytes_atomic
from booking_monitor.storage.layout import DataLayout
from booking_monitor.storage.state import BatchRef

AnyEvent = AttemptEvent | JobExpiredEvent


class BatchIntegrityError(Exception):
    """Partia uszkodzona, niezgodna z sumą kontrolną albo sprzeczna z wcześniejszym zapisem."""


def new_batch_id(now: datetime) -> str:
    """Identyfikator partii, np. `b20261104T051700Z-1a2b3c4d`."""
    if now.tzinfo is None:
        raise ValueError("oczekiwano czasu ze strefą (UTC)")
    return f"b{now.astimezone(UTC):%Y%m%dT%H%M%SZ}-{secrets.token_hex(4)}"


def serialize_events(events: Iterable[AnyEvent]) -> bytes:
    """JSONL: jedno zdarzenie na linię, każda linia zakończona `\\n`."""
    return "".join(event_to_json(event) + "\n" for event in events).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _compress(data: bytes) -> bytes:
    buffer = io.BytesIO()
    # mtime=0 i pusta nazwa pliku w nagłówku → identyczne bajty dla tej samej treści.
    with gzip.GzipFile(filename="", mode="wb", fileobj=buffer, mtime=0, compresslevel=9) as gz:
        gz.write(data)
    return buffer.getvalue()


def _decompress(data: bytes, where: str) -> bytes:
    try:
        return gzip.decompress(data)
    except (OSError, EOFError, zlib.error) as exc:
        raise BatchIntegrityError(f"{where}: uszkodzony plik gzip ({exc})") from exc


def write_batch(
    layout: DataLayout,
    cycle_id: str,
    batch_id: str,
    events: Iterable[AnyEvent],
    *,
    created_at: datetime,
    run: RunContext,
) -> BatchRef:
    """Zapisuje partię i zwraca odwołanie do `progress.json`.

    Istniejący plik o tej samej treści jest akceptowany (ponowienie zapisu),
    a o innej treści — odrzucany, bo partia po zapisie jest niezmienna.
    """
    items = list(events)
    raw = serialize_events(items)
    digest = sha256_hex(raw)
    path = layout.batch_file(cycle_id, batch_id)
    if path.exists():
        existing = _decompress(path.read_bytes(), str(path))
        if sha256_hex(existing) != digest:
            raise BatchIntegrityError(f"partia {batch_id} już istnieje z inną treścią")
    else:
        write_bytes_atomic(path, _compress(raw))
    return BatchRef(
        batch_id=batch_id,
        path=DataLayout.batch_relpath(batch_id),
        sha256=digest,
        events=len(items),
        created_at=created_at,
        run=run,
    )


def read_batch(layout: DataLayout, cycle_id: str, ref: BatchRef) -> list[AnyEvent]:
    """Wczytuje partię i weryfikuje sumę SHA-256 oraz liczbę zdarzeń."""
    path = layout.cycle_dir(cycle_id) / ref.path
    if not path.is_file():
        raise BatchIntegrityError(f"brak pliku partii {ref.batch_id}: {path}")
    raw = _decompress(path.read_bytes(), str(path))
    if sha256_hex(raw) != ref.sha256:
        raise BatchIntegrityError(
            f"partia {ref.batch_id}: suma kontrolna niezgodna z progress.json"
        )
    lines = raw.decode("utf-8").splitlines()
    try:
        events = [event_from_json(line) for line in lines if line]
    except ValidationError as exc:
        raise BatchIntegrityError(
            f"partia {ref.batch_id}: nieprawidłowe zdarzenie ({exc})"
        ) from exc
    if len(events) != ref.events:
        raise BatchIntegrityError(
            f"partia {ref.batch_id}: {len(events)} zdarzeń, oczekiwano {ref.events}"
        )
    for event in events:
        if event.cycle_id != cycle_id:
            raise BatchIntegrityError(
                f"partia {ref.batch_id}: zdarzenie cyklu {event.cycle_id} w katalogu {cycle_id}"
            )
    return events
