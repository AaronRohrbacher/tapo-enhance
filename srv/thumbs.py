"""Thumbnail generation. Three sources, in priority order:

  1. Cached recording on disk → ffmpeg extracts a frame locally. Free,
     instantaneous, doesn't touch the camera.
  2. Cached preview on disk → ditto.
  3. Camera → submit a SHORT (~2s) media-session pull through the gateway
     as a THUMB-priority job. Always runs after any pending download and
     never overlaps with the live stream.

Public surface:
  - `extract_from_local(clip, paths)`: try sources 1 + 2, return path or None.
  - `make_camera_runner(clip, paths)`: returns a runner suitable for
    `gateway.submit_thumb(...)`.
  - `ThumbBackfill`: a queue + worker that drains "want a thumb" requests
    one at a time, preferring the local source when possible. Runs
    forever as a background asyncio task.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from .errors import ApiError, Code
from .media_session import build_playback_request, get_user_id, normalize_response, open_session
from .recordings import Clip, Paths

log = logging.getLogger(__name__)

THUMB_PULL_SECONDS = 2


# ── Source 1/2: extract from local mp4 ─────────────────────────────────────


async def extract_from_local(clip: Clip, paths: Paths) -> Path | None:
    """If the full clip is already cached, generate the thumb from it. No
    camera contact; cheap. Returns the thumb path if successful, else None.

    Tries the recording first, then the preview as a fallback (which is
    smaller but already H.264 if it exists)."""
    out = paths.thumb(clip)
    if out.exists() and out.stat().st_size > 100:
        return out
    for src in (paths.recording(clip), paths.preview(clip)):
        if src.exists() and src.stat().st_size > 1024:
            out.parent.mkdir(parents=True, exist_ok=True)
            tmp = out.with_suffix(".tmp.jpg")
            cmd = [
                "ffmpeg", "-y", "-ss", "1", "-i", str(src),
                "-frames:v", "1", "-q:v", "4", "-vf", "scale=320:-2",
                str(tmp),
            ]
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                rc = await asyncio.wait_for(proc.wait(), timeout=15)
            except asyncio.TimeoutError:
                proc.kill()
                rc = -1
            if rc == 0 and tmp.exists() and tmp.stat().st_size > 100:
                tmp.replace(out)
                return out
            if tmp.exists():
                tmp.unlink()
    return None


# ── Source 3: short pull through the gateway ───────────────────────────────


def make_camera_runner(clip: Clip, paths: Paths):
    async def runner(tapo: Any, _cancel: asyncio.Event) -> Path:
        return await _short_pull(tapo, clip, paths)

    return runner


async def _short_pull(tapo: Any, clip: Clip, paths: Paths) -> Path:
    from pytapo.media_stream._utils import StreamType

    out = paths.thumb(clip)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp.jpg")
    if tmp.exists():
        tmp.unlink()

    pull_end = clip.start + THUMB_PULL_SECONDS
    user_id = await get_user_id(tapo)
    session = await open_session(tapo, StreamType.Download)
    try:
        session.set_window_size(50)
    except Exception:
        pass

    ffmpeg = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y",
        "-fflags", "+genpts+discardcorrupt",
        "-f", "mpegts", "-i", "pipe:0",
        "-map", "0:v:0", "-frames:v", "1",
        "-vf", "scale=320:-2", "-q:v", "4",
        str(tmp),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )

    deadline = asyncio.get_event_loop().time() + THUMB_PULL_SECONDS * 3 + 15
    bytes_video = 0
    try:
        await session.start()
        req = build_playback_request(user_id, clip.start, pull_end)
        try:
            transceive_iter = session.transceive(req, "application/json", no_data_timeout=5)
        except TypeError:
            transceive_iter = session.transceive(req, "application/json")

        async for raw in transceive_iter:
            if asyncio.get_event_loop().time() > deadline:
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
            await asyncio.wait_for(ffmpeg.wait(), timeout=10)
        except asyncio.TimeoutError:
            ffmpeg.kill()
            await ffmpeg.wait()
        try:
            await session.close()
        except Exception:
            pass

    if tmp.exists() and tmp.stat().st_size > 100:
        tmp.replace(out)
        return out
    if tmp.exists():
        tmp.unlink()
    raise ApiError(
        Code.DOWNLOAD_EMPTY,
        f"thumb pull produced no jpg (video_bytes={bytes_video})",
        status=502,
    )


# ── Background backfill queue ──────────────────────────────────────────────


class ThumbBackfill:
    """Drains pending thumbs, one at a time, with state per clip so the UI
    can poll. Tries local extraction first; falls back to a camera pull
    through the supplied gateway. Failures retry once, then stick."""

    def __init__(self, paths: Paths, gateway):
        self.paths = paths
        self.gateway = gateway
        self._q: asyncio.Queue = asyncio.Queue()
        self._state: dict[str, dict] = {}
        self._task: asyncio.Task | None = None

    def state(self, key: str) -> dict | None:
        return self._state.get(key)

    def all_state_for_date(self, date: str) -> list[dict]:
        prefix = date + "/"
        return [{"key": k, **st} for k, st in self._state.items() if k.startswith(prefix)]

    def request(self, clip: Clip) -> str:
        """Mark a clip as wanting a thumb; enqueue if not already in flight."""
        key = clip.key
        existing = self._state.get(key)
        if existing and existing["status"] in ("queued", "running", "ready"):
            return existing["status"]
        if existing and existing["status"] == "failed" and existing["attempts"] >= 2:
            return "failed"
        self._state[key] = {
            "status": "queued",
            "attempts": (existing or {}).get("attempts", 0),
            "error": None,
            "clip": clip,
        }
        self._q.put_nowait(clip)
        return "queued"

    def retry_failed_for_date(self, date: str) -> int:
        """Reset all failed thumbs for `date` back to the queue."""
        n = 0
        for k, st in list(self._state.items()):
            if not k.startswith(date + "/"):
                continue
            if st["status"] != "failed":
                continue
            st["status"] = "queued"
            st["attempts"] = 0
            st["error"] = None
            self._q.put_nowait(st["clip"])
            n += 1
        return n

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="thumb-backfill")

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def _loop(self) -> None:
        try:
            while True:
                clip = await self._q.get()
                key = clip.key
                st = self._state.get(key)
                if not st or st["status"] in ("ready", "failed"):
                    continue
                # Cheap check: maybe the file appeared in the meantime.
                if self.paths.thumb(clip).exists():
                    st["status"] = "ready"
                    continue
                st["status"] = "running"
                st["attempts"] += 1
                try:
                    local = await extract_from_local(clip, self.paths)
                    if local is not None:
                        st["status"] = "ready"
                        st["error"] = None
                        continue
                    await self.gateway.submit_thumb(
                        clip, make_camera_runner(clip, self.paths)
                    )
                    if self.paths.thumb(clip).exists():
                        st["status"] = "ready"
                        st["error"] = None
                    else:
                        raise RuntimeError("camera pull returned no jpg")
                except Exception as e:
                    st["error"] = f"{type(e).__name__}: {e}"
                    if st["attempts"] >= 2:
                        st["status"] = "failed"
                    else:
                        st["status"] = "queued"
                        await self._q.put(clip)
        except asyncio.CancelledError:
            return
