"""Efficient, bounded retrieval of one SD-card recording.

Camera bytes stream directly through ffmpeg; they are never accumulated in
Python memory or repeatedly probed while arriving. Some firmware continues
into later events, so ffmpeg applies a hard duration cut and the result is
validated before publication.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Callable

from .errors import ApiError, Code
from .recordings import Clip, Paths


Progress = Callable[[dict], None]


def make_runner(
    clip: Clip,
    paths: Paths,
    *,
    deadline_overhead: int = 60,
    destination: Path | None = None,
    on_progress: Progress | None = None,
    operation: str = "download",
):
    """Return a CameraGateway runner for an archive or transient destination."""

    async def runner(tapo: Any, cancel: asyncio.Event) -> Path:
        try:
            return await _download(
                tapo, clip, paths, deadline_overhead,
                destination=destination, cancel=cancel,
                on_progress=on_progress, operation=operation,
            )
        except BaseException as exc:
            if isinstance(exc, asyncio.CancelledError):
                phase, message = "cancelled", "Operation cancelled"
            else:
                phase, message = "error", str(exc) or type(exc).__name__
            _report(on_progress, {
                "type": "operation", "operation": operation, "key": clip.key,
                "phase": phase, "message": message, "error": message,
            })
            raise

    return runner


def _report(callback: Progress | None, event: dict) -> None:
    if callback is None:
        return
    try:
        callback(event)
    except Exception:
        # Reporting is deliberately isolated from camera/media correctness.
        pass


async def _probe(path: Path) -> tuple[float, bool]:
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration", "-show_entries", "stream=codec_type",
        "-of", "json", str(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
    except asyncio.TimeoutError as exc:
        proc.kill()
        await proc.wait()
        raise ApiError(Code.FFMPEG_FAILED, "media validation timed out", status=502) from exc
    if proc.returncode != 0:
        raise ApiError(
            Code.FFMPEG_FAILED, "download validation failed", status=502,
            context={"ffprobe_stderr_tail": stderr.decode(errors="replace")[-500:]},
        )
    try:
        data = json.loads(stdout)
        duration = float(data.get("format", {}).get("duration") or 0)
        has_video = any(s.get("codec_type") == "video" for s in data.get("streams", []))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ApiError(Code.FFMPEG_FAILED, "invalid media validation result", status=502) from exc
    return duration, has_video


async def _download(
    tapo: Any,
    clip: Clip,
    paths: Paths,
    deadline_overhead: int,
    *,
    destination: Path | None,
    cancel: asyncio.Event,
    on_progress: Progress | None,
    operation: str,
) -> Path:
    from pytapo.media_stream.recording_stream import RecordingStream

    out = destination or paths.recording(clip)
    base_event = {"type": "operation", "operation": operation, "key": clip.key}

    def report(phase: str, message: str, **extra) -> None:
        _report(on_progress, {**base_event, "phase": phase, "message": message, **extra})

    if out.exists() and out.stat().st_size > 0:
        report("complete", "Already available locally", progress=1.0, cached=True)
        return out

    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp.mp4")
    tmp.unlink(missing_ok=True)

    report("connecting", "Opening a camera playback session", progress=0.0)
    recording = RecordingStream(
        tapo,
        clip.start,
        clip.end,
        event_type=(2,),
        window_size=200,
        no_data_timeout=8,
    )

    use_vaapi = os.access("/dev/dri/renderD128", os.R_OK | os.W_OK)
    input_args = ["-vaapi_device", "/dev/dri/renderD128"] if use_vaapi else []
    video_args = (
        ["-vf", "format=nv12,hwupload", "-c:v", "h264_vaapi", "-qp", "24"]
        if use_vaapi
        else ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23"]
    )
    report(
        "starting_transcoder",
        "Starting hardware video conversion" if use_vaapi else "Starting video conversion",
        progress=0.0,
        encoder="vaapi" if use_vaapi else "software",
    )
    ffmpeg = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostats", "-y",
        *input_args,
        "-fflags", "+genpts+discardcorrupt", "-f", "mpegts", "-i", "pipe:0",
        # Camera end_time is advisory on affected firmware; this cut is final.
        "-t", str(clip.duration),
        "-map", "0:v:0", "-map", "0:a:0?",
        *video_args,
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+empty_moov+default_base_moof+frag_keyframe",
        "-progress", "pipe:1", str(tmp),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    async def read_progress() -> None:
        assert ffmpeg.stdout is not None
        while True:
            line = await ffmpeg.stdout.readline()
            if not line:
                return
            name, _, value = line.decode(errors="replace").strip().partition("=")
            if name not in ("out_time_us", "out_time_ms"):
                continue
            try:
                seconds = int(value) / 1_000_000
            except ValueError:
                continue
            report(
                "processing", "Converting camera media for browser playback",
                progress=min(0.99, max(0.0, seconds / max(1, clip.duration))),
                mediaSeconds=round(seconds, 2), totalSeconds=clip.duration,
            )

    stderr_tail = bytearray()

    async def read_stderr() -> None:
        assert ffmpeg.stderr is not None
        while True:
            chunk = await ffmpeg.stderr.read(4096)
            if not chunk:
                return
            stderr_tail.extend(chunk)
            if len(stderr_tail) > 8192:
                del stderr_tail[:-8192]

    progress_task = asyncio.create_task(read_progress(), name=f"ffmpeg-progress-{clip.start}")
    stderr_task = asyncio.create_task(read_stderr(), name=f"ffmpeg-stderr-{clip.start}")
    loop = asyncio.get_running_loop()
    deadline = loop.time() + clip.duration * 2 + deadline_overhead
    bytes_video = 0

    try:
        report("requesting", "Requesting exactly one event from the camera", progress=0.0)
        await recording.start()
        async for response in recording.responses():
            if cancel.is_set():
                raise asyncio.CancelledError
            if loop.time() > deadline:
                raise ApiError(Code.GATEWAY_TIMEOUT, "camera playback timed out", status=504)
            if not response.plaintext:
                continue
            try:
                assert ffmpeg.stdin is not None
                ffmpeg.stdin.write(response.plaintext)
                bytes_video += len(response.plaintext)
                await ffmpeg.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                # Usually the exact -t limit. Validation distinguishes success.
                break
    finally:
        if ffmpeg.stdin is not None:
            try:
                ffmpeg.stdin.close()
            except Exception:
                pass
        try:
            await asyncio.wait_for(ffmpeg.wait(), timeout=30)
        except asyncio.TimeoutError:
            ffmpeg.kill()
            await ffmpeg.wait()
        try:
            await asyncio.wait_for(progress_task, timeout=2)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            progress_task.cancel()
        try:
            await asyncio.wait_for(stderr_task, timeout=2)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            stderr_task.cancel()
        try:
            await recording.close()
        except Exception:
            pass

    if not (tmp.exists() and tmp.stat().st_size > 1024):
        tmp.unlink(missing_ok=True)
        raise ApiError(
            Code.DOWNLOAD_EMPTY,
            f"download produced no usable output (bytes={bytes_video}B, ffmpeg rc={ffmpeg.returncode})",
            status=502,
            context={"ffmpeg_stderr_tail": stderr_tail.decode(errors="replace")[-500:], "ts_bytes": bytes_video},
        )

    report("verifying", "Checking event boundaries and media tracks", progress=0.99)
    duration, has_video = await _probe(tmp)
    if not has_video:
        tmp.unlink(missing_ok=True)
        raise ApiError(Code.DOWNLOAD_NO_VIDEO, "camera response contained no video", status=502)
    if duration > clip.duration + 0.75:
        tmp.unlink(missing_ok=True)
        raise ApiError(
            Code.FFMPEG_FAILED, "camera event exceeded its requested boundary", status=502,
            context={"expected_seconds": clip.duration, "actual_seconds": round(duration, 3)},
        )

    tmp.replace(out)
    report(
        "complete", "Event is ready", progress=1.0, cached=False,
        mediaSeconds=round(duration, 2), totalSeconds=clip.duration,
    )
    return out
