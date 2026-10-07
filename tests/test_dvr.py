"""DVR scheduling and status calculations that do not require camera media."""

import asyncio

from datetime import datetime

import pytest

from srv.dvr import DvrService, days_between
from srv.recordings import Paths
from srv.recordings import Clip


class _Settings:
    dvr_enabled = False
    dvr_retention_days = 7
    dvr_keep_forever = False
    dvr_interval_minutes = 1440
    dvr_daily_time = "00:10"


def test_next_daily_run_is_1210am_local_time():
    service = DvrService(_Settings(), Paths("unused"), lambda fn: None, gateway=None)
    before = datetime(2026, 9, 18, 0, 9, 30)
    after = datetime(2026, 9, 18, 0, 10, 1)
    assert service._seconds_until_next_run(before) == 30
    assert service._seconds_until_next_run(after) == 86399


def test_camera_history_default_is_inclusive_span():
    assert days_between("20260915", today=datetime(2026, 9, 18)) == 4


def test_local_available_days_counts_dates_with_video_not_elapsed_span(tmp_path):
    paths = Paths(tmp_path)
    paths.ensure()
    for date in ("20260901", "20260918"):
        folder = paths.recordings / date
        folder.mkdir()
        (folder / "1_2.mp4").write_bytes(b"video")
    service = DvrService(_Settings(), paths, lambda fn: None, gateway=None)
    status = service.status()
    assert status["available_days"] == 2
    assert status["available_span_days"] == days_between("20260901")


def test_eta_models_overlapped_download_and_conversion_lanes(tmp_path):
    service = DvrService(_Settings(), Paths(tmp_path), lambda fn: None, gateway=None)
    service._sync_order = ["first", "second"]
    service._sync_durations = {"first": 10, "second": 10}
    service._sync_work = {"first": (1.0, 0.5), "second": (0.5, 0.0)}
    service._stage_rates = {"download": 1.0, "conversion": 2.0}

    # The second download takes five seconds, then its five-second conversion
    # finishes at ten seconds. The two lanes must not be added together.
    assert service._estimated_eta() == 10


def test_eta_smooths_new_throughput_measurements(tmp_path, monkeypatch):
    import srv.dvr as dvr_mod

    now = 100.0
    monkeypatch.setattr(dvr_mod.time, "monotonic", lambda: now)
    service = DvrService(_Settings(), Paths(tmp_path), lambda fn: None, gateway=None)
    service._sync_order = ["clip"]
    service._sync_durations = {"clip": 10}
    service._sync_work = {"clip": (0.0, 0.0)}
    service._stage_rates = {"download": 1.0, "conversion": 1.0}
    assert service._estimated_eta() == 20

    # A single faster sample changes only 20% of the displayed estimate.
    service._stage_rates["download"] = 2.0
    service._stage_rates["conversion"] = 2.0
    assert service._estimated_eta() == 18


def test_status_reports_download_and_conversion_counts_independently(tmp_path):
    service = DvrService(_Settings(), Paths(tmp_path), lambda fn: None, gateway=None)
    service._sync_total = 3
    service._sync_order = ["ready", "converting", "downloading"]
    service._sync_durations = {key: 10 for key in service._sync_order}
    service._sync_work = {
        "ready": (1.0, 1.0),
        "converting": (1.0, 0.5),
        "downloading": (0.25, 0.0),
    }
    status = service.status()
    assert status["download_complete"] == 2
    assert status["conversion_complete"] == 1
    assert status["download_progress"] == 0.75
    assert status["conversion_progress"] == 0.5


def test_conversion_status_reports_current_video_not_batch_total(tmp_path):
    service = DvrService(_Settings(), Paths(tmp_path), lambda fn: None, gateway=None)
    service._sync_durations = {"20261002/10/20": 10, "20261002/30/40": 10}
    service._sync_work = {
        "20261002/10/20": (1.0, 1.0),
        "20261002/30/40": (1.0, 0.4),
    }
    service._conversion_current_key = "20261002/30/40"
    service._conversion_current_progress = 0.4
    service._stage_rates["conversion"] = 2.0
    status = service.status()
    assert status["conversion_current_key"] == "20261002/30/40"
    assert status["conversion_current_progress"] == 0.4
    assert status["conversion_current_eta_seconds"] == 3


def test_conversion_indicator_returns_to_waiting_when_video_finishes(tmp_path):
    service = DvrService(_Settings(), Paths(tmp_path), lambda fn: None, gateway=None)
    key = "20261002/30/40"
    service._sync_durations = {key: 10}
    service._sync_work = {key: (1.0, 0.9)}
    service._conversion_current_key = key
    service._conversion_current_progress = 0.9

    service._emit_operation({
        "type": "operation", "operation": "dvr", "key": key,
        "stage": "conversion", "phase": "complete", "progress": 1.0,
    })

    status = service.status()
    assert status["conversion_current_key"] is None
    assert status["conversion_current_progress"] == 0.0


