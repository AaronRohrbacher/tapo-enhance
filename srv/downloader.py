"""Download a saved clip from the camera SD card to local disk.

Pulls H.265 video as MPEG-TS from the camera media session, plus the
G.711 audio payload that the camera delivers on a *separate* RTP channel
(NOT inside the TS — the previous app silently dropped audio because of
this), and runs them through a single ffmpeg pass that:
  - transcodes video to H.264 (browsers won't decode HEVC natively),
  - transcodes audio to AAC,
  - writes a fragmented mp4 (`+empty_moov+frag_keyframe`) so the file is
    valid mid-write (no end-of-process moov rewrite, no risk of corrupt
    orphans on early termination).

The runner is a coroutine the gateway calls with an open Tapo client. We
don't open a second tapo session here.

Failure modes are loud and structured: empty / truncated outputs delete
the tmp file and raise ApiError(DOWNLOAD_EMPTY); ffmpeg failures raise
ApiError(FFMPEG_FAILED); auth errors raise ApiError(CAMERA_AUTH).
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any

from .audio import G711_FRAME_BYTES, AudioFormat, parse_audio_config
from .errors import ApiError, Code
from .media_session import (
    build_playback_request,
    get_user_id,
    normalize_response,
    open_session,
)
from .recordings import Clip, Paths


async def detect_audio_format(tapo: Any) -> AudioFormat:
    try:
        cfg = await asyncio.get_event_loop().run_in_executor(None, tapo.getAudioConfig)
    except Exception:
        return AudioFormat.default()
    return parse_audio_config(cfg)


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

    fmt = await detect_audio_format(tapo)
    user_id = await get_user_id(tapo)
    session = await open_session(tapo, StreamType.Download)
    try:
        session.set_window_size(200)
    except Exception:
        pass

    audio_r, audio_w = os.pipe()
    os.set_inheritable(audio_r, True)

    ffmpeg = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y",
        "-fflags", "+genpts+discardcorrupt",
        "-f", "mpegts", "-i", "pipe:0",
        "-f", fmt.ffmpeg_format, "-ar", str(fmt.sample_rate), "-i", f"/dev/fd/{audio_r}",
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+empty_moov+default_base_moof+frag_keyframe",
        str(tmp),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        pass_fds=(audio_r,),
    )
    os.close(audio_r)  # parent doesn't need the read end

    deadline = asyncio.get_event_loop().time() + clip.duration * 2 + deadline_overhead
    bytes_video = 0
    bytes_audio = 0
    audio_closed = False

    async def feed_audio(payload: bytes) -> int:
        """Write G.711 payload in 160-byte frames; return bytes written."""
        nonlocal audio_closed
        if audio_closed:
            return 0
        # Re-frame: align to 160-byte boundaries to keep ffmpeg happy.
        n = (len(payload) // G711_FRAME_BYTES) * G711_FRAME_BYTES
        if n == 0:
            return 0
        try:
            os.write(audio_w, payload[:n])
            return n
        except OSError:
            audio_closed = True
            return 0

    # If the camera ships zero audio for the first 20s, close the pipe so
    # ffmpeg doesn't block forever waiting for a second input that's not
    # coming. Mirrors a quirk we hit on the real D225.
    audio_state = {"bytes": 0}

    def _close_audio_if_silent():
        nonlocal audio_closed
        if audio_state["bytes"] == 0 and not audio_closed:
            try:
                os.close(audio_w)
            except OSError:
                pass
            audio_closed = True

    loop = asyncio.get_event_loop()
    silent_watchdog = loop.call_later(20, _close_audio_if_silent)

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
            if r.audio and not audio_closed:
                wrote = await feed_audio(r.audio)
                bytes_audio += wrote
                audio_state["bytes"] = bytes_audio
            if r.plaintext:
                try:
                    ffmpeg.stdin.write(r.plaintext)
                    bytes_video += len(r.plaintext)
                    await ffmpeg.stdin.drain()
                except (BrokenPipeError, ConnectionResetError):
                    break
    finally:
        try:
            silent_watchdog.cancel()
        except Exception:
            pass
        try:
            ffmpeg.stdin.close()
        except Exception:
            pass
        if not audio_closed:
            try:
                os.close(audio_w)
            except OSError:
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
            f"download produced no usable output (video={bytes_video}B, audio={bytes_audio}B, ffmpeg rc={ffmpeg.returncode})",
            status=502,
            context={"ffmpeg_stderr_tail": err, "video_bytes": bytes_video, "audio_bytes": bytes_audio},
        )

    tmp.replace(out)
    return out
