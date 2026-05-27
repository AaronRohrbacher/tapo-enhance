"""Thumbnail generation: local extraction, camera pull, and the backfill
queue's behaviour around the gateway."""

from __future__ import annotations

import asyncio

import pytest

from srv.gateway import CameraGateway
from srv.recordings import Clip
from srv.thumbs import ThumbBackfill, extract_from_local, make_camera_runner


CLIP = Clip("20990101", 1700000000, 1700000005)


class FakeStreamType:
    Download = "download"


@pytest.fixture(autouse=True)
def patch_stream_type(monkeypatch):
    import sys, types
    fake_mod = types.ModuleType("pytapo.media_stream._utils")
    fake_mod.StreamType = FakeStreamType
    monkeypatch.setitem(sys.modules, "pytapo.media_stream._utils", fake_mod)


# ── local extraction ──────────────────────────────────────────────────────


async def test_extract_from_local_returns_thumb_when_recording_exists(paths, make_av_mp4):
    rec = paths.recording(CLIP)
    rec.parent.mkdir(parents=True, exist_ok=True)
    make_av_mp4(rec, duration=2)
    out = await extract_from_local(CLIP, paths)
    assert out is not None and out.exists()
    assert out.stat().st_size > 100


async def test_extract_from_local_falls_back_to_preview(paths, make_av_mp4):
    prev = paths.preview(CLIP)
    prev.parent.mkdir(parents=True, exist_ok=True)
    make_av_mp4(prev, duration=2)
    out = await extract_from_local(CLIP, paths)
    assert out is not None and out.exists()


async def test_extract_from_local_returns_none_when_nothing_cached(paths):
    assert await extract_from_local(CLIP, paths) is None


# ── camera pull ───────────────────────────────────────────────────────────


async def test_camera_runner_writes_a_jpeg(fake_media_tapo, paths):
    g = CameraGateway(get_tapo=lambda: fake_media_tapo)
    await g.start()
    try:
        out = await g.submit_thumb(CLIP, make_camera_runner(CLIP, paths))
        assert out.exists()
        assert out.stat().st_size > 100
        # File begins with the JPEG SOI marker.
        assert out.read_bytes()[:2] == b"\xff\xd8"
    finally:
        await g.stop()


# ── backfill ──────────────────────────────────────────────────────────────


async def test_backfill_uses_local_path_when_recording_cached(paths, make_av_mp4, fake_tapo):
    """When a clip is already on disk, backfill must NOT touch the camera."""
    rec = paths.recording(CLIP)
    rec.parent.mkdir(parents=True, exist_ok=True)
    make_av_mp4(rec, duration=2)

    g = CameraGateway(get_tapo=lambda: fake_tapo)
    await g.start()
    bf = ThumbBackfill(paths, g)
    bf.start()
    try:
        bf.request(CLIP)
        # Wait up to 5s for the thumb to land.
        for _ in range(50):
            if bf.state(CLIP.key) and bf.state(CLIP.key)["status"] == "ready":
                break
            await asyncio.sleep(0.1)
        assert bf.state(CLIP.key)["status"] == "ready"
        assert paths.thumb(CLIP).exists()
    finally:
        await bf.stop()
        await g.stop()


async def test_backfill_falls_through_to_camera_when_no_local_cache(fake_media_tapo, paths):
    g = CameraGateway(get_tapo=lambda: fake_media_tapo)
    await g.start()
    bf = ThumbBackfill(paths, g)
    bf.start()
    try:
        bf.request(CLIP)
        for _ in range(50):
            st = bf.state(CLIP.key)
            if st and st["status"] in ("ready", "failed"):
                break
            await asyncio.sleep(0.1)
        assert bf.state(CLIP.key)["status"] == "ready", bf.state(CLIP.key)
        assert paths.thumb(CLIP).exists()
        assert len(fake_media_tapo.sessions) == 1
    finally:
        await bf.stop()
        await g.stop()


async def test_backfill_retry_endpoint_requeues_failed(paths, fake_tapo):
    g = CameraGateway(get_tapo=lambda: fake_tapo)
    await g.start()
    bf = ThumbBackfill(paths, g)
    try:
        # Manually mark a clip as failed and exhausted, then ask for a retry.
        bf._state[CLIP.key] = {
            "status": "failed",
            "attempts": 2,
            "error": "fake",
            "clip": CLIP,
        }
        n = bf.retry_failed_for_date("20990101")
        assert n == 1
        assert bf._state[CLIP.key]["status"] == "queued"
        assert bf._state[CLIP.key]["attempts"] == 0
    finally:
        await g.stop()


async def test_backfill_dedupes_repeated_requests(paths, fake_tapo):
    g = CameraGateway(get_tapo=lambda: fake_tapo)
    await g.start()
    bf = ThumbBackfill(paths, g)
    try:
        s1 = bf.request(CLIP)
        s2 = bf.request(CLIP)
        assert s1 == "queued"
        assert s2 == "queued"
        assert bf._q.qsize() == 1
    finally:
        await g.stop()
