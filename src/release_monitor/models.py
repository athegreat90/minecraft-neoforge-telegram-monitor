"""Domain types shared by the sources, the outbox and the storage layer."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class Source(StrEnum):
    MINECRAFT = "minecraft"
    NEOFORGE = "neoforge"


class ReleaseKind(StrEnum):
    STABLE = "stable"
    BETA = "beta"
    ALPHA = "alpha"
    SNAPSHOT = "snapshot"
    PRERELEASE = "prerelease"
    RELEASE_CANDIDATE = "release_candidate"


class PublishedAtKind(StrEnum):
    """What a release timestamp means, so messages never overstate it."""

    OFFICIAL = "official"
    ARTIFACT = "artifact"
    DETECTED = "detected"


class ReleaseStatus(StrEnum):
    BASELINED = "baselined"
    FILTERED = "filtered"
    DEFERRED = "deferred"
    QUEUED = "queued"
    ANNOUNCED = "announced"


class NotificationState(StrEnum):
    PENDING = "pending"
    SENDING = "sending"
    SENT = "sent"
    FAILED_PERMANENT = "failed_permanent"


class Origin(StrEnum):
    LIVE = "live"
    BACKFILL = "backfill"
    LEGACY = "legacy"


@dataclass(frozen=True, slots=True)
class Release:
    """A version published by an upstream source.

    ``extra`` carries source-specific hints between pipeline stages and is not persisted.
    """

    source: Source
    version: str
    kind: ReleaseKind
    published_at: datetime | None = None
    published_at_kind: PublishedAtKind = PublishedAtKind.DETECTED
    mc_version: str | None = None
    url: str | None = None
    extra: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.published_at is not None and self.published_at.tzinfo is None:
            raise ValueError("published_at must be timezone-aware")


@dataclass(frozen=True, slots=True)
class StoredRelease:
    """A release as persisted, with its lifecycle status."""

    release: Release
    status: ReleaseStatus
    first_seen_at: datetime
    discovered_at: datetime


@dataclass(frozen=True, slots=True)
class Notification:
    """One outbox row: the announcement of a single release."""

    id: int
    source: Source
    version: str
    state: NotificationState
    payload: str
    attempts: int
    attempt_id: str | None
    next_attempt_at: datetime
    last_error_class: str | None
    telegram_message_id: int | None
    origin: Origin
    created_at: datetime
    updated_at: datetime
    sent_at: datetime | None
