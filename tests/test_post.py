"""Tests for the Post dataclass and the SQLite queue."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from autoxpost.core.post import Post, PostStatus, PostTarget, TargetStatus
from autoxpost.core.queue import PostQueue


@pytest.fixture
def queue(tmp_path: Path) -> PostQueue:
    return PostQueue(tmp_path / "test.db")


def test_post_normalises_targets() -> None:
    p = Post(text="hi", targets=["X", "mastodon", "x", "", "  "])
    assert p.targets == ["x", "mastodon"]
    # No scheduled time → moves to DUE so the scheduler picks it up.
    assert p.status == PostStatus.DUE


def test_post_scheduled_stays_pending() -> None:
    future = datetime.utcnow() + timedelta(hours=1)
    p = Post(text="hi", targets=["x"], scheduled_at=future)
    assert p.status == PostStatus.PENDING
    assert p.scheduled_at == future


def test_post_round_trip_json() -> None:
    p = Post(
        text="hello world",
        targets=["x", "bluesky"],
        media_paths=["/tmp/a.png"],
        metadata={"campaign": "launch"},
    )
    p.target_results = [PostTarget(platform="x", status=TargetStatus.SUCCESS, remote_id="42")]
    blob = p.to_json()
    restored = Post.from_dict(json.loads(blob))
    assert restored.text == p.text
    assert restored.targets == p.targets
    assert restored.metadata == p.metadata
    assert restored.target_results[0].remote_id == "42"


def test_queue_add_and_get(queue: PostQueue) -> None:
    p = Post(text="first", targets=["x", "mastodon"])
    queue.add(p)
    fetched = queue.get(p.id)
    assert fetched is not None
    assert fetched.text == "first"
    assert fetched.targets == ["x", "mastodon"]


def test_queue_list_due_returns_past_scheduled(queue: PostQueue) -> None:
    past = datetime.utcnow() - timedelta(minutes=5)
    p = Post(text="overdue", targets=["x"], scheduled_at=past)
    queue.add(p)
    assert any(post.id == p.id for post in queue.list_due())


def test_queue_list_due_skips_future(queue: PostQueue) -> None:
    future = datetime.utcnow() + timedelta(hours=2)
    p = Post(text="future", targets=["x"], scheduled_at=future)
    queue.add(p)
    assert not any(post.id == p.id for post in queue.list_due())


def test_queue_update_target_and_status(queue: PostQueue) -> None:
    p = Post(text="x", targets=["x"])
    queue.add(p)
    queue.update_target(p.id, PostTarget(
        platform="x", status=TargetStatus.SUCCESS, remote_id="abc", attempts=1
    ))
    queue.update_status(p.id, PostStatus.PUBLISHED, published_at=datetime.utcnow())
    refreshed = queue.get(p.id)
    assert refreshed is not None
    assert refreshed.status == PostStatus.PUBLISHED
    targets = queue.targets_for(p.id)
    assert targets[0].remote_id == "abc"
    assert targets[0].attempts == 1
