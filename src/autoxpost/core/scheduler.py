"""APScheduler wiring — checks the queue every N seconds for due posts."""

from __future__ import annotations

import logging
import signal
from typing import Callable

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger

from autoxpost.core.publisher import Publisher
from autoxpost.core.queue import PostQueue

log = logging.getLogger(__name__)


class PostScheduler:
    """Blocking scheduler that polls the queue for due posts and publishes them."""

    def __init__(
        self,
        queue: PostQueue,
        publisher: Publisher,
        poll_interval_seconds: int = 30,
    ):
        self.queue = queue
        self.publisher = publisher
        self.poll_interval_seconds = poll_interval_seconds
        self._scheduler = BlockingScheduler()

    def _tick(self) -> None:
        try:
            due = self.queue.list_due()
        except Exception:  # noqa: BLE001
            log.exception("failed to read due posts")
            return
        if not due:
            return
        log.info("publishing %d due post(s)", len(due))
        for post in due:
            result = self.publisher.publish(post)
            log.info(
                "post %s -> ok=%s failed=%d",
                post.id,
                ",".join(result.succeeded) or "-",
                len(result.failed),
            )

    def run(self) -> None:
        """Block forever, publishing due posts as they become available."""
        self._scheduler.add_job(
            self._tick,
            trigger=IntervalTrigger(seconds=self.poll_interval_seconds),
            id="autoxpost-tick",
            max_instances=1,
            coalesce=True,
            next_run_time=None,  # wait one full interval before first run
        )
        log.info("AutoXPost scheduler started (poll every %ss)", self.poll_interval_seconds)
        self._install_signal_handlers()
        try:
            self._scheduler.start()
        except (KeyboardInterrupt, SystemExit):
            log.info("scheduler stopped")
        finally:
            self._scheduler.shutdown(wait=False)

    def _install_signal_handlers(self) -> None:
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                self._scheduler.add_job(
                    lambda: self._scheduler.shutdown(wait=False),
                    trigger="date",
                    run_date=None,
                )
                # apscheduler installs its own signal handlers; let it.
                signal.signal(sig, lambda *_: self._scheduler.shutdown(wait=False))
            except (ValueError, NotImplementedError):
                # Not all platforms support all signals.
                pass
