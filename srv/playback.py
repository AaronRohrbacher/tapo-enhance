"""Progressive browser playback for one camera recording.

The selected recording is converted to a growing HLS playlist. The caller can
return the URL as soon as ``ready`` is set after the first segment, while the
gateway continues to own the sole camera media session until the exact clip
duration has been written.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, Callable

from .errors import ApiError, Code
from .recordings import Clip, Paths


Progress = Callable[[dict], None]


def make_runner(
    clip: Clip,
    paths: Paths,
    ready: asyncio.Event,
    *,
    on_progress: Progress | None = None,
):
    async def runner(tapo: Any, cancel: asyncio.Event) -> Path:
        try:
            return await _stream_hls(tapo, clip, paths, ready, cancel, on_progress)
        except BaseException as exc:
            phase = "cancelled" if isinstance(exc, asyncio.CancelledError) else "error"
            message = "Playback cancelled" if phase == "cancelled" else (str(exc) or type(exc).__name__)
            if on_progress is not None:
                on_progress({
                    "type": "operation",
                    "operation": "playback",
                    "key": clip.key,
                    "phase": phase,
                    "message": message,
                    "error": None if phase == "cancelled" else message,
                })
            raise

    return runner


def playlist_path(paths: Paths, clip: Clip) -> Path:
    return paths.playback / clip.date / f"{clip.start}_{clip.end}" / "index.m3u8"


def playlist_url(clip: Clip) -> str:
    return f"/playback/{clip.date}/{clip.start}_{clip.end}/index.m3u8"


async def _stream_hls(
    tapo: Any,
    clip: Clip,
    paths: Paths,
    ready: asyncio.Event,
    cancel: asyncio.Event,
    on_progress: Progress | None,
) -> Path:
    from pytapo.media_stream.recording_stream import RecordingStream

    out = playlist_path(paths, clip)
    out.parent.mkdir(parents=True, exist_ok=True)
    for old in out.parent.iterdir():
        if old.is_file() and old.suffix in (".ts", ".m3u8", ".tmp"):
            old.unlink(missing_ok=True)

    base = {"type": "operation", "operation": "playback", "key": clip.key}

    def report(phase: str, message: str, **extra) -> None:
        if on_progress is not None:
            on_progress({**base, "phase": phase, "message": message, **extra})

    use_vaapi = os.access("/dev/dri/renderD128", os.R_OK | os.W_OK)
    input_args = ["-vaapi_device", "/dev/dri/renderD128"] if use_vaapi else []
    video_args = (
        ["-vf", "format=nv12,hwupload", "-c:v", "h264_vaapi", "-qp", "24", "-g", "30", "-bf", "0"]
        if use_vaapi
        else [
            "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
            "-crf", "23", "-g", "30", "-keyint_min", "30", "-sc_threshold", "0",
        ]
    )
    ffmpeg = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostats", "-y",
        *input_args,
        "-fflags", "+genpts+discardcorrupt", "-f", "mpegts", "-i", "pipe:0",
        "-t", str(clip.duration),
        "-map", "0:v:0", "-map", "0:a:0?", *video_args,
        "-c:a", "aac", "-b:a", "96k", "-ar", "48000",
        "-f", "hls", "-hls_time", "2", "-hls_list_size", "0",
        "-hls_playlist_type", "event",
        "-hls_flags", "independent_segments+temp_file",
        "-hls_segment_filename", str(out.parent / "segment_%05d.ts"),
        "-progress", "pipe:1", str(out),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stderr_tail = bytearray()

    async def monitor() -> None:
        assert ffmpeg.stdout is not None
        while line := await ffmpeg.stdout.readline():
            name, _, value = line.decode(errors="replace").strip().partition("=")
            if not ready.is_set() and out.exists() and "segment_" in out.read_text(errors="ignore"):
                ready.set()
                report("ready", "Playback started", progress=0.0, url=playlist_url(clip))
            if name not in ("out_time_us", "out_time_ms"):
                continue
            try:
                seconds = int(value) / 1_000_000
            except ValueError:
                continue
            report(
                "streaming", "Streaming selected event",
                progress=min(0.99, seconds / max(1, clip.duration)),
                mediaSeconds=round(seconds, 2), totalSeconds=clip.duration,
            )

    async def capture_stderr() -> None:
        assert ffmpeg.stderr is not None
        while chunk := await ffmpeg.stderr.read(4096):
            stderr_tail.extend(chunk)
            if len(stderr_tail) > 8192:
                del stderr_tail[:-8192]

    monitor_task = asyncio.create_task(monitor(), name=f"playback-progress-{clip.start}")
    stderr_task = asyncio.create_task(capture_stderr(), name=f"playback-stderr-{clip.start}")
    recording = RecordingStream(
        tapo, clip.start, clip.end, event_type=(2,), window_size=200, no_data_timeout=8
    )
    bytes_video = 0
    report("connecting", "Opening selected camera event", progress=0.0)
    try:
        await recording.start()
        async for response in recording.responses():
            if cancel.is_set():
                raise asyncio.CancelledError
            if not response.plaintext:
                continue
            try:
                assert ffmpeg.stdin is not None
                ffmpeg.stdin.write(response.plaintext)
                bytes_video += len(response.plaintext)
                await ffmpeg.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                break
    finally:
        if ffmpeg.stdin is not None:
            try:
                ffmpeg.stdin.close()
            except Exception:
                pass
        try:
            await asyncio.wait_for(ffmpeg.wait(), timeout=15)
        except asyncio.TimeoutError:
            ffmpeg.kill()
            await ffmpeg.wait()
        for task in (monitor_task, stderr_task):
            try:
                await asyncio.wait_for(task, timeout=2)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                task.cancel()
        await recording.close()

    if not ready.is_set() and out.exists() and "segment_" in out.read_text(errors="ignore"):
        ready.set()
    if not ready.is_set():
        raise ApiError(
            Code.DOWNLOAD_EMPTY,
            "camera playback produced no playable segment",
            status=502,
            context={
                "ts_bytes": bytes_video,
                "ffmpeg_returncode": ffmpeg.returncode,
                "ffmpeg_stderr_tail": stderr_tail.decode(errors="replace")[-500:],
            },
        )
    report("complete", "Selected event finished", progress=1.0, url=playlist_url(clip))
    return out
