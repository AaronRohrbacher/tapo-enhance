"""Download a saved clip from the camera SD card to local disk.

Pulls MPEG-TS chunks from the camera media session (video + opus audio,
both inside the TS — the D225 firmware multiplexes them, despite older
docs that claim audio comes on a separate RTP channel) and runs them
through a single ffmpeg pass that:
  - transcodes video from HEVC → H.264 (browsers won't decode HEVC natively),
  - transcodes audio from opus → AAC (broader browser support),
  - writes a fragmented mp4 (`+empty_moov+frag_keyframe`) so the file is
    valid mid-write — no end-of-process moov rewrite, no risk of corrupt
    orphans on early termination.

The runner is a coroutine the gateway calls with an open Tapo client. We
don't open a second tapo session here.

Failure modes are loud and structured: empty / truncated outputs delete
the tmp file and raise ApiError(DOWNLOAD_EMPTY); auth errors raise
ApiError(CAMERA_AUTH) via the route's _classify helper.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from .errors import ApiError, Code
from .media_session import (
    build_playback_request,
    get_user_id,
    normalize_response,
    open_session,
)
from .recordings import Clip, Paths


def make_runner(clip: Clip, paths: Paths, *, deadline_overhead: int = 60):
    """Returns a coroutine compatible with CameraGateway's `Runner` shape."""

    async def runner(tapo: Any, _cancel: asyncio.Event) -> Path:
        return await _download(tapo, clip, paths, deadline_overhead)

    return runner


async def _download(tapo: Any, clip: Clip, paths: Paths, deadline_overhead: int) -> Path:
    from pytapo.media_stream._utils import StreamType

    out = paths.recording(clip)
    if out.exists() and out.stat().st_size > 0:
        return out

    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp.mp4")
    if tmp.exists():
        tmp.unlink()

    user_id = await get_user_id(tapo)
    session = await open_session(tapo, StreamType.Download)
    try:
        session.set_window_size(200)
    except Exception:
        pass

    ffmpeg = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y",
        "-fflags", "+genpts+discardcorrupt",
        "-f", "mpegts", "-i", "pipe:0",
        "-map", "0:v:0", "-map", "0:a:0?",  # audio is optional — ? = don't fail if absent
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+empty_moov+default_base_moof+frag_keyframe",
        str(tmp),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )

    loop = asyncio.get_event_loop()
    deadline = loop.time() + clip.duration * 2 + deadline_overhead
    bytes_video = 0

    try:
        await session.start()
        req = build_playback_request(user_id, clip.start, clip.end)
        try:
            transceive_iter = session.transceive(req, "application/json", no_data_timeout=5)
        except TypeError:
            transceive_iter = session.transceive(req, "application/json")

        async for raw in transceive_iter:
            if loop.time() > deadline:
                break
            r = normalize_response(raw)
            if r.finished:
                break
            if r.plaintext:
                try:
                    ffmpeg.stdin.write(r.plaintext)
                    bytes_video += len(r.plaintext)
                    await ffmpeg.stdin.drain()
                except (BrokenPipeError, ConnectionResetError):
                    break
    finally:
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
            await session.close()
        except Exception:
            pass

    if not (tmp.exists() and tmp.stat().st_size > 1024):
        try:
            err = (await ffmpeg.stderr.read()).decode(errors="replace")[-300:]
        except Exception:
            err = ""
        if tmp.exists():
            tmp.unlink()
        raise ApiError(
            Code.DOWNLOAD_EMPTY,
            f"download produced no usable output (bytes={bytes_video}B, ffmpeg rc={ffmpeg.returncode})",
            status=502,
            context={"ffmpeg_stderr_tail": err, "ts_bytes": bytes_video},
        )

    tmp.replace(out)
    return out
