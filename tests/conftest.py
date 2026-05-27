"""Shared pytest fixtures.

Two categories of test depend on different fakes:

  * Pure unit tests use the FakeTapo client (in-memory, no I/O).
  * Integration tests that exercise ffmpeg use FakeMediaSession, which
    yields a real H.265+AAC MPEG-TS plus a synthetic G.711 audio stream
    — the same shape the D225 actually emits over its proprietary
    media protocol. This means the full downloader path runs end-to-end
    (real ffmpeg subprocess, real on-disk write, real ffprobe verify)
    without needing live hardware.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture
def cache_dir(tmp_path) -> Path:
    d = tmp_path / "cache"
    d.mkdir()
    return d


@pytest.fixture
def paths(cache_dir):
    from srv.recordings import Paths
    p = Paths(cache_dir)
    p.ensure()
    return p


# ── FakeTapo ───────────────────────────────────────────────────────────────


class FakeTapo:
    """In-memory stand-in for pytapo.Tapo. Mirrors response shapes verified
    against a real D225."""

    DEFAULT_DATES = ["20260426", "20260425", "20260424"]
    DEFAULT_CLIPS = {
        "20260426": [
            {"startTime": 1777187350, "endTime": 1777187366},
            {"startTime": 1777192872, "endTime": 1777192887},
        ],
        "20260425": [{"startTime": 1777137763, "endTime": 1777137781}],
        "20260424": [],
    }

    def __init__(self):
        self.calls: list[tuple] = []

    def _log(self, name, *a, **kw):
        self.calls.append((name, a, kw))

    def getBasicInfo(self):
        return {
            "device_info": {
                "basic_info": {
                    "device_alias": "Front Door",
                    "device_model": "D225",
                    "sw_version": "1.2.3",
                    "hw_version": "1.0",
                    "mac": "AA:BB:CC:DD:EE:FF",
                }
            }
        }

    def getBatteryStatus(self):
        return {"battery_percent": 73, "is_charging": False}

    def getRecordingsList(self, start_date="", end_date=""):
        self._log("getRecordingsList", start_date, end_date)
        return [{"date": d} for d in self.DEFAULT_DATES]

    def getRecordings(self, date):
        self._log("getRecordings", date)
        clips = self.DEFAULT_CLIPS.get(date, [])
        return [{"video_results": clips}] if clips else []

    def getTimeCorrection(self):
        return 0

    def getUserID(self):
        return "fake-user"

    def getAudioConfig(self):
        return {
            "audio_config": {
                "microphone": {"encode_type": "G711alaw", "sampling_rate": 8}
            }
        }

    def getLED(self):
        return {"value": "on"}

    def getPrivacyMode(self):
        return {"value": "off"}

    def getMotionDetection(self):
        return {"enabled": "on"}

    def getEvents(self, startTime=0, endTime=0):
        return [{"event_type": "motion", "start_time": startTime, "end_time": endTime}]

    def setPrivacyMode(self, *_a, **_kw):
        return None

    def reboot(self):
        return None

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)

        def _stub(*a, **kw):
            self.calls.append(("__getattr__", (name,) + a, kw))
            return None

        return _stub


@pytest.fixture
def fake_tapo():
    return FakeTapo()


# ── ffmpeg helpers ─────────────────────────────────────────────────────────


def _make_av_ts(out: Path, duration: int = 2):
    """Synthesize an H.265 + AAC MPEG-TS — close enough to what the D225
    actually emits over its media session for the downloader to chew on."""
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", f"testsrc=d={duration}:s=320x240:r=10",
            "-f", "lavfi", "-i", f"sine=f=440:d={duration}",
            "-c:v", "libx265", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "96k",
            "-shortest", "-f", "mpegts", str(out),
        ],
        check=True, capture_output=True,
    )


def _make_av_mp4(out: Path, duration: int = 2):
    """Synthesize an H.264+AAC mp4 (for thumb-from-disk tests etc)."""
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", f"testsrc=d={duration}:s=320x240:r=10",
            "-f", "lavfi", "-i", f"sine=f=440:d={duration}",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "96k",
            "-shortest", "-movflags", "+faststart", str(out),
        ],
        check=True, capture_output=True,
    )


@pytest.fixture
def make_av_ts():
    return _make_av_ts


@pytest.fixture
def make_av_mp4():
    return _make_av_mp4


def _ffprobe_streams(path: Path) -> list[dict]:
    import json
    proc = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-print_format", "json",
            "-show_streams",
            str(path),
        ],
        capture_output=True, text=True, timeout=15,
    )
    if proc.returncode != 0:
        return []
    try:
        return json.loads(proc.stdout).get("streams", [])
    except Exception:
        return []


@pytest.fixture
def ffprobe_streams():
    return _ffprobe_streams


# ── FakeMediaSession ───────────────────────────────────────────────────────
# A drop-in for pytapo's media session that yields a real MPEG-TS plus
# synthetic G.711 audio frames, then a `stream_status=finished` notification.


@pytest.fixture
def make_fake_media_session(tmp_path):
    """Returns a factory(duration_s, with_audio=True) → FakeMediaSession."""

    def factory(duration: int = 2, with_audio: bool = True):
        ts_path = tmp_path / f"src_{duration}s.ts"
        if not ts_path.exists():
            _make_av_ts(ts_path, duration=duration)
        ts_bytes = ts_path.read_bytes()
        # 8kHz alaw mono → 8000 bytes/sec
        audio_total = bytes((i * 31) & 0xFF for i in range(8000 * duration)) if with_audio else b""

        class Resp:
            def __init__(self, plaintext, mimetype="video/mp2t", audio_payload=b""):
                self.plaintext = plaintext
                self.mimetype = mimetype
                self.audioPayload = audio_payload
                self.audioPayloadType = "alaw"

        class FakeSession:
            opened = False
            closed = False

            def set_window_size(self, *_a, **_kw):
                pass

            async def start(self):
                FakeSession.opened = True

            async def close(self):
                FakeSession.closed = True

            async def transceive(self, _req, _mime, **_kwargs):
                CHUNK = 16 * 1024
                chunks = [ts_bytes[i:i + CHUNK] for i in range(0, len(ts_bytes), CHUNK)] or [b""]
                per_audio = (len(audio_total) // max(1, len(chunks))) // 160 * 160 if audio_total else 0
                offset = 0
                for c in chunks:
                    ap = audio_total[offset:offset + per_audio]
                    offset += per_audio
                    yield Resp(c, audio_payload=ap)
                if offset < len(audio_total):
                    yield Resp(b"", audio_payload=audio_total[offset:])
                yield Resp(
                    b'{"params":{"event_type":"stream_status","status":"finished"}}',
                    mimetype="application/json",
                )

        return FakeSession()

    return factory


@pytest.fixture
def fake_media_tapo(fake_tapo, make_fake_media_session):
    """FakeTapo whose getMediaSession returns a fresh FakeMediaSession each
    call. Use this in tests of the downloader / thumb pulls."""

    sessions: list = []

    def get_session(*_a, **_kw):
        s = make_fake_media_session(duration=2)
        sessions.append(s)
        return s

    fake_tapo.getMediaSession = get_session  # type: ignore[attr-defined]
    fake_tapo.sessions = sessions  # type: ignore[attr-defined]
    return fake_tapo
