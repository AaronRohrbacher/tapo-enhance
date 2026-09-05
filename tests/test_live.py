"""Live preview through the gateway. Verifies the most important
contention guarantee: a download click pauses live, runs, and live
resumes — all on a single camera media session at a time.

Uses a fake live source that pushes bytes into the consumer until
the cancel event flips. No real camera, no real ffmpeg required for
the contention test (the HLS-output test uses real ffmpeg)."""

from __future__ import annotations

import asyncio

import pytest

from srv.gateway import CameraGateway
from srv.live import HlsConsumer
from srv.recordings import Clip


CLIP = Clip("20990101", 1700000000, 1700000002)


async def test_live_resumes_around_a_download(make_av_ts, tmp_path):
    """The headline UX: live runs, user clicks download, live pauses,
    download completes, live resumes. The activity log records the
    sequence; the test asserts the order."""

    feed_count = 0

    async def fake_live_source(_tapo, cancel: asyncio.Event, consumer):
        nonlocal feed_count
        while not cancel.is_set():
            try:
                await asyncio.wait_for(cancel.wait(), timeout=0.02)
            except asyncio.TimeoutError:
                feed_count += 1
                await consumer.feed(b"\x47" * 188)

    g = CameraGateway(get_tapo=lambda: object(), live_source=fake_live_source)
    await g.start()

    class Recorder:
        def __init__(self):
            self.events = []
            self.feeds = 0
        async def started(self): self.events.append("started")
        async def feed(self, _b): self.feeds += 1
        async def paused(self): self.events.append("paused")
        async def stopped(self): self.events.append("stopped")

    consumer = Recorder()
    g.attach_live(consumer)
    try:
        # Wait for live to actually be running.
        for _ in range(50):
            if g.live_running():
                break
            await asyncio.sleep(0.01)
        assert g.live_running()
        # Let some bytes flow.
        await asyncio.sleep(0.1)
        assert consumer.feeds > 0
        before = consumer.feeds

        async def dl_runner(_t, _c):
            assert not g.live_running(), "live still running while download owns the camera"
            await asyncio.sleep(0.02)
            return None

        await g.submit_download(CLIP, dl_runner)

        # Live must come back.
        for _ in range(50):
            if g.live_running():
                break
            await asyncio.sleep(0.01)
        assert g.live_running()
        await asyncio.sleep(0.1)
        # And feeds must continue accruing post-resume.
        assert consumer.feeds > before
    finally:
        await g.stop()
    assert consumer.events.count("started") >= 2  # initial + resume
    assert "paused" in consumer.events
    assert consumer.events[-1] == "stopped"


async def test_hls_consumer_writes_playlist_when_fed_real_ts(tmp_path, make_av_ts):
    """End-to-end with real ffmpeg: feed a known-good MPEG-TS into
    HlsConsumer and confirm an HLS playlist + segments land on disk."""
    src = tmp_path / "src.ts"
    make_av_ts(src, duration=3)
    ts_bytes = src.read_bytes()

    consumer = HlsConsumer(tmp_path / "stream")
    await consumer.started()
    # Feed in 16KB chunks at near real-time-ish pace.
    for i in range(0, len(ts_bytes), 16 * 1024):
        await consumer.feed(ts_bytes[i:i + 16 * 1024])
    # A pause flushes the current HLS window. The next generation must remove
    # it rather than turning live view into accumulated playback.
    await consumer.paused()
    playlist = consumer.playlist_path
    assert playlist.exists(), "HLS playlist not created"
    assert playlist.stat().st_size > 0
    segs = list(consumer.stream_dir.glob("seg_*.ts"))
    assert segs, "no HLS segments written"
    assert all(s.stat().st_size > 0 for s in segs)
    await consumer.started()
    assert not consumer.playlist_path.exists()
    assert not list(consumer.stream_dir.glob("seg_*.ts")), "restart retained stale live segments"
    await consumer.stopped()
    assert not list(consumer.stream_dir.iterdir()), "stopped stream cache was not purged"


async def test_detach_live_cleans_up():
    """Detaching live must stop the live task and emit stopped()."""

    async def fake_live_source(_t, cancel, consumer):
        await cancel.wait()

    g = CameraGateway(get_tapo=lambda: object(), live_source=fake_live_source)
    await g.start()

    class Recorder:
        def __init__(self): self.events = []
        async def started(self): self.events.append("started")
        async def feed(self, _b): pass
        async def paused(self): self.events.append("paused")
        async def stopped(self): self.events.append("stopped")

    consumer = Recorder()
    g.attach_live(consumer)
    try:
        for _ in range(50):
            if g.live_running():
                break
            await asyncio.sleep(0.01)
        await g.detach_live()
        assert not g.live_running()
        assert "started" in consumer.events
        assert "stopped" in consumer.events
        # paused must NOT be in events for a clean detach.
        assert "paused" not in consumer.events
    finally:
        await g.stop()
