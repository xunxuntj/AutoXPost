"""Adapter interface every platform implements."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from autoxpost.core.post import Post


@dataclass
class PublishOutcome:
    success: bool
    remote_id: str | None = None
    remote_url: str | None = None
    error: str | None = None


class PlatformAdapter(ABC):
    """Base class. Subclasses encapsulate one social network's SDK."""

    #: The platform name used in `Post.targets`.
    name: str = ""

    def __init__(self, config: object) -> None:
        self.config = config

    @abstractmethod
    def publish(self, post: Post) -> PublishOutcome:
        """Publish `post` and return a `PublishOutcome` describing the result."""

    def validate(self, post: Post) -> str | None:
        """Optional: return an error message if `post` can't be sent to this platform."""
        return None

    @property
    def char_limit(self) -> int | None:
        return None
