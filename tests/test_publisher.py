"""Tests for the Publisher orchestrator using fake adapters."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from autoxpost.core.post import Post, PostStatus
from autoxpost.core.publisher import Publisher
from autoxpost.core.queue import PostQueue
from autoxpost.platforms.base import PlatformAdapter, PublishOutcome


class FakeAdapter(PlatformAdapter):
    """An adapter that records calls and returns a scripted outcome."""

    def __init__(self, name: str, outcome: PublishOutcome):
        super().__init__(config=None)
        self.name = name
        self._outcome = outcome
        self.calls: list[Any] = []

    def publish(self, post: Post) -> PublishOutcome:
        self.calls.append(post)
        return self._outcome


@pytest.fixture
def queue(tmp_path: Path) -> PostQueue:
    return PostQueue(tmp_path / "pub.db")


def test_publisher_succeeds_on_all_platforms(queue: PostQueue) -> None:
    x = FakeAdapter("x", PublishOutcome(success=True, remote_id="1", remote_url="https://x/1"))
    mast = FakeAdapter("mastodon", PublishOutcome(success=True, remote_id="2", remote_url="https://m/2"))
    pub = Publisher(queue, {"x": x, "mastodon": mast})
    post = Post(text="hi", targets=["x", "mastodon"])
    queue.add(post)

    result = pub.publish(post)

    assert result.ok
    assert result.succeeded == ["x", "mastodon"]
    assert result.failed == []
    assert queue.get(post.id).status == PostStatus.PUBLISHED
    assert len(x.calls) == 1
    assert len(mast.calls) == 1


def test_publisher_marks_partial_when_some_fail(queue: PostQueue) -> None:
    x = FakeAdapter("x", PublishOutcome(success=False, error="rate limited"))
    mast = FakeAdapter("mastodon", PublishOutcome(success=True, remote_id="2"))
    pub = Publisher(queue, {"x": x, "mastodon": mast})
    post = Post(text="hi", targets=["x", "mastodon"])
    queue.add(post)

    result = pub.publish(post)

    assert result.succeeded == ["mastodon"]
    assert [p for p, _ in result.failed] == ["x"]
    assert queue.get(post.id).status == PostStatus.PARTIAL


def test_publisher_skips_unknown_platforms(queue: PostQueue) -> None:
    pub = Publisher(queue, {})  # no adapters
    post = Post(text="hi", targets=["unknownplatform"])
    queue.add(post)

    result = pub.publish(post)

    assert result.succeeded == []
    assert [p for p, _ in result.failed] == ["unknownplatform"]
    assert queue.get(post.id).status == PostStatus.FAILED
    # The target row was recorded as skipped.
    targets = queue.targets_for(post.id)
    assert targets[0].error is not None
    assert "no adapter" in targets[0].error


def test_publisher_catches_adapter_exception(queue: PostQueue) -> None:
    class Exploding(PlatformAdapter):
        name = "boom"

        def publish(self, post: Post) -> PublishOutcome:
            raise RuntimeError("kaboom")

    pub = Publisher(queue, {"boom": Exploding(None)})
    post = Post(text="hi", targets=["boom"])
    queue.add(post)
    result = pub.publish(post)
    assert [p for p, _ in result.failed] == ["boom"]
    assert queue.get(post.id).status == PostStatus.FAILED
