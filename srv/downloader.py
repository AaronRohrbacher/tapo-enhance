"""Efficient, bounded retrieval of SD-card recordings.

Camera bytes stream directly through ffmpeg; they are never accumulated in
Python memory or repeatedly probed while arriving. Some firmware continues
into later events, so ffmpeg applies a hard duration cut and the result is
validated before publication.

Batch callers can use ``make_fetch_runner`` and ``ConversionQueue`` to release
the serialized camera lane after a fast stream-copy. Browser conversion then
runs independently while the next clip downloads.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Callable

from .errors import ApiError, Code
from .media import media_wait
from .recordings import Clip, Paths


Progress = Callable[[dict], None]


class ConversionQueue:
    """Bounded conversion work shared by DVR and bulk-download pipelines."""

    def __init__(self, workers: int = 1):
        self._slots = asyncio.Semaphore(max(1, workers))
        self._tasks: set[asyncio.Task] = set()

    def submit(
        self, clip: Clip, paths: Paths, staged: Path, *,
        destination: Path | None = None, on_progress: Progress | None = None,
        operation: str = "download",
    ) -> asyncio.Task:
        async def run() -> Path:
            async with self._slots:
                return await convert_staged(
                    clip, paths, staged, destination=destination,
                    on_progress=on_progress, operation=operation,
                )

        task = asyncio.create_task(run(), name=f"convert-{clip.start}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task


def make_fetch_runner(
    clip: Clip, paths: Paths, *, deadline_overhead: int = 60,
    on_progress: Progress | None = None, operation: str = "download",
):
    """Return a gateway runner that retrieves one duration-limited MPEG-TS."""

    async def runner(tapo: Any, cancel: asyncio.Event) -> Path:
        try:
            return await _fetch_staged(
                tapo, clip, paths, deadline_overhead, cancel=cancel,
                on_progress=on_progress, operation=operation,
            )
        except BaseException as exc:
            phase = "cancelled" if isinstance(exc, asyncio.CancelledError) else "error"
            message = "Operation cancelled" if phase == "cancelled" else (str(exc) or type(exc).__name__)
            _report(on_progress, {
                "type": "operation", "operation": operation, "key": clip.key,
                "stage": "download", "phase": phase, "message": message,
                "error": message,
            })
            raise

    return runner


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


async def cached_media_is_valid(path: Path, expected_duration: int) -> bool:
    """Return true only for a playable, substantially complete cached clip."""
    if not path.exists() or path.stat().st_size <= 1024:
        return False
    try:
        duration, has_video = await _probe(path)
    except (ApiError, OSError):
        return False
    tolerance = max(2.0, expected_duration * 0.1)
    return has_video and duration >= max(0.1, expected_duration - tolerance)


async def _fetch_staged(
    tapo: Any, clip: Clip, paths: Paths, deadline_overhead: int, *,
    cancel: asyncio.Event, on_progress: Progress | None, operation: str,
) -> Path:
    """Copy camera MPEG-TS to disk, cutting at the requested clip duration."""
    from pytapo.media_stream.recording_stream import RecordingStream

    out = paths.recording(clip)
    out.parent.mkdir(parents=True, exist_ok=True)
    staged = out.with_suffix(".download.ts")
    staged.unlink(missing_ok=True)
    base = {"type": "operation", "operation": operation, "key": clip.key,
            "stage": "download"}

    def report(phase: str, message: str, **extra) -> None:
        _report(on_progress, {**base, "phase": phase, "message": message, **extra})

    report("connecting", "Opening a camera playback session", progress=0.0)
    recording = RecordingStream(
        tapo, clip.start, clip.end, event_type=(2,), window_size=200,
        no_data_timeout=8,
    )
    ffmpeg = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostats", "-y",
        "-fflags", "+genpts+discardcorrupt", "-f", "mpegts", "-i", "pipe:0",
        "-t", str(clip.duration), "-map", "0:v:0", "-map", "0:a:0?",
        "-c", "copy", "-f", "mpegts", "-progress", "pipe:1", str(staged),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stderr_tail = bytearray()

    async def read_progress() -> None:
        assert ffmpeg.stdout is not None
        while line := await ffmpeg.stdout.readline():
            name, _, value = line.decode(errors="replace").strip().partition("=")
            if name not in ("out_time_us", "out_time_ms"):
                continue
            try:
                seconds = int(value) / 1_000_000
            except ValueError:
                continue
            report("downloading", "Downloading camera media",
                   progress=min(0.99, max(0.0, seconds / max(1, clip.duration))),
                   mediaSeconds=round(seconds, 2), totalSeconds=clip.duration)

    async def read_stderr() -> None:
        assert ffmpeg.stderr is not None
        while chunk := await ffmpeg.stderr.read(4096):
            stderr_tail.extend(chunk)
            if len(stderr_tail) > 8192:
                del stderr_tail[:-8192]

    progress_task = asyncio.create_task(read_progress())
    stderr_task = asyncio.create_task(read_stderr())
    loop = asyncio.get_running_loop()
    deadline = loop.time() + clip.duration * 2 + deadline_overhead
    bytes_video = 0
    try:
        report("requesting", "Requesting exactly one event from the camera", progress=0.0)
        await media_wait(recording.start(), cancel, timeout=15)
        responses = recording.responses().__aiter__()
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise ApiError(Code.GATEWAY_TIMEOUT, "camera playback timed out", status=504)
            try:
                response = await media_wait(responses.__anext__(), cancel, timeout=min(10, remaining))
            except StopAsyncIteration:
                break
            except asyncio.TimeoutError as exc:
                raise ApiError(Code.GATEWAY_TIMEOUT, "camera stopped sending playback data", status=504) from exc
            if cancel.is_set():
                raise asyncio.CancelledError
            if not response.plaintext:
                continue
            try:
                assert ffmpeg.stdin is not None
                ffmpeg.stdin.write(response.plaintext)
                bytes_video += len(response.plaintext)
                await media_wait(ffmpeg.stdin.drain(), cancel, timeout=10)
            except (BrokenPipeError, ConnectionResetError):
                break
    finally:
        if ffmpeg.stdin is not None:
            ffmpeg.stdin.close()
        try:
            await asyncio.wait_for(ffmpeg.wait(), timeout=30)
        except asyncio.TimeoutError:
            ffmpeg.kill()
            await ffmpeg.wait()
        await asyncio.gather(progress_task, stderr_task, return_exceptions=True)
        try:
            await asyncio.wait_for(recording.close(), timeout=5)
        except (asyncio.TimeoutError, Exception):
            pass

    if not (staged.exists() and staged.stat().st_size > 1024):
        staged.unlink(missing_ok=True)
        raise ApiError(
            Code.DOWNLOAD_EMPTY,
            f"download produced no usable output (bytes={bytes_video}B, ffmpeg rc={ffmpeg.returncode})",
            status=502,
            context={"ffmpeg_stderr_tail": stderr_tail.decode(errors="replace")[-500:],
                     "ts_bytes": bytes_video},
        )
    report("downloaded", "Download complete; queued for conversion", progress=1.0)
    return staged


async def convert_staged(
    clip: Clip, paths: Paths, staged: Path, *, destination: Path | None = None,
    on_progress: Progress | None = None, operation: str = "download",
) -> Path:
    """Convert a staged MPEG-TS without occupying the camera gateway."""
    out = destination or paths.recording(clip)
    tmp = out.with_suffix(".tmp.mp4")
    tmp.unlink(missing_ok=True)
    base = {"type": "operation", "operation": operation, "key": clip.key,
            "stage": "conversion"}

    def report(phase: str, message: str, **extra) -> None:
        _report(on_progress, {**base, "phase": phase, "message": message, **extra})

    use_vaapi = os.access("/dev/dri/renderD128", os.R_OK | os.W_OK)
    input_args = ["-vaapi_device", "/dev/dri/renderD128"] if use_vaapi else []
    video_args = (["-vf", "format=nv12,hwupload", "-c:v", "h264_vaapi", "-qp", "24"]
                  if use_vaapi else ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23"])
    report("converting", "Converting downloaded media for browser playback", progress=0.0)
    ffmpeg = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostats", "-y",
        *input_args, "-fflags", "+genpts+discardcorrupt", "-f", "mpegts",
        "-i", str(staged), "-t", str(clip.duration), "-map", "0:v:0",
        "-map", "0:a:0?", *video_args, "-c:a", "aac", "-b:a", "128k",
        "-avoid_negative_ts", "make_zero", "-movflags", "+faststart",
        "-progress", "pipe:1", str(tmp), stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stderr_tail = bytearray()

    async def read_progress() -> None:
        assert ffmpeg.stdout is not None
        while line := await ffmpeg.stdout.readline():
            name, _, value = line.decode(errors="replace").strip().partition("=")
            if name not in ("out_time_us", "out_time_ms"):
                continue
            try:
                seconds = int(value) / 1_000_000
            except ValueError:
                continue
            report("converting", "Converting downloaded media for browser playback",
                   progress=min(0.99, max(0.0, seconds / max(1, clip.duration))),
                   mediaSeconds=round(seconds, 2), totalSeconds=clip.duration)

    progress_task = asyncio.create_task(read_progress())
    assert ffmpeg.stderr is not None
    while chunk := await ffmpeg.stderr.read(4096):
        stderr_tail.extend(chunk)
        if len(stderr_tail) > 8192:
            del stderr_tail[:-8192]
    await ffmpeg.wait()
    await progress_task
    completed = False
    try:
        if ffmpeg.returncode != 0 or not (tmp.exists() and tmp.stat().st_size > 1024):
            raise ApiError(Code.FFMPEG_FAILED, "video conversion failed", status=502,
                           context={"ffmpeg_stderr_tail": stderr_tail.decode(errors="replace")[-500:]})
        duration, has_video = await _probe(tmp)
        if not has_video:
            raise ApiError(Code.DOWNLOAD_NO_VIDEO, "camera response contained no video", status=502)
        tmp.replace(out)
        completed = True
        report("complete", "Event is ready", progress=1.0,
               mediaSeconds=round(duration, 2), totalSeconds=clip.duration)
        return out
    except asyncio.CancelledError:
        # Keep the complete staged download so startup reconciliation can
        # resume conversion without asking the camera for the clip again.
        tmp.unlink(missing_ok=True)
        raise
    except BaseException as exc:
        tmp.unlink(missing_ok=True)
        staged.unlink(missing_ok=True)
        message = str(exc) or type(exc).__name__
        report("error", message, error=message)
        raise
    finally:
        if completed:
            staged.unlink(missing_ok=True)


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

    if await cached_media_is_valid(out, clip.duration):
        report("complete", "Already available locally", progress=1.0, cached=True)
        return out
    if out.exists():
        report("verifying", "Cached copy is corrupt; retrieving a replacement", progress=0.0)

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
        # This is a completed archive file, not a progressively served stream.
        # Normalized timestamps and a regular MP4 avoid inflated duration
        # metadata seen with camera MPEG-TS timestamps in fragmented MP4s.
        "-avoid_negative_ts", "make_zero", "-movflags", "+faststart",
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
        await media_wait(recording.start(), cancel, timeout=15)
        responses = recording.responses().__aiter__()
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise ApiError(Code.GATEWAY_TIMEOUT, "camera playback timed out", status=504)
            try:
                response = await media_wait(
                    responses.__anext__(), cancel, timeout=min(10.0, remaining),
                )
            except StopAsyncIteration:
                break
            except asyncio.TimeoutError as exc:
                raise ApiError(
                    Code.GATEWAY_TIMEOUT, "camera stopped sending playback data", status=504,
                ) from exc
            if cancel.is_set():
                raise asyncio.CancelledError
            if not response.plaintext:
                continue
            try:
                assert ffmpeg.stdin is not None
                ffmpeg.stdin.write(response.plaintext)
                bytes_video += len(response.plaintext)
                await media_wait(ffmpeg.stdin.drain(), cancel, timeout=10)
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
            await asyncio.wait_for(recording.close(), timeout=5)
        except (asyncio.TimeoutError, Exception):
            pass

    if not (tmp.exists() and tmp.stat().st_size > 1024):
        tmp.unlink(missing_ok=True)
        raise ApiError(
            Code.DOWNLOAD_EMPTY,
            f"download produced no usable output (bytes={bytes_video}B, ffmpeg rc={ffmpeg.returncode})",
            status=502,
            context={"ffmpeg_stderr_tail": stderr_tail.decode(errors="replace")[-500:], "ts_bytes": bytes_video},
        )

    report("verifying", "Checking downloaded media tracks", progress=0.99)
    duration, has_video = await _probe(tmp)
    if not has_video:
        tmp.unlink(missing_ok=True)
        raise ApiError(Code.DOWNLOAD_NO_VIDEO, "camera response contained no video", status=502)
    # ffmpeg's output-side -t above is the boundary. Container duration can be
    # longer by timestamp offset or codec-frame rounding and is not evidence
    # that media beyond the requested event was written.

    tmp.replace(out)
    report(
        "complete", "Event is ready", progress=1.0, cached=False,
        mediaSeconds=round(duration, 2), totalSeconds=clip.duration,
    )
    return out
