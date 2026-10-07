"""Bulk download jobs. A user clicks "pull entire SD card" and we kick off
a long-running task that submits each clip to the gateway one by one. The
gateway already serializes camera access, so this is just a thin queue +
status accounting layer."""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Iterable

from .downloader import ConversionQueue, cached_media_is_valid, make_fetch_runner
from .recordings import Clip, Paths


class Job:
    __slots__ = ("id", "kind", "total", "done", "failed", "status", "current",
                 "errors", "started", "finished", "_clips", "_task", "_cancel")

    def __init__(self, kind: str, clips: list[Clip]):
        self.id = uuid.uuid4().hex[:8]
        self.kind = kind
        self.total = len(clips)
        self.done = 0
        self.failed = 0
        self.status = "queued"
        self.current: str | None = None
        self.errors: list[str] = []
        self.started = time.time()
        self.finished: float | None = None
        self._clips = clips
        self._task: asyncio.Task | None = None
        self._cancel = asyncio.Event()

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "total": self.total,
            "done": self.done,
            "failed": self.failed,
            "status": self.status,
            "current": self.current,
            "errors": self.errors[-20:],
            "started": self.started,
            "finished": self.finished,
        }


class JobRegistry:
    def __init__(self, paths: Paths, gateway, conversions: ConversionQueue | None = None):
        self.paths = paths
        self.gateway = gateway
        self.conversions = conversions or ConversionQueue()
        self._jobs: dict[str, Job] = {}

    def list(self) -> list[Job]:
        return list(self._jobs.values())

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def cancel(self, job_id: str) -> bool:
        j = self._jobs.get(job_id)
        if not j or j.status not in ("queued", "running"):
            return False
        j._cancel.set()
        j.status = "cancelling"
        return True

    def submit_bulk(self, clips: Iterable[Clip]) -> Job:
        clip_list = list(clips)
        job = Job("bulk_download", clip_list)
        self._jobs[job.id] = job
        job.status = "running"
        job._task = asyncio.create_task(self._run(job), name=f"job-{job.id}")
        return job

    async def _run(self, job: Job) -> None:
        conversions: list[tuple[Clip, asyncio.Task]] = []
        try:
            for clip in job._clips:
                if job._cancel.is_set():
                    break
                job.current = f"{clip.date} {clip.start}"
                out = self.paths.recording(clip)
                if await cached_media_is_valid(out, clip.duration):
                    job.done += 1
                    continue
                try:
                    staged = await self.gateway.submit_download(
                        clip, make_fetch_runner(clip, self.paths)
                    )
                    conversions.append((
                        clip, self.conversions.submit(clip, self.paths, staged)
                    ))
                except Exception as e:
                    job.failed += 1
                    job.errors.append(f"{clip.key}: {type(e).__name__}: {e}")
            for clip, task in conversions:
                if job._cancel.is_set() and not task.done():
                    task.cancel()
                try:
                    await task
                    job.done += 1
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    job.failed += 1
                    job.errors.append(f"{clip.key}: {type(e).__name__}: {e}")
        finally:
            job.current = None
            job.finished = time.time()
            if job._cancel.is_set():
                job.status = "cancelled"
            else:
                job.status = "done"
