"""Audio codec detection from getAudioConfig — the single point of truth
for whether the downloader feeds ffmpeg `alaw` vs `mulaw`."""

from __future__ import annotations

from srv.audio import AudioFormat, parse_audio_config


def test_default_when_config_missing():
    assert parse_audio_config(None) == AudioFormat("alaw", 8000)
    assert parse_audio_config({}) == AudioFormat("alaw", 8000)
    assert parse_audio_config({"audio_config": {}}) == AudioFormat("alaw", 8000)


def test_alaw_8khz_typical_d225_response():
    cfg = {"audio_config": {"microphone": {"encode_type": "G711alaw", "sampling_rate": 8}}}
    assert parse_audio_config(cfg) == AudioFormat("alaw", 8000)


def test_detects_mulaw_via_substring():
    cfg = {"audio_config": {"microphone": {"encode_type": "G711ulaw", "sampling_rate": 8}}}
    assert parse_audio_config(cfg) == AudioFormat("mulaw", 8000)


def test_treats_sampling_rate_as_khz_when_small():
    cfg = {"audio_config": {"microphone": {"encode_type": "alaw", "sampling_rate": 16}}}
    assert parse_audio_config(cfg) == AudioFormat("alaw", 16000)


def test_uses_explicit_hz_when_already_large():
    cfg = {"audio_config": {"microphone": {"encode_type": "alaw", "sampling_rate": 8000}}}
    assert parse_audio_config(cfg) == AudioFormat("alaw", 8000)


def test_falls_back_to_default_on_garbage_rate():
    cfg = {"audio_config": {"microphone": {"encode_type": "alaw", "sampling_rate": "wat"}}}
    assert parse_audio_config(cfg) == AudioFormat("alaw", 8000)


def test_falls_back_when_microphone_missing():
    cfg = {"audio_config": {"speaker": {"foo": "bar"}}}
    assert parse_audio_config(cfg) == AudioFormat("alaw", 8000)
