"""Pure-function coverage for recordings parsing + path layout."""

from __future__ import annotations

from pathlib import Path

from srv.recordings import Clip, Paths, cache_usage, list_local, parse_clips, parse_dates


# ── parse_dates ────────────────────────────────────────────────────────────


def test_parse_dates_dict_with_date_field():
    assert parse_dates([{"date": "20260101"}, {"date": "20260102"}]) == ["20260102", "20260101"]


def test_parse_dates_nested_search_results_shape():
    raw = [
        {"search_results_0": {"date": "20260301"}},
        {"search_results_1": {"date": "20260302"}},
    ]
    assert parse_dates(raw) == ["20260302", "20260301"]


def test_parse_dates_string_list():
    assert parse_dates(["20260105", "20260104"]) == ["20260105", "20260104"]


def test_parse_dates_dedupes():
    assert parse_dates([{"date": "20260101"}, {"date": "20260101"}]) == ["20260101"]


def test_parse_dates_ignores_garbage():
    assert parse_dates([{"date": "junk"}, {"date": 12345}, None, 42, "20260106"]) == ["20260106"]


def test_parse_dates_empty():
    assert parse_dates([]) == []
    assert parse_dates(None) == []


# ── parse_clips ────────────────────────────────────────────────────────────


def test_parse_clips_video_results_shape():
    raw = [
        {"video_results": [
            {"startTime": 1000, "endTime": 1010},
            {"startTime": 2000, "endTime": 2020},
        ]}
    ]
    clips = parse_clips(raw, "20260101")
    assert clips == [
        Clip("20260101", 1000, 1010),
        Clip("20260101", 2000, 2020),
    ]


def test_parse_clips_flat_shape():
    raw = [{"startTime": 1000, "endTime": 1010}]
    assert parse_clips(raw, "20260101") == [Clip("20260101", 1000, 1010)]


def test_parse_clips_search_video_results_shape():
    raw = [{"search_video_results_0": {"startTime": 100, "endTime": 110}}]
    assert parse_clips(raw, "20260101") == [Clip("20260101", 100, 110)]


def test_parse_clips_orders_chronologically():
    raw = [{"video_results": [
        {"startTime": 2000, "endTime": 2010},
        {"startTime": 1000, "endTime": 1010},
    ]}]
    clips = parse_clips(raw, "20260101")
    assert [c.start for c in clips] == [1000, 2000]


def test_parse_clips_skips_zero_or_negative_duration():
    raw = [{"video_results": [
        {"startTime": 1000, "endTime": 1000},  # zero
        {"startTime": 1000, "endTime": 999},   # negative
        {"startTime": 1000, "endTime": 1010},  # valid
    ]}]
    assert parse_clips(raw, "20260101") == [Clip("20260101", 1000, 1010)]


def test_parse_clips_skips_non_int_times():
    raw = [{"video_results": [
        {"startTime": "abc", "endTime": 1000},
        {"startTime": None, "endTime": None},
    ]}]
    assert parse_clips(raw, "20260101") == []


def test_parse_clips_empty():
    assert parse_clips([], "20260101") == []
    assert parse_clips(None, "20260101") == []


# ── Clip ───────────────────────────────────────────────────────────────────


def test_clip_key_is_stable():
    c = Clip("20260101", 1000, 1010)
    assert c.key == "20260101/1000/1010"


def test_clip_duration():
    assert Clip("20260101", 1000, 1010).duration == 10


def test_clip_to_json_uses_camera_field_names():
    j = Clip("20260101", 1000, 1010).to_json()
    assert j == {"date": "20260101", "startTime": 1000, "endTime": 1010}


# ── Paths ──────────────────────────────────────────────────────────────────


def test_paths_layout_under_root(tmp_path):
    p = Paths(tmp_path / "cache")
    p.ensure()
    for d in (p.recordings, p.thumbs, p.previews, p.playback, p.stream):
        assert d.exists() and d.is_dir()


def test_paths_recording_includes_date_dir(tmp_path):
    p = Paths(tmp_path / "cache")
    c = Clip("20260101", 1000, 1010)
    assert p.recording(c).name == "1000_1010.mp4"
    assert p.recording(c).parent.name == "20260101"


def test_paths_thumb_and_preview(tmp_path):
    p = Paths(tmp_path / "cache")
    c = Clip("20260101", 1000, 1010)
    assert p.thumb(c).suffix == ".jpg"
    assert p.preview(c).suffix == ".mp4"


# ── list_local ─────────────────────────────────────────────────────────────


def test_list_local_returns_files_sorted_newest_first(tmp_path):
    p = Paths(tmp_path / "cache")
    p.ensure()
    (p.recordings / "20260101").mkdir()
    (p.recordings / "20260102").mkdir()
    (p.recordings / "20260101" / "1000_1010.mp4").write_bytes(b"x" * 4096)
    (p.recordings / "20260102" / "2000_2020.mp4").write_bytes(b"x" * 8192)

    rows = list_local(p)
    assert [r["date"] for r in rows] == ["20260102", "20260101"]
    assert all(r["path"].startswith("/recordings/") for r in rows)


def test_list_local_handles_empty_dir(tmp_path):
    p = Paths(tmp_path / "cache")
    p.ensure()
    assert list_local(p) == []


def test_list_local_skips_non_mp4(tmp_path):
    p = Paths(tmp_path / "cache")
    p.ensure()
    (p.recordings / "20260101").mkdir()
    (p.recordings / "20260101" / "extra.txt").write_text("nope")
    assert list_local(p) == []


# ── cache_usage ────────────────────────────────────────────────────────────


def test_cache_usage_reports_bytes_and_files(tmp_path):
    p = Paths(tmp_path / "cache")
    p.ensure()
    (p.recordings / "20260101").mkdir()
    (p.recordings / "20260101" / "a.mp4").write_bytes(b"x" * 10)
    (p.thumbs / "20260101").mkdir()
    (p.thumbs / "20260101" / "a.jpg").write_bytes(b"x" * 5)

    u = cache_usage(p)
    assert u["recordings"]["files"] == 1
    assert u["recordings"]["bytes"] == 10
    assert u["thumbs"]["files"] == 1
    assert u["thumbs"]["bytes"] == 5
    assert u["total_bytes"] == 15
