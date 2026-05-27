"""Single-frame snapshot. Two paths:
  - if the live HLS stream is running, extract a frame from the most recent
    completed segment (cheap, never touches the camera).
  - otherwise return CAMERA_OFFLINE so the UI can prompt the user to start
    live first.

We deliberately don't open a fresh media session just for a snapshot —
that would race with whatever the gateway is doing."""

from __future__ import annotations

import asyncio
from pathlib import Path

from .errors import ApiError, Code
from .recordings import Paths


async def from_recent_segment(paths: Paths) -> bytes:
    segs = sorted(paths.stream.glob("seg_*.ts"), key=lambda p: p.stat().st_mtime)
    if len(segs) < 1:
        raise ApiError(
            Code.CAMERA_OFFLINE,
            "No live stream running. Start the live feed first.",
            status=503,
        )
    # Skip the very last (still being written) when possible.
    seg = segs[-2] if len(segs) >= 2 else segs[-1]
    if seg.stat().st_size < 4096:
        raise ApiError(
            Code.CAMERA_OFFLINE,
            "Live stream segment too small to extract a frame.",
            status=503,
        )
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-i", str(seg),
        "-frames:v", "1", "-q:v", "3",
        "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        data, _ = await asyncio.wait_for(proc.communicate(), timeout=8)
    except asyncio.TimeoutError:
        proc.kill()
        raise ApiError(Code.GATEWAY_TIMEOUT, "snapshot timeout", status=504)
    if not data:
        raise ApiError(Code.INTERNAL, "snapshot ffmpeg produced no bytes", status=502)
    return data
