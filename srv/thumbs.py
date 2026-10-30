"""Thumbnail retrieval. Three sources, in priority order:

  1. Cached recording on disk → ffmpeg extracts a frame locally. Free,
     instantaneous, doesn't touch the camera.
  2. Cached preview on disk → ditto.
  3. Camera → request its native event JPEG through pytapo. No recording is
     retrieved or decoded. The gateway serializes this with other camera
     media operations.

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
import logging
from pathlib import Path
from typing import Any

from pytapo.media_stream.recording_thumbnail import RecordingThumbnail

from .errors import ApiError, Code
from .media import media_wait
from .recordings import Clip, Paths

log = logging.getLogger(__name__)


def valid_thumbnail(path: Path) -> bool:
    """Reject partial/HTML responses that are large enough to fool a size check."""
    try:
        if path.stat().st_size <= 100:
            return False
        with path.open("rb") as fh:
            start = fh.read(2)
            fh.seek(-2, 2)
            end = fh.read(2)
        return start == b"\xff\xd8" and end == b"\xff\xd9"
    except OSError:
        return False

# ── Source 1/2: extract from local mp4 ─────────────────────────────────────


async def extract_from_local(clip: Clip, paths: Paths) -> Path | None:
    """If the full clip is already cached, generate the thumb from it. No
    camera contact; cheap. Returns the thumb path if successful, else None.

    Tries the recording first, then the preview as a fallback (which is
    smaller but already H.264 if it exists)."""
    out = paths.thumb(clip)
    if valid_thumbnail(out):
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
                rc = -1
            finally:
                if proc.returncode is None:
                    proc.kill()
                    await proc.wait()
            if rc == 0 and valid_thumbnail(tmp):
                tmp.replace(out)
                return out
            if tmp.exists():
                tmp.unlink()
    return None


# ── Source 3: camera-native recording JPEG ─────────────────────────────────


def make_camera_runner(clip: Clip, paths: Paths):
    async def runner(tapo: Any, _cancel: asyncio.Event) -> Path:
        return await _native_thumbnail(tapo, clip, paths, _cancel)

    return runner


async def _native_thumbnail(tapo: Any, clip: Clip, paths: Paths, cancel: asyncio.Event) -> Path:
    out = paths.thumb(clip)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp.jpg")
    tmp.unlink(missing_ok=True)
    thumbnail = RecordingThumbnail(tapo, clip.start, clip.end)
    try:
        await media_wait(thumbnail.download(tmp, overwriteFiles=True), cancel, timeout=20)
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        raise ApiError(
            Code.DOWNLOAD_EMPTY,
            "camera returned no native recording thumbnail",
            status=502,
            context={"reason": str(exc)},
        ) from exc

    if valid_thumbnail(tmp):
        tmp.replace(out)
        return out
    tmp.unlink(missing_ok=True)
    raise ApiError(
        Code.DOWNLOAD_EMPTY,
        "camera returned an empty native recording thumbnail",
        status=502,
    )


# ── Background backfill queue ──────────────────────────────────────────────


class ThumbBackfill:
    """Drains pending thumbs, one at a time, with state per clip so the UI
    can poll. Tries local extraction first; falls back to a camera pull
    through the supplied gateway. Failures retry once, then stick."""

    def __init__(self, paths: Paths, gateway, *, on_event=None):
        self.paths = paths
        self.gateway = gateway
        self._q: asyncio.Queue = asyncio.Queue()
        self._state: dict[str, dict] = {}
        self._task: asyncio.Task | None = None
        self._on_event = on_event

    def _event(self, key: str) -> dict:
        st = self._state.get(key) or {}
        return {
            "type": "thumb", "key": key, "status": st.get("status"),
            "error": st.get("error"), "message": st.get("message"),
            "attempt": st.get("attempts", 0),
        }

    def snapshot(self) -> list[dict]:
        return [self._event(key) for key in self._state]

    def _emit(self, key: str) -> None:
        if self._on_event is not None:
            self._on_event(self._event(key))

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
            "message": "Waiting for camera access",
            "clip": clip,
        }
        self._q.put_nowait(clip)
        self._emit(key)
        return "queued"

    def requeue(self, clip: Clip) -> None:
        """Force a clip back into the queue, regardless of prior status. Used
        when the thumb file was deleted out-of-band (cache clear) and the
        in-memory `ready` state is now lying."""
        key = clip.key
        self._state[key] = {
            "status": "queued",
            "attempts": 0,
            "error": None,
            "message": "Waiting for camera access",
            "clip": clip,
        }
        self._q.put_nowait(clip)
        self._emit(key)

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
            st["message"] = "Waiting for camera access"
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
                if valid_thumbnail(self.paths.thumb(clip)):
                    st["status"] = "ready"
                    st["message"] = "Thumbnail already cached"
                    self._emit(key)
                    continue
                st["status"] = "running"
                st["attempts"] += 1
                st["message"] = "Retrieving the first frame only"
                self._emit(key)
                try:
                    local = await extract_from_local(clip, self.paths)
                    if local is not None:
                        st["status"] = "ready"
                        st["error"] = None
                        st["message"] = "Thumbnail ready from local media"
                        self._emit(key)
                        continue
                    await self.gateway.submit_thumb(
                        clip, make_camera_runner(clip, self.paths)
                    )
                    if valid_thumbnail(self.paths.thumb(clip)):
                        st["status"] = "ready"
                        st["error"] = None
                        st["message"] = "Thumbnail ready"
                    else:
                        raise RuntimeError("camera pull returned no jpg")
                except Exception as e:
                    st["error"] = f"{type(e).__name__}: {e}"
                    if st["attempts"] >= 2:
                        st["status"] = "failed"
                        st["message"] = "Thumbnail failed"
                    else:
                        st["status"] = "queued"
                        st["message"] = "Retrying thumbnail"
                        await self._q.put(clip)
                self._emit(key)
        except asyncio.CancelledError:
            return
