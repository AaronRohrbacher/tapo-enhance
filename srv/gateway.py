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
    DOWNLOAD = 0   # user clicked watch / download — top priority
    THUMB = 10     # background backfill — bottom priority


# A runner takes (tapo_client, cancel_event) and returns the result the
# submitter awaits (e.g. a Path for downloaders, anything for thumbs).
Runner = Callable[[Any, asyncio.Event], Awaitable[Any]]


class LiveConsumer(Protocol):
    """The live-preview consumer. Gateway calls these at the right moments."""

    async def started(self) -> None: ...
    async def feed(self, ts_chunk: bytes) -> None: ...
    async def paused(self) -> None: ...
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


class CameraGateway:
    def __init__(
        self,
        *,
        get_tapo: Callable[[], Any],
        live_source: LiveSource | None = None,
        on_camera_error: Callable[[BaseException], Awaitable[None]] | None = None,
    ):
        """`get_tapo` returns an authenticated Tapo client. `live_source` is the
        coroutine that, when called, opens the camera media session and pushes
        TS chunks to a LiveConsumer until the cancel event is set. Both are
        injected so tests can substitute fakes."""
        self._get_tapo = get_tapo
        self._live_source = live_source
        self._on_camera_error = on_camera_error
        self._queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._inflight: dict[str, _TransientJob] = {}
        self._seq = 0
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._live_consumer: LiveConsumer | None = None
        self._live_task: asyncio.Task | None = None
        self._live_cancel = asyncio.Event()
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

    # ── public submission API ──────────────────────────────────────────

    def submit_download(self, clip: Clip, runner: Runner, *, label: str = "") -> asyncio.Future:
        return self._submit(Priority.DOWNLOAD, f"dl:{clip.key}", runner, label or f"dl {clip.key}")

    def submit_thumb(self, clip: Clip, runner: Runner, *, label: str = "") -> asyncio.Future:
        return self._submit(Priority.THUMB, f"th:{clip.key}", runner, label or f"th {clip.key}")

    def attach_live(self, consumer: LiveConsumer) -> None:
        """Attach a live consumer. Worker will start streaming as soon as the
        transient queue drains. Replacing an existing consumer detaches it."""
        if self._live_source is None:
            raise RuntimeError("gateway has no live_source configured")
        self._live_consumer = consumer
        self._wake.set()

    async def detach_live(self) -> None:
        consumer = self._live_consumer
        self._live_consumer = None
        await self._stop_live_if_running(for_pause=False)
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
        # Tell the live loop to yield as soon as it can.
        self._live_cancel.set()
        self._wake.set()
        self._activity_log.append(("submit", key))
        return fut

    async def _run(self) -> None:
        try:
            while True:
                # Drain transient queue first, every iteration.
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

                # Wait for: new job, live ended, or wake.
                self._wake.clear()
                wait_for = [asyncio.create_task(self._wake.wait())]
                if self._live_task and not self._live_task.done():
                    wait_for.append(self._live_task)
                done, pending = await asyncio.wait(
                    wait_for, return_when=asyncio.FIRST_COMPLETED
                )
                for t in pending:
                    t.cancel()
        except asyncio.CancelledError:
            await self._stop_live_if_running(for_pause=False)
            raise

    async def _stop_live_if_running(self, *, for_pause: bool) -> None:
        had_task = self._live_task is not None
        if self._live_task is not None:
            self._activity_log.append(("live", "pause" if for_pause else "stop"))
            self._live_cancel.set()
            if not self._live_task.done():
                try:
                    await asyncio.wait_for(self._live_task, timeout=5)
                except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                    self._live_task.cancel()
                    try:
                        await self._live_task
                    except (asyncio.CancelledError, Exception):
                        pass
            self._live_task = None
            self._live_cancel.clear()
        # Emit pause/stop regardless of whether the inner task exited early.
        if had_task and for_pause and self._live_consumer is not None:
            try:
                await self._live_consumer.paused()
            except Exception:
                log.exception("live consumer .paused() raised")

    async def _run_transient(self, job: _TransientJob) -> None:
        kind = "download" if job.priority == Priority.DOWNLOAD else "thumb"
        self._busy_kind = kind
        self._activity_log.append((kind, "start:" + job.key))
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

    async def _live_loop(self, consumer: LiveConsumer) -> None:
        # `paused()` / `stopped()` are emitted by the gateway, NOT here, so
        # the consumer can distinguish pause-for-preemption from final stop.
        try:
            tapo = await asyncio.get_event_loop().run_in_executor(
                None, self._get_tapo
            )
            await consumer.started()
            assert self._live_source is not None
            await self._live_source(tapo, self._live_cancel, consumer)
        except asyncio.CancelledError:
            raise
        except BaseException as e:
            self._activity_log.append(("live", "error:" + repr(e)[:60]))
            await self._notify_camera_error(e)

    async def _notify_camera_error(self, exc: BaseException) -> None:
        if self._on_camera_error is None:
            return
        try:
            await self._on_camera_error(exc)
        except Exception:
            log.exception("on_camera_error handler raised")
