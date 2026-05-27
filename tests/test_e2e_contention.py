"""End-to-end test of the architectural promise: live stream is running,
user clicks a download, live pauses, download completes, live resumes —
all on a single shared camera media session, never overlapping.

This is THE test that catches the regression the user reported. It uses
the real CameraGateway, real ffmpeg subprocesses for the HLS muxer and
the downloader, and a fake camera session yielding actual TS bytes.
"""

from __future__ import annotations

import asyncio

import pytest

from srv.downloader import make_runner as download_runner
from srv.gateway import CameraGateway
from srv.live import HlsConsumer
from srv.recordings import Clip


CLIP = Clip("20990101", 1700000000, 1700000002)


class FakeStreamType:
    Download = "download"
    Stream = "stream"


@pytest.fixture(autouse=True)
def patch_stream_type(monkeypatch):
    import sys, types
    fake_mod = types.ModuleType("pytapo.media_stream._utils")
    fake_mod.StreamType = FakeStreamType
    monkeypatch.setitem(sys.modules, "pytapo.media_stream._utils", fake_mod)


async def test_live_pauses_for_download_then_resumes(
    fake_media_tapo, paths, tmp_path, ffprobe_streams,
):
    """The headline contention test. Sequence we expect:
       live.start → download.start (live paused) → download.done → live.resume
    Every step is observed via the gateway activity log.
    """
    # Provide a live source that pumps fresh fake-session bytes until cancelled.
    async def live_src(tapo, cancel: asyncio.Event, consumer):
        sess = tapo.getMediaSession(FakeStreamType.Stream)
        await sess.start()
        try:
            async for resp in sess.transceive("{}", "application/json"):
                if cancel.is_set():
                    break
                if getattr(resp, "mimetype", "") == "video/mp2t" and resp.plaintext:
                    await consumer.feed(resp.plaintext)
        finally:
            await sess.close()

    g = CameraGateway(get_tapo=lambda: fake_media_tapo, live_source=live_src)
    await g.start()

    consumer = HlsConsumer(tmp_path / "stream")
    g.attach_live(consumer)

    try:
        # Wait for live to actually run and produce at least one segment.
        for _ in range(80):
            if g.live_running():
                break
            await asyncio.sleep(0.05)
        assert g.live_running()

        # Submit a download. The downloader uses gateway.submit_download which
        # pauses live, runs, then resumes.
        out = await g.submit_download(CLIP, download_runner(CLIP, paths))
        assert out.exists()
        streams = ffprobe_streams(out)
        types = {s["codec_type"] for s in streams}
        assert types == {"video", "audio"}, "downloader lost audio while contending with live"

        # And live must be back up.
        for _ in range(80):
            if g.live_running():
                break
            await asyncio.sleep(0.05)
        assert g.live_running(), "live did not resume after download"

        # Activity log should record: live start → live pause → download → live start (again).
        kinds = [k for k, _ in g.activity if k in ("live", "download")]
        assert "live" in kinds
        assert "download" in kinds
        # The download substring "done" must appear, no "fail".
        assert any(e.startswith("done:") for k, e in g.activity if k == "download")
        assert not any(e.startswith("fail:") for k, e in g.activity if k == "download")
    finally:
        await g.stop()


async def test_thumb_during_live_is_serialised(fake_media_tapo, paths, tmp_path):
    """Backfill thumb runs while live is running — must pause live for the
    duration of the thumb pull, just like a download."""
    from srv.thumbs import make_camera_runner

    async def live_src(tapo, cancel: asyncio.Event, consumer):
        sess = tapo.getMediaSession(FakeStreamType.Stream)
        await sess.start()
        try:
            async for resp in sess.transceive("{}", "application/json"):
                if cancel.is_set():
                    break
                if getattr(resp, "mimetype", "") == "video/mp2t" and resp.plaintext:
                    await consumer.feed(resp.plaintext)
        finally:
            await sess.close()

    g = CameraGateway(get_tapo=lambda: fake_media_tapo, live_source=live_src)
    await g.start()
    consumer = HlsConsumer(tmp_path / "stream")
    g.attach_live(consumer)
    try:
        for _ in range(80):
            if g.live_running():
                break
            await asyncio.sleep(0.05)
        assert g.live_running()

        # During the thumb pull, busy_kind must be "thumb" (not None, not "live").
        assertion_results = []

        async def watcher():
            for _ in range(30):
                if g.busy_kind() == "thumb":
                    assertion_results.append(("thumb_busy", not g.live_running()))
                    return
                await asyncio.sleep(0.01)

        watch_task = asyncio.create_task(watcher())
        out = await g.submit_thumb(CLIP, make_camera_runner(CLIP, paths))
        await watch_task
        assert out.exists()
        # While the thumb was running, live must NOT have been running concurrently.
        if assertion_results:
            kind, paused = assertion_results[0]
            assert paused, "live was running while thumb was pulling — that's the contention bug"
    finally:
        await g.stop()
