"""CameraGateway — the single, serialized owner of every camera media session.

Why this exists
---------------
The Tapo D225 services exactly ONE media session at a time. If any code path
opens a session while another is open (or even while one is being torn down),
both stall. Previous versions of this app had multiple owners — a live-stream
subprocess, a bulk download path, and a thumb generator — and they fought
each other constantly. The user saw "thumbs interrupting watch interrupting
thumbs" and unreliable downloads.

This module is the architectural fix. It exposes three operations:
  - `attach_live(consumer)` / `detach_live()` — long-lived live preview.
  - `submit_download(clip, runner)` — one-shot, user priority.
  - `submit_thumb(clip, runner)` — one-shot, lowest priority.

Internally a single worker coroutine drains transient (download / thumb)
jobs first, in priority order. While transient jobs are queued, the live
session is paused (closed). When the queue drains, live resumes if a
consumer is still attached. Identical inflight jobs are deduped — two
callers asking for the same download share one Future.

This module knows nothing about ffmpeg, file paths, or HLS — it only knows
how to lend the camera to one consumer at a time. The actual download /
thumb / live work is supplied by the caller as `runner` callbacks that
receive an open Tapo client.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Awaitable, Callable, Protocol

from .recordings import Clip


log = logging.getLogger(__name__)


class Priority(IntEnum):
    QUERY = 0      # short interactive camera API call
    DOWNLOAD = 0   # user clicked watch / download — top priority
    THUMB = 10     # background backfill — bottom priority
    DVR = 20       # unattended history sync must not stall the UI


# A runner takes (tapo_client, cancel_event) and returns the result the
# submitter awaits (e.g. a Path for downloaders, anything for thumbs).
Runner = Callable[[Any, asyncio.Event], Awaitable[Any]]


class LiveConsumer(Protocol):
    """The live-preview consumer. Gateway calls these at the right moments."""

    async def started(self) -> None: ...
    async def feed(self, ts_chunk: bytes) -> None: ...
    async def paused(self) -> None: ...
    async def reconnecting(self) -> None: ...
    async def stopped(self) -> None: ...


# A live source pulls TS chunks off the Tapo media session. Injected by the
# caller so tests don't need real pytapo.
LiveSource = Callable[[Any, asyncio.Event, LiveConsumer], Awaitable[None]]


@dataclass(order=True)
class _TransientJob:
    priority: int
    seq: int
    key: str = field(compare=False)
    runner: Runner = field(compare=False)
    cancel: asyncio.Event = field(compare=False)
    future: asyncio.Future = field(compare=False)
    label: str = field(default="", compare=False)
    finished: asyncio.Event = field(default_factory=asyncio.Event, compare=False)


class CameraGateway:
    def __init__(
        self,
        *,
        get_tapo: Callable[[], Any],
        live_source: LiveSource | None = None,
        on_camera_error: Callable[[BaseException], Awaitable[None]] | None = None,
        on_event: Callable[[dict], None] | None = None,
    ):
        """`get_tapo` returns an authenticated Tapo client. `live_source` is the
        coroutine that, when called, opens the camera media session and pushes
        TS chunks to a LiveConsumer until the cancel event is set. `on_event`,
        if given, is called (synchronously, non-blocking) with a status dict on
        every state change, so the UI can be pushed instead of polling. All are
        injected so tests can substitute fakes."""
        self._get_tapo = get_tapo
        self._live_source = live_source
        self._on_camera_error = on_camera_error
        self._on_event = on_event
        self._queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._inflight: dict[str, _TransientJob] = {}
        self._seq = 0
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._live_consumer: LiveConsumer | None = None
        self._live_task: asyncio.Task | None = None
        self._live_stop_lock = asyncio.Lock()
        self._live_cancel = asyncio.Event()
        self._live_restarts = 0
        self._live_error: str | None = None
        # Set whenever the worker is currently inside a transient runner; lets
        # tests assert no overlap with live, and is the fact every "no
        # contention" property test depends on.
        self._busy_kind: str | None = None
        self._activity_log: list[tuple[str, str]] = []  # for tests

    # ── lifecycle ──────────────────────────────────────────────────────

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="camera-gateway")

    async def stop(self) -> None:
        await self.detach_live()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        # Fail any remaining queued jobs.
        while not self._queue.empty():
            try:
                job = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if not job.future.done():
                job.future.set_exception(RuntimeError("gateway stopped"))
            self._inflight.pop(job.key, None)
            job.finished.set()

    # ── public submission API ──────────────────────────────────────────

    def submit_download(self, clip: Clip, runner: Runner, *, label: str = "") -> asyncio.Future:
        return self._submit(Priority.DOWNLOAD, f"dl:{clip.key}", runner, label or f"dl {clip.key}")

    def submit_query(self, key: str, runner: Runner, *, label: str = "") -> asyncio.Future:
        """Run an interactive camera API call through the single owner."""
        return self._submit(Priority.QUERY, f"query:{key}", runner, label or key)

    def submit_playback(self, clip: Clip, runner: Runner, *, label: str = "") -> asyncio.Future:
        """Submit transient viewing work without conflating it with an archive save."""
        for key, job in tuple(self._inflight.items()):
            if key.startswith("play:") and key != f"play:{clip.key}":
                job.cancel.set()
        return self._submit(Priority.DOWNLOAD, f"play:{clip.key}", runner, label or f"play {clip.key}")

    def cancel_playback(self) -> list[asyncio.Future]:
        futures = []
        for key, job in tuple(self._inflight.items()):
            if key.startswith("play:"):
                job.cancel.set()
                futures.append(asyncio.create_task(job.finished.wait()))
        return futures

    def submit_thumb(self, clip: Clip, runner: Runner, *, label: str = "") -> asyncio.Future:
        return self._submit(Priority.THUMB, f"th:{clip.key}", runner, label or f"th {clip.key}")

    def submit_dvr_download(self, clip: Clip, runner: Runner, *, label: str = "") -> asyncio.Future:
        """Queue unattended DVR work behind every interactive camera request."""
        return self._submit(Priority.DVR, f"dl:{clip.key}", runner, label or f"DVR {clip.key}")

    def submit_dvr_query(self, key: str, runner: Runner, *, label: str = "") -> asyncio.Future:
        """Serialize a short DVR index query without blocking queued UI work."""
        return self._submit(Priority.DVR, f"dvr-query:{key}", runner, label or key)

    def cancel_dvr(self) -> list[asyncio.Future]:
        """Release DVR camera work without stopping the shared gateway."""
        futures = []
        for job in tuple(self._inflight.values()):
            if job.priority == Priority.DVR:
                job.cancel.set()
                futures.append(asyncio.create_task(job.finished.wait()))
        return futures

    def attach_live(self, consumer: LiveConsumer) -> None:
        """Attach a live consumer. Worker will start streaming as soon as the
        transient queue drains. Replacing an existing consumer detaches it."""
        if self._live_source is None:
            raise RuntimeError("gateway has no live_source configured")
        self._live_consumer = consumer
        self._wake.set()
        self._emit()

    async def detach_live(self) -> None:
        consumer = self._live_consumer
        self._live_consumer = None
        self._wake.set()
        await self._stop_live_if_running(for_pause=False)
        self._emit()
        if consumer is not None:
            try:
                await consumer.stopped()
            except Exception:
                log.exception("live consumer .stopped() raised")

    # ── observability hooks (used by tests + status endpoint) ──────────

    def queued_count(self) -> int:
        return self._queue.qsize()

    def busy_kind(self) -> str | None:
        return self._busy_kind

    def live_attached(self) -> bool:
        return self._live_consumer is not None

    def live_running(self) -> bool:
        return self._live_task is not None and not self._live_task.done()

    def status(self) -> dict:
        return {
            "type": "gateway",
            "busy": self._busy_kind,
            "queued": self.queued_count(),
            "liveAttached": self.live_attached(),
            "liveRunning": self.live_running(),
            "liveRestarts": self._live_restarts,
            "liveError": self._live_error,
        }

    def _emit(self) -> None:
        if self._on_event is not None:
            self._on_event(self.status())

    @property
    def activity(self) -> list[tuple[str, str]]:
        return list(self._activity_log)

    # ── internals ──────────────────────────────────────────────────────

    def _submit(self, priority: int, key: str, runner: Runner, label: str) -> asyncio.Future:
        existing = self._inflight.get(key)
        if existing is not None:
            return existing.future
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        self._seq += 1
        job = _TransientJob(
            priority=int(priority),
            seq=self._seq,
            key=key,
            runner=runner,
            cancel=asyncio.Event(),
            future=fut,
            label=label,
        )
        self._inflight[key] = job
        self._queue.put_nowait(job)
        # Wake the worker immediately. It closes live once, drains the queued
        # batch in priority order, then reconnects live. Native thumbnails are
        # small JPEG requests, so they no longer justify indefinite deferral.
        self._live_cancel.set()
        self._wake.set()
        self._activity_log.append(("submit", key))
        self._emit()
        return fut

    async def _run(self) -> None:
        try:
            while True:
                # Close live once, drain the whole queued batch, then resume.
                # No two media sessions overlap on the camera.
                while not self._queue.empty():
                    job = await self._queue.get()
                    await self._stop_live_if_running(for_pause=True)
                    await self._run_transient(job)

                # Queue empty: start live if a consumer is attached.
                if self._live_consumer is not None and not self.live_running():
                    self._live_cancel.clear()
                    self._activity_log.append(("live", "start"))
                    self._live_task = asyncio.create_task(
                        self._live_loop(self._live_consumer), name="camera-live"
                    )
                    self._emit()

                # Wait for: new job, live ended, or wake.
                self._wake.clear()
                wake_waiter = asyncio.create_task(self._wake.wait())
                wait_for = [wake_waiter]
                if self._live_task and not self._live_task.done():
                    wait_for.append(self._live_task)
                done, pending = await asyncio.wait(
                    wait_for, return_when=asyncio.FIRST_COMPLETED
                )
                # Only the temporary wake waiter is ours to cancel here.
                # Cancelling live here and again in teardown can interrupt
                # its socket cleanup, leaving the next camera job contending.
                for t in pending:
                    if t is wake_waiter:
                        t.cancel()
                        await asyncio.gather(t, return_exceptions=True)
                # A camera stream can finish or fail without being explicitly
                # preempted.  Reap its ffmpeg consumer before restarting it;
                # previously the next cycle overwrote the process reference and
                # left multiple ffmpegs fighting over one playlist.
                if self._live_task is not None and self._live_task in done:
                    await self._stop_live_if_running(for_pause=True)
        except asyncio.CancelledError:
            await self._stop_live_if_running(for_pause=False)
            raise

    async def _stop_live_if_running(self, *, for_pause: bool) -> None:
        # Tab changes (detach) and queued requests can tear down the same
        # session simultaneously. Exactly one owner must await/clear it.
        async with self._live_stop_lock:
            await self._stop_live_locked(for_pause=for_pause)

    async def _stop_live_locked(self, *, for_pause: bool) -> None:
        had_task = self._live_task is not None
        if self._live_task is not None:
            live_task = self._live_task
            self._activity_log.append(("live", "pause" if for_pause else "stop"))
            self._live_cancel.set()
            if not live_task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(live_task), timeout=5)
                except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                    live_task.cancel()
                    try:
                        await live_task
                    except (asyncio.CancelledError, Exception):
                        pass
            self._live_task = None
            self._live_cancel.clear()
            self._emit()
        # Emit pause/stop regardless of whether the inner task exited early.
        if had_task and for_pause and self._live_consumer is not None:
            try:
                await self._live_consumer.paused()
            except Exception:
                log.exception("live consumer .paused() raised")

    async def _run_transient(self, job: _TransientJob) -> None:
        if job.cancel.is_set():
            if not job.future.done():
                job.future.cancel()
            self._inflight.pop(job.key, None)
            job.finished.set()
            return
        kind = (
            "query" if job.key.startswith("query:") or job.key.startswith("dvr-query:")
            else "download" if job.priority == Priority.DOWNLOAD
            else "dvr" if job.priority == Priority.DVR
            else "thumb"
        )
        self._busy_kind = kind
        self._activity_log.append((kind, "start:" + job.key))
        self._emit()
        try:
            try:
                tapo = await asyncio.get_event_loop().run_in_executor(
                    None, self._get_tapo
                )
            except Exception as e:
                if not job.future.done():
                    job.future.set_exception(e)
                self._activity_log.append((kind, "fail:" + job.key))
                await self._notify_camera_error(e)
                return
            try:
                res = await job.runner(tapo, job.cancel)
            except asyncio.CancelledError:
                if not job.future.done():
                    job.future.cancel()
                self._activity_log.append((kind, "cancel:" + job.key))
                # A runner uses its job event for ordinary user cancellation.
                # Only propagate cancellation when the gateway worker itself
                # is shutting down.
                if asyncio.current_task().cancelling() or not job.cancel.is_set():
                    raise
            except Exception as e:
                if not job.future.done():
                    job.future.set_exception(e)
                self._activity_log.append((kind, "fail:" + job.key))
                await self._notify_camera_error(e)
            else:
                if not job.future.done():
                    job.future.set_result(res)
                self._activity_log.append((kind, "done:" + job.key))
        finally:
            self._busy_kind = None
            self._inflight.pop(job.key, None)
            job.finished.set()
            self._emit()

    async def _live_loop(self, consumer: LiveConsumer) -> None:
        # `paused()` / `stopped()` are emitted by the gateway, NOT here, so
        # the consumer can distinguish pause-for-preemption from final stop.
        try:
            tapo = await asyncio.get_event_loop().run_in_executor(
                None, self._get_tapo
            )
            self._live_error = None
            await consumer.started()
            assert self._live_source is not None
            await self._live_source(tapo, self._live_cancel, consumer)
            if not self._live_cancel.is_set():
                reconnecting = getattr(consumer, "reconnecting", consumer.paused)
                await reconnecting()
                await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            raise
        except BaseException as e:
            self._live_restarts += 1
            self._live_error = f"{type(e).__name__}: {e}"
            self._activity_log.append(("live", "error:" + repr(e)[:60]))
            self._emit()
            await self._notify_camera_error(e)
            if not self._live_cancel.is_set():
                reconnecting = getattr(consumer, "reconnecting", consumer.paused)
                await reconnecting()
                await asyncio.sleep(1.0)

    async def _notify_camera_error(self, exc: BaseException) -> None:
        if self._on_camera_error is None:
            return
        try:
            await self._on_camera_error(exc)
        except Exception:
            log.exception("on_camera_error handler raised")
