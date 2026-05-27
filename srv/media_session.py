"""Thin wrapper over pytapo's media session protocol.

The gateway lends an authenticated `tapo` client to a runner; runners use
this module to actually pump bytes from / send requests to the camera.

Why this lives in its own module: the runner needs to call `getMediaSession`
in a worker thread (pytapo's AsyncHandler runs `loop.run_until_complete`
internally and that explodes if invoked from the main asyncio loop), and the
JSON request envelope for playback / live-preview is fiddly enough that
duplicating it across downloader.py, live.py, and thumbs.py would invite
drift. Centralising it here also gives tests one mock surface.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class TsResponse:
    plaintext: bytes | None
    audio: bytes
    mimetype: str
    finished: bool


def build_playback_request(
    user_id: str,
    start_time: int,
    end_time: int,
    *,
    scale: str = "1/1",
    channels: tuple[int, ...] = (0, 1),
    event_type: tuple[int, ...] = (1, 2),
) -> str:
    return json.dumps({
        "type": "request",
        "seq": 1,
        "params": {
            "playback": {
                "client_id": user_id,
                "channels": list(channels),
                "scale": scale,
                "start_time": str(start_time),
                "end_time": str(end_time),
                "event_type": list(event_type),
            },
            "method": "get",
        },
    })


def build_preview_request(*, channels: tuple[int, ...] = (0,), resolution: str = "HD") -> str:
    return json.dumps({
        "type": "request",
        "seq": 1,
        "params": {
            "preview": {
                "audio": ["default"],
                "channels": list(channels),
                "resolutions": [resolution],
            },
            "method": "get",
        },
    })


async def open_session(tapo: Any, stream_type) -> Any:
    """Run pytapo's getMediaSession in a worker thread (it nests an event loop)."""
    return await asyncio.get_event_loop().run_in_executor(None, tapo.getMediaSession, stream_type)


async def get_user_id(tapo: Any) -> str:
    return await asyncio.get_event_loop().run_in_executor(None, tapo.getUserID)


def normalize_response(resp) -> TsResponse:
    """Reduce pytapo's response object to our internal TsResponse. We tolerate
    the slightly different shapes the library has used historically."""
    mt = getattr(resp, "mimetype", "") or ""
    plain = getattr(resp, "plaintext", None)
    audio = getattr(resp, "audioPayload", None) or b""
    finished = False
    if mt == "application/json" and plain:
        try:
            txt = plain.decode() if isinstance(plain, (bytes, bytearray)) else plain
            j = json.loads(txt)
            p = j.get("params", {})
            if p.get("event_type") == "stream_status" and p.get("status") == "finished":
                finished = True
        except Exception:
            pass
    return TsResponse(
        plaintext=plain if mt == "video/mp2t" else None,
        audio=bytes(audio) if mt == "video/mp2t" else b"",
        mimetype=mt,
        finished=finished,
    )
