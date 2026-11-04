"""Atomowy zapis i odczyt plików stanu JSON (A§8).

Zapis: plik tymczasowy w tym samym katalogu + `os.replace`, więc przerwany proces
nie zostawia uszkodzonego pliku. Format: JSON, wcięcie 2, posortowane klucze,
UTF-8, znak nowej linii na końcu — stabilne różnice w historii Git.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from pydantic import BaseModel


def dump_json(model: BaseModel) -> str:
    """Kanoniczna postać JSON modelu (tekst zakończony nową linią)."""
    payload = model.model_dump(mode="json")
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def write_bytes_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def write_model(path: Path, model: BaseModel) -> None:
    """Zapisuje model jako JSON atomowo (tworzy brakujące katalogi)."""
    write_text_atomic(path, dump_json(model))


def read_model[M: BaseModel](path: Path, model: type[M]) -> M:
    """Wczytuje i waliduje model; brak pliku → `FileNotFoundError`."""
    if not path.is_file():
        raise FileNotFoundError(path)
    return model.model_validate_json(path.read_text(encoding="utf-8"))
