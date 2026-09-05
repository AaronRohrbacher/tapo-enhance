"""Progressive archive playback through a real ffmpeg process."""

from __future__ import annotations

import asyncio

from srv.playback import make_runner, playlist_path
from srv.recordings import Clip


CLIP = Clip("20990101", 1700000000, 1700000002)


async def test_playback_publishes_hls_without_archiving(fake_media_tapo, paths):
    ready = asyncio.Event()
    events = []
    runner = make_runner(CLIP, paths, ready, on_progress=events.append)

    out = await runner(fake_media_tapo, asyncio.Event())

    assert ready.is_set()
    assert out == playlist_path(paths, CLIP)
    assert out.exists()
    manifest = out.read_text()
    assert "#EXTM3U" in manifest
    assert "segment_" in manifest
    assert not paths.recording(CLIP).exists()
    assert not paths.preview(CLIP).exists()
    assert any(event["phase"] == "ready" for event in events)
    assert events[-1]["phase"] == "complete"


async def test_playback_cancel_is_reported(fake_media_tapo, paths):
    ready = asyncio.Event()
    cancel = asyncio.Event()
    cancel.set()
    events = []
    runner = make_runner(CLIP, paths, ready, on_progress=events.append)

    try:
        await runner(fake_media_tapo, cancel)
    except asyncio.CancelledError:
        pass

    assert events[-1]["phase"] == "cancelled"
