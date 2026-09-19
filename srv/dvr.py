"""Local DVR mode: mirror camera recordings into the app's archive.

The camera remains the source of truth. This worker only reads the camera's
recording index, downloads missing clips, and optionally removes old files
from the local archive. It never calls a camera-delete API.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Callable

from .downloader import make_runner
from .recordings import Paths, list_local, parse_clips, parse_dates

log = logging.getLogger(__name__)

ALLOWED_INTERVALS = {15, 30, 60, 360, 720, 1440}


def days_between(oldest: str | None, *, today: datetime | None = None) -> int:
    if not oldest:
        return 0
    try:
        first = datetime.strptime(oldest, "%Y%m%d").date()
    except (TypeError, ValueError):
        return 0
    now = (today or datetime.now()).date()
    return max(0, (now - first).days + 1)


class DvrService:
    def __init__(
        self,
        settings,
        paths: Paths,
        camera_call: Callable,
        gateway,
        on_event: Callable[[dict], None] | None = None,
    ):
        self.settings = settings
        self.paths = paths
        self.camera_call = camera_call
        self.gateway = gateway
        self.on_event = on_event
        self._task: asyncio.Task | None = None
        self._manual_task: asyncio.Task | None = None
        self._sync_lock = asyncio.Lock()
        self._last_sync: float | None = None
        self._last_error: str | None = None
        self._running = False
        self._syncing = False
        self._camera_oldest: str | None = None

    def config(self) -> dict:
        return {
            "enabled": bool(self.settings.dvr_enabled),
            "retention_days": self.settings.dvr_retention_days,
            "keep_forever": bool(self.settings.dvr_keep_forever),
            "interval_minutes": self.settings.dvr_interval_minutes,
            "daily_time": self.settings.dvr_daily_time,
            "schedule": self._schedule_label(),
            **self.status(),
        }

    def status(self) -> dict:
        rows = list_local(self.paths)
        dates = {row["date"] for row in rows if row.get("date")}
        oldest = min(dates) if dates else None
        newest = max(dates) if dates else None
        return {
            "running": self._running,
            "syncing": self._syncing,
            "available_days": len(dates),
            "available_span_days": days_between(oldest),
            "local_oldest_date": oldest,
            "local_newest_date": newest,
            "local_files": len(rows),
            "camera_oldest_date": self._camera_oldest,
            "camera_available_days": days_between(self._camera_oldest),
            "last_sync": self._last_sync,
            "last_error": self._last_error,
        }

    async def configure(
        self, *, enabled: bool, retention_days: int, keep_forever: bool,
        interval_minutes: int, daily_time: str,
    ) -> dict:
        self.settings.save_dvr_config(
            enabled=enabled, retention_days=retention_days, keep_forever=keep_forever,
            interval_minutes=interval_minutes, daily_time=daily_time,
        )
        await self.stop()
        if enabled:
            self.start(initial_sync=True)
        self._emit()
        return self.config()

    def start(self, *, initial_sync: bool = False) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(
                self._run(initial_sync=initial_sync), name="dvr-sync"
            )

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        manual, self._manual_task = self._manual_task, None
        if manual and not manual.done():
            manual.cancel()
            try:
                await manual
            except asyncio.CancelledError:
                pass
        self._running = False

    def trigger_sync(self) -> bool:
        if self._manual_task and not self._manual_task.done():
            return False
        self._manual_task = asyncio.create_task(
            self._manual_sync(), name="dvr-manual-sync"
        )
        return True

    async def _manual_sync(self) -> None:
        self._syncing = True
        self._emit()
        try:
            await self.sync_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._last_error = f"{type(exc).__name__}: {exc}"
            log.exception("manual DVR sync failed")
        finally:
            self._syncing = False
            self._emit()

    async def sync_once(self) -> None:
        if not self.settings.configured() or not self.settings.dvr_enabled:
            return
        async with self._sync_lock:
            days = max(1, int(self.settings.dvr_retention_days or 1))
            end = datetime.now()
            start = end - timedelta(days=days - 1)
            dates, clips = await self._camera_clips(start, end)
            self._camera_oldest = min(dates) if dates else None
            failed = await self._download_missing(clips)
            if not self.settings.dvr_keep_forever:
                self._purge_before(end.date() - timedelta(days=days - 1))
            self._last_error = f"{failed} download(s) failed" if failed else None
            self._last_sync = datetime.now().timestamp()
            self._emit()

    async def sync_previous_day(self) -> None:
        """Daily incremental pass: ask only for yesterday's recordings."""
        if not self.settings.configured() or not self.settings.dvr_enabled:
            return
        async with self._sync_lock:
            yesterday = datetime.now().date() - timedelta(days=1)
            point = datetime.combine(yesterday, datetime.min.time())
            _dates, clips = await self._camera_clips(point, point)
            failed = await self._download_missing(clips)
            days = max(1, int(self.settings.dvr_retention_days or 1))
            if not self.settings.dvr_keep_forever:
                self._purge_before(datetime.now().date() - timedelta(days=days - 1))
            self._last_error = f"{failed} download(s) failed" if failed else None
            self._last_sync = datetime.now().timestamp()
            self._emit()

    async def sync_recent(self) -> None:
        """Interval pass: recheck today and yesterday, downloading only new clips."""
        if not self.settings.configured() or not self.settings.dvr_enabled:
            return
        async with self._sync_lock:
            end = datetime.now()
            start = end - timedelta(days=1)
            _dates, clips = await self._camera_clips(start, end)
            failed = await self._download_missing(clips)
            days = max(1, int(self.settings.dvr_retention_days or 1))
            if not self.settings.dvr_keep_forever:
                self._purge_before(end.date() - timedelta(days=days - 1))
            self._last_error = f"{failed} download(s) failed" if failed else None
            self._last_sync = datetime.now().timestamp()
            self._emit()

    async def _camera_clips(self, start: datetime, end: datetime):
        def index(tapo):
            raw_dates = tapo.getRecordingsList(
                start_date=start.strftime("%Y%m%d"),
                end_date=end.strftime("%Y%m%d"),
            )
            start_key, end_key = start.strftime("%Y%m%d"), end.strftime("%Y%m%d")
            dates = [d for d in parse_dates(raw_dates) if start_key <= d <= end_key]
            clips = []
            for date in dates:
                try:
                    clips.extend(parse_clips(tapo.getRecordings(date), date))
                except Exception:
                    log.exception("could not read DVR recordings for %s", date)
            return dates, clips
        return await asyncio.to_thread(self.camera_call, index)

    async def _download_missing(self, clips) -> int:
        failed = 0
        for clip in clips:
            out = self.paths.recording(clip)
            if out.exists() and out.stat().st_size > 0:
                continue
            try:
                await self.gateway.submit_download(
                    clip, make_runner(clip, self.paths, operation="dvr"),
                    label=f"DVR {clip.key}",
                )
            except Exception:
                failed += 1
                log.exception("DVR download failed for %s", clip.key)
        return failed

    def _purge_before(self, cutoff) -> None:
        """Remove only local MP4 files older than the configured window."""
        root = self.paths.recordings
        if not root.exists():
            return
        for date_dir in list(root.iterdir()):
            if not date_dir.is_dir():
                continue
            try:
                old = datetime.strptime(date_dir.name, "%Y%m%d").date() < cutoff
            except ValueError:
                continue
            if not old:
                continue
            for path in date_dir.glob("*.mp4"):
                try:
                    path.unlink()
                except OSError:
                    log.warning("could not remove old local DVR file %s", path)
            try:
                date_dir.rmdir()
            except OSError:
                pass

    async def _run(self, *, initial_sync: bool) -> None:
        self._running = True
        self._emit()
        try:
            if initial_sync:
                try:
                    await self.sync_once()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._last_error = f"{type(exc).__name__}: {exc}"
                    log.exception("DVR history sync failed")
                    self._emit()
            while self.settings.dvr_enabled:
                interval = int(self.settings.dvr_interval_minutes or 1440)
                await asyncio.sleep(
                    self._seconds_until_next_run()
                    if interval == 1440 else interval * 60
                )
                try:
                    if interval == 1440:
                        await self.sync_previous_day()
                    else:
                        await self.sync_recent()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._last_error = f"{type(exc).__name__}: {exc}"
                    log.exception("daily DVR sync failed")
                    self._emit()
        finally:
            self._running = False
            self._emit()

    def _seconds_until_next_run(self, now: datetime | None = None) -> float:
        current = now or datetime.now()
        try:
            hour, minute = (int(part) for part in self.settings.dvr_daily_time.split(":"))
        except (TypeError, ValueError):
            hour, minute = 0, 10
        target = current.replace(
            hour=hour, minute=minute, second=0, microsecond=0,
        )
        if target <= current:
            target += timedelta(days=1)
        return max(1.0, (target - current).total_seconds())

    def _schedule_label(self) -> str:
        minutes = int(self.settings.dvr_interval_minutes or 1440)
        if minutes == 1440:
            return f"daily at {self.settings.dvr_daily_time} server time for the previous day"
        if minutes < 60:
            return f"every {minutes} minutes"
        return f"every {minutes // 60} hours"

    def _emit(self) -> None:
        if self.on_event:
            self.on_event({"type": "dvr", **self.config()})
