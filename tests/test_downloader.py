"""End-to-end downloader test: real ffmpeg + the FakeMediaSession that
yields a valid H.265+AAC MPEG-TS plus simulated G.711 audio frames.
The output mp4 is then ffprobed; we assert it has BOTH video AND audio
streams. This is the test that catches the missing-audio regression."""

from __future__ import annotations

import asyncio

import pytest

from srv.downloader import make_runner
from srv.errors import ApiError, Code
from srv.gateway import CameraGateway
from srv.recordings import Clip, Paths


CLIP = Clip("20990101", 1700000000, 1700000002)


async def _run_via_gateway(tapo, clip, paths):
    g = CameraGateway(get_tapo=lambda: tapo)
    await g.start()
    try:
        return await g.submit_download(clip, make_runner(clip, paths))
    finally:
        await g.stop()


async def test_downloads_produce_h264_plus_aac_mp4(fake_media_tapo, paths, ffprobe_streams):
    """The headline regression test: the camera emits H.265 + separate
    G.711 audio, and the user must get an H.264 + AAC mp4 they can play
    in a normal browser."""
    out = await _run_via_gateway(fake_media_tapo, CLIP, paths)
    assert out.exists()
    assert out.stat().st_size > 4096
    streams = ffprobe_streams(out)
    types = {s["codec_type"] for s in streams}
    codecs = {s["codec_name"] for s in streams}
    assert types == {"video", "audio"}, f"missing track: {streams}"
    assert "h264" in codecs, f"video not transcoded to h264: {codecs}"
    assert "aac" in codecs, f"audio not transcoded to aac: {codecs}"


async def test_dedup_dual_clicks_share_one_pull(fake_media_tapo, paths):
    """Clicking download twice must NOT pull twice."""
    g = CameraGateway(get_tapo=lambda: fake_media_tapo)
    await g.start()
    try:
        f1 = g.submit_download(CLIP, make_runner(CLIP, paths))
        f2 = g.submit_download(CLIP, make_runner(CLIP, paths))
        out1 = await f1
        out2 = await f2
        assert out1 == out2
        # Only one media-session was actually opened.
        assert len(fake_media_tapo.sessions) == 1
    finally:
        await g.stop()


async def test_already_cached_short_circuits(fake_media_tapo, paths, make_av_mp4):
    """If the file's already on disk, runner must return without touching
    the camera. Catches a regression where bulk download would re-pull
    every clip even when cached."""
    cached = paths.recording(CLIP)
    cached.parent.mkdir(parents=True, exist_ok=True)
    make_av_mp4(cached, duration=1)
    pre_size = cached.stat().st_size
    pre_mtime = cached.stat().st_mtime

    out = await _run_via_gateway(fake_media_tapo, CLIP, paths)
    assert out == cached
    # Untouched.
    assert cached.stat().st_size == pre_size
    assert cached.stat().st_mtime == pre_mtime
    # And no media session was opened.
    assert len(fake_media_tapo.sessions) == 0


async def test_empty_pull_raises_structured_error(fake_tapo, paths, monkeypatch):
    """If the camera produces no bytes, the user gets a clean DOWNLOAD_EMPTY
    error — not a silent zero-byte file in their archive."""

    class EmptySession:
        def set_window_size(self, *a, **kw): pass
        async def start(self): pass
        async def close(self): pass
        async def transceive(self, *_a, **_kw):
            class Resp:
                plaintext = b""
                mimetype = "video/mp2t"
                audioPayload = b""
            yield Resp()

    fake_tapo.getMediaSession = lambda *a, **kw: EmptySession()  # type: ignore[attr-defined]

    g = CameraGateway(get_tapo=lambda: fake_tapo)
    await g.start()
    try:
        with pytest.raises(ApiError) as ei:
            await g.submit_download(CLIP, make_runner(CLIP, paths))
        assert ei.value.code is Code.DOWNLOAD_EMPTY
        # And no zero-byte residue left behind.
        assert not paths.recording(CLIP).exists()
    finally:
        await g.stop()


async def test_serialises_two_different_downloads(fake_media_tapo, paths):
    """Two distinct downloads should run, but never overlap on the camera."""
    clip2 = Clip("20990101", 1700000000, 1700000002)
    clip3 = Clip("20990101", 1700000005, 1700000007)
    g = CameraGateway(get_tapo=lambda: fake_media_tapo)
    await g.start()
    try:
        f1 = g.submit_download(clip2, make_runner(clip2, paths))
        f2 = g.submit_download(clip3, make_runner(clip3, paths))
        await asyncio.gather(f1, f2)
        # Both files exist.
        assert paths.recording(clip2).exists()
        assert paths.recording(clip3).exists()
        # No more than 2 sessions were ever opened.
        assert len(fake_media_tapo.sessions) == 2
    finally:
        await g.stop()
