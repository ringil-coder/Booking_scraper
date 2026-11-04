"""Schemat zdarzeń zapisywanych w partiach JSONL.gz na gałęzi `monitor-data` (A§8).

Partia to niezmienny plik: jedna linia = jedno zdarzenie (JSON). Stan zadań cyklu
odtwarzamy wyłącznie z manifestu i zdarzeń — SQLite jest tylko indeksem roboczym.
"""

from __future__ import annotations

import os
import platform
from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, TypeAdapter

from booking_monitor.models import (
    Availability,
    FrozenModel,
    JobStatus,
    Offer,
    StayQuery,
    TechnicalReason,
)

EVENT_SCHEMA_VERSION = 1


class RunContext(FrozenModel):
    """Pochodzenie zapisu: workflow run, próba workflow, wersja kodu i środowisko."""

    workflow_run_id: str | None = None
    workflow_run_attempt: int | None = None
    workflow_job: str | None = None
    code_sha: str | None = None
    runner_environment: str | None = None

    @classmethod
    def from_environment(cls, code_sha: str | None = None) -> RunContext:
        attempt = os.environ.get("GITHUB_RUN_ATTEMPT")
        runner = os.environ.get("RUNNER_ENVIRONMENT")  # github-hosted / self-hosted
        image = os.environ.get("ImageOS") or os.environ.get("RUNNER_OS")  # noqa: SIM112
        environment = ":".join(p for p in (runner, image) if p) or f"local:{platform.node()}"
        return cls(
            workflow_run_id=os.environ.get("GITHUB_RUN_ID"),
            workflow_run_attempt=int(attempt) if attempt else None,
            workflow_job=os.environ.get("GITHUB_JOB"),
            code_sha=code_sha or os.environ.get("GITHUB_SHA"),
            runner_environment=environment,
        )


class ObservationRecord(FrozenModel):
    """Zinterpretowana strona: dostępność i oferty. Najwyżej jedna na próbę (A§9)."""

    observation_id: str
    observed_at: datetime
    availability: Availability
    parser_version: str
    source_url: str | None = None
    message: str | None = None
    offers: tuple[Offer, ...] = ()


class AttemptEvent(FrozenModel):
    """Zakończona próba zadania wraz z wynikiem i nowym stanem zadania."""

    type: Literal["attempt"] = "attempt"
    schema_version: int = EVENT_SCHEMA_VERSION
    attempt_id: str
    cycle_id: str
    job_id: str
    query: StayQuery
    attempt_no: int = Field(ge=1)
    started_at: datetime
    finished_at: datetime
    run: RunContext
    technical_reason: TechnicalReason | None = None
    message: str | None = None
    job_status_after: Literal[JobStatus.COMPLETED, JobStatus.RETRY_WAIT, JobStatus.FAILED]
    next_attempt_at: datetime | None = None
    observation: ObservationRecord | None = None


class JobExpiredEvent(FrozenModel):
    """Zadanie, którego data przyjazdu minęła przed wykonaniem (A§2)."""

    type: Literal["job_expired"] = "job_expired"
    schema_version: int = EVENT_SCHEMA_VERSION
    cycle_id: str
    job_id: str
    query: StayQuery
    at: datetime
    run: RunContext
    reason: str = "checkin_in_past"


Event = Annotated[AttemptEvent | JobExpiredEvent, Field(discriminator="type")]
EVENT_ADAPTER: TypeAdapter[AttemptEvent | JobExpiredEvent] = TypeAdapter(Event)


def event_to_json(event: AttemptEvent | JobExpiredEvent) -> str:
    """Deterministyczna serializacja jednej linii JSONL."""
    return event.model_dump_json(exclude_none=False)


def event_from_json(line: str) -> AttemptEvent | JobExpiredEvent:
    return EVENT_ADAPTER.validate_json(line)
