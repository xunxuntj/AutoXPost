"""Publisher — orchestrates "publish this post to that platform"."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from autoxpost.core.post import Post, PostStatus, PostTarget, TargetStatus
from autoxpost.core.queue import PostQueue
from autoxpost.platforms.base import PlatformAdapter, PublishOutcome

if TYPE_CHECKING:
    from autoxpost.core.safety import RiskGuard

log = logging.getLogger(__name__)


@dataclass
class PublishResult:
    post_id: str
    succeeded: list[str]
    failed: list[tuple[str, str]]  # (platform, error)
    skipped: list[tuple[str, str]]  # (platform, reason)

    @property
    def ok(self) -> bool:
        return bool(self.succeeded) and not self.failed and not self.skipped


class Publisher:
    """Resolves adapters by name, calls them, records outcomes in the queue."""

    def __init__(
        self,
        queue: PostQueue,
        adapters: dict[str, PlatformAdapter],
        guard: "RiskGuard | None" = None,
    ):
        self.queue = queue
        self.adapters = adapters
        self.guard = guard

    @property
    def configured_platforms(self) -> list[str]:
        return sorted(self.adapters)

    def publish(self, post: Post) -> PublishResult:
        """Publish `post` to every configured platform it asked for.

        Platforms the post requested but that have no configured adapter are
        recorded as `skipped` with an explanatory error so the user can fix
        the env later.

        The passed-in `post` is mutated in place: `status`, `published_at`,
        and `target_results` are filled in with the actual outcomes so
        callers (e.g. the predefined runner) can write the post back to
        disk with accurate state.
        """
        self.queue.update_status(post.id, PostStatus.PUBLISHING)
        succeeded: list[str] = []
        failed: list[tuple[str, str]] = []
        skipped: list[tuple[str, str]] = []
        results: list[PostTarget] = []

        for platform in post.targets:
            target = self._existing_target(post.id, platform) or PostTarget(platform=platform)
            target.attempts += 1
            adapter = self.adapters.get(platform)

            if adapter is None:
                target.status = TargetStatus.SKIPPED
                target.error = f"no adapter configured for platform '{platform}'"
                self.queue.update_target(post.id, target)
                results.append(target)
                failed.append((platform, target.error))
                continue

            # Safety pre-check: rate-limit cooldown, min interval, daily /
            # burst caps, duplicate text, media size / mime.
            if self.guard is not None:
                check = self.guard.pre_check(post, platform)
                if not check.allowed:
                    target.status = TargetStatus.SKIPPED
                    target.error = check.reason
                    self.queue.update_target(post.id, target)
                    results.append(target)
                    skipped.append((platform, check.reason))
                    log.info("skip %s on %s: %s", post.id, platform, check.reason)
                    continue

            try:
                outcome: PublishOutcome = adapter.publish(post)
            except Exception as exc:  # noqa: BLE001 — adapters raise arbitrary SDK errors
                log.exception("publish failed on %s for post %s", platform, post.id)
                target.status = TargetStatus.FAILED
                target.error = f"{type(exc).__name__}: {exc}"[:1000]
                self.queue.update_target(post.id, target)
                self.queue.log_publish(post.id, platform, success=False, error=target.error)
                if self.guard is not None:
                    self.guard.record_failure(post, platform, exc)
                results.append(target)
                failed.append((platform, target.error))
                continue

            if outcome.success:
                target.status = TargetStatus.SUCCESS
                target.remote_id = outcome.remote_id
                target.remote_url = outcome.remote_url
                target.error = None
                self.queue.update_target(post.id, target)
                self.queue.log_publish(post.id, platform, success=True)
                if self.guard is not None:
                    self.guard.record_success(post, platform)
                results.append(target)
                succeeded.append(platform)
            else:
                target.status = TargetStatus.FAILED
                target.error = outcome.error
                self.queue.update_target(post.id, target)
                self.queue.log_publish(post.id, platform, success=False, error=outcome.error)
                if self.guard is not None:
                    # Treat any failure as a failure for the kill switch.
                    # Rate-limit-shaped errors carry an extractable signal.
                    self.guard.record_failure(post, platform, None)
                results.append(target)
                failed.append((platform, outcome.error or "unknown error"))

        # Roll the post's overall status up. SKIPPED targets don't count
        # against the post: it's only FAILED if every *attempted* target
        # failed. If everything was skipped, treat as FAILED so the
        # runner notices and the user can intervene.
        attempted = succeeded or failed  # any real outcome?
        if succeeded and not failed:
            overall = PostStatus.PUBLISHED
        elif succeeded and failed:
            overall = PostStatus.PARTIAL
        elif skipped and not attempted:
            # All targets were skipped (e.g. global cooldown) — surface as
            # FAILED so the runner / user notices, but the targets stay
            # SKIPPED in the queue.
            overall = PostStatus.FAILED
        else:
            overall = PostStatus.FAILED
        self.queue.update_status(
            post.id, overall, published_at=datetime.utcnow() if overall != PostStatus.FAILED else None
        )
        # Mirror the outcomes back onto the in-memory post so callers can
        # serialise it (e.g. write back to a JSON file on disk).
        post.target_results = results
        post.status = overall
        if overall != PostStatus.FAILED:
            post.published_at = datetime.utcnow()
        return PublishResult(
            post_id=post.id, succeeded=succeeded, failed=failed, skipped=skipped,
        )

    def _existing_target(self, post_id: str, platform: str) -> PostTarget | None:
        for t in self.queue.targets_for(post_id):
            if t.platform == platform:
                return t
        return None
