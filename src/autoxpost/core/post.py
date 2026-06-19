"""Post data model.

A `Post` is the unit the user authors: a piece of text plus a list of
target platforms. The queue stores one row per post plus a row per
(target, attempt) so a failure on one platform doesn't poison the
others.
"""

from __future__ import annotations

import enum
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


class PostStatus(str, enum.Enum):
    PENDING = "pending"  # scheduled for the future
    DUE = "due"  # ready to publish
    PUBLISHING = "publishing"  # a worker has it
    PUBLISHED = "published"  # all targets succeeded
    PARTIAL = "partial"  # some targets succeeded, some failed
    FAILED = "failed"  # all targets failed


class TargetStatus(str, enum.Enum):
    PENDING = "pending"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"  # e.g. rate-limited, retried too many times


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


@dataclass
class PostTarget:
    """A single (post, platform) pair and its outcome."""

    platform: str
    status: TargetStatus = TargetStatus.PENDING
    remote_id: str | None = None
    remote_url: str | None = None
    error: str | None = None
    attempts: int = 0

    def to_row(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "status": self.status.value,
            "remote_id": self.remote_id,
            "remote_url": self.remote_url,
            "error": self.error,
            "attempts": self.attempts,
        }

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "PostTarget":
        return cls(
            platform=row["platform"],
            status=TargetStatus(row["status"]),
            remote_id=row.get("remote_id"),
            remote_url=row.get("remote_url"),
            error=row.get("error"),
            attempts=int(row.get("attempts") or 0),
        )


@dataclass
class Post:
    text: str
    targets: list[str]
    media_paths: list[str] = field(default_factory=list)
    id: str = field(default_factory=_new_id)
    status: PostStatus = PostStatus.PENDING
    scheduled_at: datetime | None = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    published_at: datetime | None = None
    target_results: list[PostTarget] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Normalise target list: lowercase, dedup, drop blanks.
        seen: set[str] = set()
        normalised: list[str] = []
        for t in self.targets:
            t = (t or "").strip().lower()
            if t and t not in seen:
                seen.add(t)
                normalised.append(t)
        self.targets = normalised
        if self.status == PostStatus.PENDING and self.scheduled_at is None:
            self.status = PostStatus.DUE

    # --- serialisation -----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "text": self.text,
            "targets": list(self.targets),
            "media_paths": list(self.media_paths),
            "status": self.status.value,
            "scheduled_at": self.scheduled_at.isoformat() if self.scheduled_at else None,
            "created_at": self.created_at.isoformat(),
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "target_results": [t.to_row() for t in self.target_results],
            "metadata": self.metadata,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Post":
        return cls(
            id=data.get("id") or _new_id(),
            text=data["text"],
            targets=list(data.get("targets") or []),
            media_paths=list(data.get("media_paths") or []),
            status=PostStatus(data.get("status", PostStatus.PENDING.value)),
            scheduled_at=(
                datetime.fromisoformat(data["scheduled_at"])
                if data.get("scheduled_at")
                else None
            ),
            created_at=(
                datetime.fromisoformat(data["created_at"])
                if data.get("created_at")
                else datetime.utcnow()
            ),
            published_at=(
                datetime.fromisoformat(data["published_at"])
                if data.get("published_at")
                else None
            ),
            target_results=[
                PostTarget.from_row(t) for t in data.get("target_results", [])
            ],
            metadata=dict(data.get("metadata") or {}),
        )
