"""Manual sync regressions: pushed progress, overlap, errors and cancellation."""
import asyncio

import pytest

from srv.gateway import CameraGateway
from srv.jobs import JobRegistry
from srv.recordings import Clip

CLIPS = [Clip("20990101", 1700000000 + i * 5, 1700000002 + i * 5) for i in range(2)]


async def test_bulk_pushes_real_media_progress_and_skips_valid_cache(fake_media_tapo, paths):
    events = []
    gateway = CameraGateway(get_tapo=lambda: fake_media_tapo)
    jobs = JobRegistry(paths, gateway, on_event=events.append)
    await gateway.start()
    try:
        job = jobs.submit_bulk([CLIPS[0], CLIPS[0], CLIPS[1]])
        await asyncio.wait_for(job._task, 10)
        assert job.total == job.done == 2
        assert job.failed == 0
        assert job.status == "done"
        snapshots = [e["job"] for e in events if e["type"] == "job"]
        assert any(0 < e["progress"] < 1 for e in snapshots)
        assert any(e["download_current"] for e in snapshots)
        assert any(e["conversion_current"] for e in snapshots)
        assert snapshots[-1]["progress"] == 1
        assert len([e for e in events if e.get("phase") == "complete"]) == 2
        assert len(fake_media_tapo.sessions) == 2
        second = jobs.submit_bulk(CLIPS)
        await second._task
        assert second.done == 2
        assert len(fake_media_tapo.sessions) == 2
    finally:
        await jobs.stop()
        await gateway.stop()
        await jobs.conversions.stop()


async def test_completed_conversion_is_counted_while_next_download_waits(paths, monkeypatch):
    import srv.jobs as module
    second_started = asyncio.Event()
    release_second = asyncio.Event()
    completed_first = asyncio.Event()
    events = []

    def runner(clip, _paths, *, on_progress, operation):
        async def run(_tapo, _cancel):
            if clip == CLIPS[1]:
                second_started.set()
                await release_second.wait()
            on_progress({"key": clip.key, "stage": "download", "progress": 1.0})
            return paths.recording(clip).with_suffix(".download.ts")
        return run

    class Conversions:
        def submit(self, clip, _paths, staged, **kwargs):
            async def run():
                kwargs["on_progress"]({"key": clip.key, "stage": "conversion", "progress": .5})
                return staged
            return asyncio.create_task(run())

    def emit(event):
        events.append(event)
        if event["type"] == "job" and event["job"]["done"] == 1:
            completed_first.set()

    monkeypatch.setattr(module, "make_fetch_runner", runner)
    gateway = CameraGateway(get_tapo=lambda: object())
    jobs = JobRegistry(paths, gateway, Conversions(), on_event=emit)
    await gateway.start()
    try:
        job = jobs.submit_bulk(CLIPS)
        await asyncio.wait_for(second_started.wait(), 2)
        await asyncio.wait_for(completed_first.wait(), 2)
        assert job.status == "running"
        assert job.done == 1
        assert not job._task.done()
        assert any(e["type"] == "job" and e["job"]["progress"] >= .5 for e in events)
    finally:
        release_second.set()
        await job._task
        await gateway.stop()


async def test_bulk_cancel_finishes_current_clip_and_preserves_gateway(paths, monkeypatch):
    import srv.jobs as module
    started = asyncio.Event()
    release = asyncio.Event()

    def runner(clip, _paths, **kwargs):
        async def run(_tapo, _cancel):
            started.set()
            await release.wait()
            raise RuntimeError("camera disconnected")
        return run

    monkeypatch.setattr(module, "make_fetch_runner", runner)
    gateway = CameraGateway(get_tapo=lambda: object())
    jobs = JobRegistry(paths, gateway)
    await gateway.start()
    try:
        job = jobs.submit_bulk(CLIPS)
        await asyncio.wait_for(started.wait(), 2)
        assert jobs.cancel(job.id)
        assert job.status == "cancelling"
        release.set()
        await asyncio.wait_for(job._task, 2)
        assert job.status == "cancelled"
        assert job.failed == 1
        async def query(_tapo, _cancel):
            return "still alive"
        assert await asyncio.wait_for(gateway.submit_query("after-cancel", query), 2) == "still alive"
    finally:
        release.set()
        await jobs.stop()
        await gateway.stop()


async def test_bulk_reports_unexpected_preflight_error(paths, monkeypatch):
    import srv.jobs as module
    async def failed(*args):
        raise OSError("storage unavailable")
    monkeypatch.setattr(module, "cached_media_is_valid", failed)
    events = []
    jobs = JobRegistry(paths, None, on_event=events.append)
    job = jobs.submit_bulk(CLIPS)
    await job._task
    assert job.status == "error"
    assert "storage unavailable" in job.errors[0]
    assert events[-1]["job"]["status"] == "error"


async def test_bulk_failed_conversion_is_visible_and_other_clips_finish(paths, monkeypatch):
    import srv.jobs as module
    class Gateway:
        def submit_fetch(self, clip, runner):
            async def result():
                return paths.recording(clip)
            return asyncio.create_task(result())
    class Conversions:
        def submit(self, clip, *args, **kwargs):
            async def run():
                if clip == CLIPS[0]:
                    raise RuntimeError("encoder failed")
            return asyncio.create_task(run())
    jobs = JobRegistry(paths, Gateway(), Conversions())
    job = jobs.submit_bulk(CLIPS)
    await job._task
    assert (job.done, job.failed, job.status) == (1, 1, "done")
    assert job.to_dict()["progress"] == 1
    assert "encoder failed" in job.errors[0]
    assert job.message == "Sync finished with errors"