async def test_camera_index_failure_is_not_silently_skipped(tmp_path):
    class Camera:
        def getRecordingsList(self, **_kwargs):
            return [{"date": "20260918"}, {"date": "20260917"}]

        def getRecordings(self, date):
            if date == "20260917":
                raise RuntimeError("camera index failed")
            return [{"video_results": [{"startTime": 1, "endTime": 2}]}]

    class ImmediateGateway:
        def submit_dvr_query(self, _key, runner, **_kwargs):
            return runner(Camera(), None)

    service = DvrService(
        _Settings(), Paths(tmp_path), lambda op: op(Camera()), ImmediateGateway()
    )
    with pytest.raises(RuntimeError, match="camera index failed"):
        await service._camera_clips(
            datetime(2026, 9, 17), datetime(2026, 9, 18)
        )


async def test_dvr_starts_next_download_before_conversion_finishes(tmp_path, monkeypatch):
    """Conversion must not occupy the serialized camera-download lane."""
    import srv.dvr as dvr_mod

    clips = [Clip("20260918", 1, 3), Clip("20260918", 4, 6)]
    events = []
    release_conversion = asyncio.Event()

    async def invalid(_path, _duration):
        return False

    def fake_fetch_runner(clip, _paths, **_kwargs):
        async def runner(_tapo, _cancel):
            events.append(f"download:{clip.start}")
            path = tmp_path / f"{clip.start}.ts"
            path.write_bytes(b"x" * 2048)
            return path
        return runner

    class Gateway:
        async def submit_dvr_download(self, _clip, runner, **_kwargs):
            return await runner(None, asyncio.Event())

    class Conversions:
        def submit(self, clip, *_args, **_kwargs):
            async def convert():
                events.append(f"convert-start:{clip.start}")
                await release_conversion.wait()
                return tmp_path / f"{clip.start}.mp4"
            return asyncio.create_task(convert())

    monkeypatch.setattr(dvr_mod, "cached_media_is_valid", invalid)
    monkeypatch.setattr(dvr_mod, "make_fetch_runner", fake_fetch_runner)
    service = DvrService(
        _Settings(), Paths(tmp_path), lambda fn: None, Gateway(),
        conversions=Conversions(),
    )
    task = asyncio.create_task(service._download_missing(clips))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert "download:1" in events
    assert "download:4" in events
    assert not task.done()
    release_conversion.set()
    assert await task == 0
    assert service.status()["sync_progress"] == 1.0


async def test_dvr_resumes_staged_conversion_without_camera_download(
    tmp_path, monkeypatch,
):
    import srv.dvr as dvr_mod

    clip = Clip("20260918", 1, 3)
    paths = Paths(tmp_path)
    paths.ensure()
    staged = paths.recording(clip).with_suffix(".download.ts")
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_bytes(b"x" * 2048)
    gateway_calls = 0
    converted = []

    async def invalid(_path, _duration):
        return False

    class Gateway:
        async def submit_dvr_download(self, *_args, **_kwargs):
            nonlocal gateway_calls
            gateway_calls += 1
            raise TimeoutError

    class Conversions:
        def submit(self, queued_clip, _paths, queued_staged, **_kwargs):
            async def convert():
                converted.append((queued_clip, queued_staged))
            return asyncio.create_task(convert())

    monkeypatch.setattr(dvr_mod, "cached_media_is_valid", invalid)
    service = DvrService(
        _Settings(), paths, lambda fn: None, Gateway(), conversions=Conversions(),
    )
    assert await service._download_missing([clip]) == 0
    assert gateway_calls == 0
    assert converted == [(clip, staged)]


async def test_dvr_retries_transient_camera_download_failure(tmp_path, monkeypatch):
    import srv.dvr as dvr_mod

    clip = Clip("20260918", 1, 3)
    attempts = 0

    async def invalid(_path, _duration):
        return False

    async def no_delay(_seconds):
        return None

    def fake_fetch_runner(*_args, **_kwargs):
        return object()

    class Gateway:
        async def submit_dvr_download(self, *_args, **_kwargs):
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise TimeoutError("camera asleep")
            staged = tmp_path / "clip.ts"
            staged.write_bytes(b"x" * 2048)
            return staged

    class Conversions:
        def submit(self, *_args, **_kwargs):
            async def convert():
                return tmp_path / "clip.mp4"
            return asyncio.create_task(convert())

    monkeypatch.setattr(dvr_mod, "cached_media_is_valid", invalid)
    monkeypatch.setattr(dvr_mod, "make_fetch_runner", fake_fetch_runner)
    monkeypatch.setattr(dvr_mod.asyncio, "sleep", no_delay)
    service = DvrService(
        _Settings(), Paths(tmp_path), lambda fn: None, Gateway(),
        conversions=Conversions(),
    )
    assert await service._download_missing([clip]) == 0
    assert attempts == 3
