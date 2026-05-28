"""Live preview. The previous version ran a separate `stream_worker.py`
subprocess that opened its OWN media session, completely outside the
gateway — which is THE root cause of the contention bugs the user has
been hitting. We now run live as a gateway-managed live consumer:
the gateway opens / closes the media session in lockstep with the queue,
so live and downloads/thumbs cannot ever overlap on the camera.

The consumer runs an ffmpeg child that converts the incoming MPEG-TS
stream into HLS (.ts segments + .m3u8 playlist) for the browser. When
the gateway preempts us, we tell ffmpeg's stdin "EOF" by closing it —
ffmpeg flushes the current segment cleanly. When we resume, we spawn a
fresh ffmpeg with a manifest discontinuity tag; HLS players handle this
as a brief stutter, no full reload required.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from .media_session import build_preview_request, normalize_response, open_session

log = logging.getLogger(__name__)


PLAYLIST_NAME = "output.m3u8"
SEGMENT_PATTERN = "seg_%05d.ts"


class HlsConsumer:
    """LiveConsumer that pipes TS chunks into ffmpeg → HLS files. The same
    instance is reused across pause / resume cycles; ffmpeg is restarted
    each cycle so it always writes valid output."""

    def __init__(self, stream_dir: Path):
        self.stream_dir = stream_dir
        self.stream_dir.mkdir(parents=True, exist_ok=True)
        self._ffmpeg: asyncio.subprocess.Process | None = None
        self._segment_index = 0  # counter so files don't collide across restarts
        self._first_started = False
        self.errors: list[str] = []

    @property
    def playlist_path(self) -> Path:
        return self.stream_dir / PLAYLIST_NAME

    async def started(self) -> None:
        # Wipe stale files only on the FIRST start. Subsequent resumes keep
        # the existing segments visible while ffmpeg writes new ones.
        if not self._first_started:
            self._first_started = True
            for f in self.stream_dir.iterdir():
                if f.suffix in (".ts", ".m3u8"):
                    try:
                        f.unlink()
                    except OSError:
                        pass
        await self._spawn_ffmpeg()

    async def feed(self, ts_chunk: bytes) -> None:
        if not ts_chunk or self._ffmpeg is None or self._ffmpeg.stdin is None:
            return
        try:
            self._ffmpeg.stdin.write(ts_chunk)
            await self._ffmpeg.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            await self._reap()

    async def paused(self) -> None:
        await self._reap()

    async def stopped(self) -> None:
        await self._reap()

    async def _spawn_ffmpeg(self) -> None:
        # Start segment numbering after whatever's already on disk so the
        # browser's HLS player sees fresh, monotonically increasing segments.
        existing = sorted(self.stream_dir.glob("seg_*.ts"))
        if existing:
            try:
                last = int(existing[-1].stem.split("_")[1])
                self._segment_index = last + 1
            except (ValueError, IndexError):
                pass

        self._ffmpeg = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y",
            "-fflags", "+genpts+discardcorrupt",
            "-f", "mpegts", "-i", "pipe:0",
            "-map", "0:v:0", "-map", "0:a:0?",
            "-c:v", "copy",
            "-c:a", "aac", "-b:a", "96k", "-ar", "48000",
            "-f", "hls",
            "-hls_time", "2",
            "-hls_list_size", "6",
            "-hls_flags", "delete_segments+append_list+omit_endlist",
            "-hls_segment_filename", str(self.stream_dir / SEGMENT_PATTERN),
            "-start_number", str(self._segment_index),
            str(self.playlist_path),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )

    async def _reap(self) -> None:
        ffmpeg = self._ffmpeg
        self._ffmpeg = None
        if ffmpeg is None:
            return
        if ffmpeg.stdin is not None:
            try:
                ffmpeg.stdin.close()
            except Exception:
                pass
        try:
            await asyncio.wait_for(ffmpeg.wait(), timeout=5)
        except asyncio.TimeoutError:
            ffmpeg.kill()
            try:
                await ffmpeg.wait()
            except Exception:
                pass
        if ffmpeg.returncode and ffmpeg.returncode != 0:
            try:
                err = (await ffmpeg.stderr.read()).decode(errors="replace")[-300:]
                self.errors.append(err)
            except Exception:
                pass


# ── Live source: pump bytes from the camera media session ──────────────────


async def camera_live_source(tapo: Any, cancel: asyncio.Event, consumer) -> None:
    from pytapo.media_stream._utils import StreamType

    session = await open_session(tapo, StreamType.Stream)
    try:
        await session.start()
        req = build_preview_request()
        try:
            iter_ = session.transceive(req, "application/json")
        except TypeError:
            iter_ = session.transceive(req, "application/json")

        async for raw in iter_:
            if cancel.is_set():
                break
            r = normalize_response(raw)
            if r.plaintext:
                await consumer.feed(r.plaintext)
    finally:
        try:
            await session.close()
        except Exception:
            pass
