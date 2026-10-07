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

from .media import media_wait

log = logging.getLogger(__name__)


PLAYLIST_NAME = "output.m3u8"
SEGMENT_PATTERN = "seg_%05d.ts"


class HlsConsumer:
    """LiveConsumer that pipes TS chunks into ffmpeg → HLS files. The same
    instance is reused across pause / resume cycles; ffmpeg is restarted
    each cycle so it always writes valid output."""

    def __init__(self, stream_dir: Path, *, on_event=None):
        self.stream_dir = stream_dir
        self.stream_dir.mkdir(parents=True, exist_ok=True)
        self._ffmpeg: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task | None = None
        self._stderr_tail = bytearray()
        self._generation = 0
        self._playable_generation = 0
        self._on_event = on_event
        self._use_vaapi = os.access("/dev/dri/renderD128", os.R_OK | os.W_OK)
        self.errors: list[str] = []

    @property
    def playlist_path(self) -> Path:
        return self.stream_dir / PLAYLIST_NAME

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def playable(self) -> bool:
        return self._generation > 0 and self._playable_generation == self._generation

    @property
    def playback_url(self) -> str:
        return f"/stream/{PLAYLIST_NAME}?v={self._generation}"

    async def started(self) -> None:
        # A live generation must contain only frames from this camera session.
        # Retaining segments across reconnects turns the browser timeline into
        # accumulated playback and can make "live" replay earlier footage.
        self._clear_stream_files()
        self._generation += 1
        await self._spawn_ffmpeg()
        self._emit("starting", "Opening the camera live feed")

    async def feed(self, ts_chunk: bytes) -> None:
        if not ts_chunk or self._ffmpeg is None or self._ffmpeg.stdin is None:
            return
        try:
            self._ffmpeg.stdin.write(ts_chunk)
            await self._ffmpeg.stdin.drain()
            if (
                self._playable_generation != self._generation
                and self.playlist_path.exists()
                and self.playlist_path.stat().st_size > 0
            ):
                self._playable_generation = self._generation
                self._emit("playing", "Live feed is playing")
        except (BrokenPipeError, ConnectionResetError) as exc:
            await self._reap()
            raise RuntimeError("live transcoder stopped accepting camera data") from exc

    async def paused(self) -> None:
        await self._reap()
        self._emit("paused", "Live feed paused for a user-requested camera operation")

    async def reconnecting(self) -> None:
        await self._reap()
        self._emit("reconnecting", "Camera live stream ended; reconnecting")

    async def stopped(self) -> None:
        await self._reap()
        self._clear_stream_files()
        self._emit("stopped", "Live feed stopped")

    def _clear_stream_files(self) -> None:
        """A stopped live view has no consumers, so retain no transient HLS."""
        for path in self.stream_dir.iterdir():
            if path.suffix in (".ts", ".m3u8", ".tmp"):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass

    def _emit(self, phase: str, message: str, *, error: str | None = None) -> None:
        if self._on_event is not None:
            self._on_event({
                "type": "operation",
                "operation": "live",
                "key": "live",
                "phase": phase,
                "message": message,
                "error": error,
                "generation": self._generation,
                "url": f"/stream/output.m3u8?v={self._generation}",
            })

    async def _spawn_ffmpeg(self) -> None:
        input_args = ["-vaapi_device", "/dev/dri/renderD128"] if self._use_vaapi else []
        if self._use_vaapi:
            video_args = [
                "-vf", "format=nv12,hwupload", "-c:v", "h264_vaapi",
                "-qp", "24", "-g", "30", "-bf", "0",
            ]
        else:
            video_args = [
                "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
                "-crf", "23", "-g", "30", "-keyint_min", "30", "-sc_threshold", "0",
            ]

        self._stderr_tail.clear()
        self._ffmpeg = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
            *input_args,
            "-fflags", "+genpts+discardcorrupt",
            "-f", "mpegts", "-i", "pipe:0",
            "-map", "0:v:0", "-map", "0:a:0?",
            *video_args,
            "-c:a", "aac", "-b:a", "96k", "-ar", "48000",
            "-f", "hls",
            "-hls_time", "2",
            "-hls_list_size", "6",
            "-hls_delete_threshold", "2",
            "-hls_flags", "delete_segments+omit_endlist+independent_segments+temp_file",
            "-hls_segment_filename", str(self.stream_dir / SEGMENT_PATTERN),
            "-start_number", "0",
            str(self.playlist_path),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        self._stderr_task = asyncio.create_task(
            self._capture_stderr(), name="live-ffmpeg-stderr"
        )

    async def _capture_stderr(self) -> None:
        ffmpeg = self._ffmpeg
        if ffmpeg is None or ffmpeg.stderr is None:
            return
        while True:
            chunk = await ffmpeg.stderr.read(4096)
            if not chunk:
                return
            self._stderr_tail.extend(chunk)
            if len(self._stderr_tail) > 8192:
                del self._stderr_tail[:-8192]

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
        if self._stderr_task is not None:
            try:
                await asyncio.wait_for(self._stderr_task, timeout=1)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._stderr_task.cancel()
            self._stderr_task = None
        if ffmpeg.returncode and ffmpeg.returncode != 0:
            err = self._stderr_tail.decode(errors="replace")[-500:]
            self.errors.append(err or f"ffmpeg exited {ffmpeg.returncode}")
            if self._use_vaapi:
                self._use_vaapi = False
            self._emit("error", "Live transcoder failed; retrying", error=self.errors[-1])


# ── Live source: pump bytes from the camera media session ──────────────────


async def camera_live_source(tapo: Any, cancel: asyncio.Event, consumer) -> None:
    from pytapo.media_stream.preview_stream import PreviewStream

    preview = PreviewStream(tapo, quality="HD", no_data_timeout=10)
    try:
        await media_wait(preview.start(), cancel, timeout=15)
        responses = preview.responses().__aiter__()
        while True:
            if cancel.is_set():
                break
            try:
                response = await media_wait(responses.__anext__(), cancel, timeout=15)
            except StopAsyncIteration:
                break
            if response.plaintext:
                await media_wait(consumer.feed(response.plaintext), cancel, timeout=10)
    finally:
        try:
            await asyncio.wait_for(preview.close(), timeout=5)
        except Exception:
            pass
