"""Camera audio codec detection. The D225 mic emits G.711 (alaw or mulaw)
on a *separate* RTP payload, NOT inside the MPEG-TS — so the downloader
must mux it back in via a second ffmpeg pipe. The shape returned by
pytapo's getAudioConfig has varied across firmwares, so we look in
several places before falling back to defaults."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AudioFormat:
    ffmpeg_format: str  # "alaw" or "mulaw"
    sample_rate: int    # Hz, e.g. 8000

    @classmethod
    def default(cls) -> "AudioFormat":
        return cls("alaw", 8000)


def parse_audio_config(cfg) -> AudioFormat:
    """Pull the encode_type and sampling_rate out of the camera's audio config.
    Returns defaults if the shape doesn't match what we recognize."""
    if not isinstance(cfg, dict):
        return AudioFormat.default()
    mic = (
        cfg.get("audio_config", {}).get("microphone", {})
        if isinstance(cfg.get("audio_config"), dict)
        else {}
    )
    if not isinstance(mic, dict):
        mic = {}
    encode = str(mic.get("encode_type", "")).lower()
    fmt = "mulaw" if "ulaw" in encode or "u-law" in encode else "alaw"
    sr_raw = mic.get("sampling_rate")
    try:
        # Camera reports sampling rate in kHz (e.g. "8" → 8000).
        sr_int = int(sr_raw) if sr_raw is not None else 8
    except (TypeError, ValueError):
        sr_int = 8
    if sr_int < 100:  # treat as kHz
        sr_int *= 1000
    if sr_int <= 0:
        sr_int = 8000
    return AudioFormat(fmt, sr_int)


# G.711 frame size (160 bytes per 20ms frame at 8kHz mono) — the camera
# delivers audio in arbitrary chunk sizes; we re-frame before writing to
# the ffmpeg pipe so timestamps stay clean.
G711_FRAME_BYTES = 160
