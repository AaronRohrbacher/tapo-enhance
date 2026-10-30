"""Manual download jobs with pushed, duration-weighted pipeline progress."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Callable, Iterable

from .downloader import ConversionQueue, cached_media_is_valid, make_fetch_runner
from .recordings import Clip, Paths

log = logging.getLogger("uvicorn.error.jobs")
ACTIVE = {"queued", "running", "cancelling"}


class Job:
    def __init__(self, kind: str, clips: list[Clip]):
        self.id = uuid.uuid4().hex[:8]
        self.kind = kind
        self.total = len(clips)
        self.done = 0
        self.failed = 0
        self.status = "queued"
        self.current: str | None = None
        self.message = "Waiting for camera access"
        self.errors: list[str] = []
        self.started = time.time()
        self.finished: float | None = None
        self._clips = clips
        self._task: asyncio.Task | None = None
        self._cancel = asyncio.Event()
        self._work = {c.key: [0.0, 0.0] for c in clips}
        self._download_current: str | None = None
        self._conversion_current: str | None = None
        self._settled: set[str] = set()
        self._revision = 0

    def to_dict(self) -> dict:
        duration = sum(max(1, c.duration) for c in self._clips)
        progress = sum(max(1, c.duration) * sum(self._work[c.key]) for c in self._clips)
        return {
            "id": self.id, "revision": self._revision, "kind": self.kind, "total": self.total,
            "done": self.done, "failed": self.failed, "status": self.status,
            "current": self.current, "message": self.message,
            "progress": progress / (2 * duration) if duration else 0.0,
            "download_current": self._download_current,
            "conversion_current": self._conversion_current,
            "errors": self.errors[-20:], "started": self.started,
            "finished": self.finished,
        }


class JobRegistry:
    def __init__(self, paths: Paths, gateway, conversions: ConversionQueue | None = None,
                 on_event: Callable[[dict], None] | None = None):
        self.paths = paths
        self.gateway = gateway
        self.conversions = conversions or ConversionQueue()
        self.on_event = on_event
        self._jobs: dict[str, Job] = {}

    def list(self) -> list[Job]:
        return list(self._jobs.values())

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def _emit(self, job: Job) -> None:
        job._revision += 1
        if self.on_event:
            self.on_event({"type": "job", "job": job.to_dict()})

    def cancel(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if not job or job.status not in ("queued", "running"):
            return False
        job._cancel.set()
        job.status = "cancelling"
        job.message = "Stopping downloads; finishing queued conversions"
        self._emit(job)
        return True

    async def stop(self) -> None:
        tasks = [j._task for j in self._jobs.values() if j._task and not j._task.done()]
        for j in self._jobs.values():
            if j.status in ACTIVE:
                j._cancel.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def submit_bulk(self, clips: Iterable[Clip]) -> Job:
        # Repeated selections must not count or convert the same output twice.
        clip_list = list({c.key: c for c in clips}.values())
        keys = {c.key for c in clip_list}
        for existing in self._jobs.values():
            if existing.status in {"queued", "running"} and {c.key for c in existing._clips} == keys:
                return existing
        # Keep recent results for reload/reconnect, without retaining every job forever.
        finished = [key for key, j in self._jobs.items() if j.status not in ACTIVE]
        for key in finished[:-19]:
            del self._jobs[key]
        job = Job("bulk_download", clip_list)
        self._jobs[job.id] = job
        job.status = "running"
        self._emit(job)
        log.info("manual sync %s started (%s clips)", job.id, job.total)
        job._task = asyncio.create_task(self._run(job), name=f"job-{job.id}")
        return job

    def _progress(self, job: Job, event: dict) -> None:
        key = event.get("key")
        if key not in job._work or key in job._settled:
            return
        stage = event.get("stage")
        index = 1 if stage == "conversion" else 0
        value = event.get("progress")
        if isinstance(value, (int, float)):
            job._work[key][index] = max(job._work[key][index], min(1.0, max(0.0, value)))
        if stage == "conversion":
            job._conversion_current = key
        else:
            job._download_current = key
        if job.status != "cancelling":
            job.current = key
            job.message = event.get("message") or event.get("phase") or "Working"
        self._emit(job)

    def _settle(self, job: Job, clip: Clip, error: Exception | None = None) -> None:
        if clip.key in job._settled:
            return
        job._settled.add(clip.key)
        job._work[clip.key] = [1.0, 1.0]
        if error is None:
            job.done += 1
            if self.on_event:
                self.on_event({"type": "operation", "operation": "bulk", "job_id": job.id,
                               "key": clip.key, "phase": "complete", "progress": 1.0})
        else:
            job.failed += 1
            job.errors.append(f"{clip.key}: {type(error).__name__}: {error}")
            log.warning("manual sync %s failed: %s", job.id, job.errors[-1])
        if job._conversion_current == clip.key:
            job._conversion_current = None
        self._emit(job)

    async def _convert(self, job: Job, clip: Clip, task: asyncio.Task) -> None:
        try:
            await asyncio.shield(task)
        except Exception as exc:
            self._settle(job, clip, exc)
        else:
            self._settle(job, clip)

    async def _run(self, job: Job) -> None:
        conversions: list[asyncio.Task] = []
        try:
            for clip in job._clips:
                if job._cancel.is_set():
                    break
                job.current = clip.key
                job.message = "Checking local recording"
                self._emit(job)
                if await cached_media_is_valid(self.paths.recording(clip), clip.duration):
                    self._settle(job, clip)
                    continue
                try:
                    job._download_current = clip.key
                    job.message = "Waiting for camera access"
                    self._emit(job)
                    staged = await asyncio.shield(self.gateway.submit_fetch(
                        clip, make_fetch_runner(clip, self.paths,
                            on_progress=lambda e: self._progress(job, e), operation="bulk")
                    ))
                    job._work[clip.key][0] = 1.0
                    job._download_current = None
                    task = self.conversions.submit(clip, self.paths, staged,
                        on_progress=lambda e: self._progress(job, e), operation="bulk")
                    conversions.append(asyncio.create_task(self._convert(job, clip, task)))
                except Exception as exc:
                    job._download_current = None
                    self._settle(job, clip, exc)
            if not job._cancel.is_set():
                job.message = "Finishing video conversions"
            self._emit(job)
            await asyncio.gather(*conversions)
        except asyncio.CancelledError:
            job._cancel.set()
            raise
        except Exception as exc:
            job.errors.append(f"{type(exc).__name__}: {exc}")
            job.status = "error"
            log.exception("manual sync %s failed", job.id)
        finally:
            for task in conversions:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*conversions, return_exceptions=True)
            job.current = job._download_current = job._conversion_current = None
            job.finished = time.time()
            if job._cancel.is_set():
                job.status, job.message = "cancelled", "Sync cancelled"
            elif job.status != "error":
                job.status = "done"
                job.message = "Sync finished with errors" if job.failed else "Sync complete"
            self._emit(job)
            log.info("manual sync %s %s (%s ready, %s failed)",
                     job.id, job.status, job.done, job.failed)
