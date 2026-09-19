"""DVR scheduling and status calculations that do not require camera media."""

from datetime import datetime

from srv.dvr import DvrService, days_between
from srv.recordings import Paths


class _Settings:
    dvr_enabled = False
    dvr_retention_days = 7
    dvr_keep_forever = False
    dvr_interval_minutes = 1440
    dvr_daily_time = "00:10"


def test_next_daily_run_is_1210am_local_time():
    service = DvrService(_Settings(), Paths("unused"), lambda fn: None, gateway=None)
    before = datetime(2026, 9, 18, 0, 9, 30)
    after = datetime(2026, 9, 18, 0, 10, 1)
    assert service._seconds_until_next_run(before) == 30
    assert service._seconds_until_next_run(after) == 86399


def test_camera_history_default_is_inclusive_span():
    assert days_between("20260915", today=datetime(2026, 9, 18)) == 4


def test_local_available_days_counts_dates_with_video_not_elapsed_span(tmp_path):
    paths = Paths(tmp_path)
    paths.ensure()
    for date in ("20260901", "20260918"):
        folder = paths.recordings / date
        folder.mkdir()
        (folder / "1_2.mp4").write_bytes(b"video")
    service = DvrService(_Settings(), paths, lambda fn: None, gateway=None)
    status = service.status()
    assert status["available_days"] == 2
    assert status["available_span_days"] == 18
