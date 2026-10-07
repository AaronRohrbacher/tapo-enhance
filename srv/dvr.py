"""Local DVR mode: mirror camera recordings into the app's archive.

The camera remains the source of truth. This worker only reads the camera's
recording index, downloads missing clips, and optionally removes old files
from the local archive. It never calls a camera-delete API.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta
from typing import Callable

from .downloader import ConversionQueue, cached_media_is_valid, make_fetch_runner
from .recordings import Paths, list_local, parse_clips, parse_dates

# Uvicorn configures its application logger at INFO while leaving the root
# logger at WARNING. Use that hierarchy so normal DVR lifecycle records are
# visible in server/container output, not just exception records.
log = logging.getLogger("uvicorn.error.dvr")

ALLOWED_INTERVALS = {15, 30, 60, 360, 720, 1440}
DOWNLOAD_ATTEMPTS = 3


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
        conversions: ConversionQueue | None = None,
    ):
        self.settings = settings
        self.paths = paths
        self.camera_call = camera_call
        self.gateway = gateway
        self.on_event = on_event
        self.conversions = conversions or ConversionQueue()
        self._task: asyncio.Task | None = None
        self._manual_task: asyncio.Task | None = None
        self._sync_lock = asyncio.Lock()
        self._last_sync: float | None = None
        self._last_error: str | None = None
        self._running = False
        self._syncing = False
        self._camera_oldest: str | None = None
        self._sync_total = 0
        self._sync_complete = 0
        self._sync_failed = 0
        self._sync_failed_keys: set[str] = set()
        self._download_failed_keys: set[str] = set()
        self._conversion_failed_keys: set[str] = set()
        self._conversion_current_key: str | None = None
        self._conversion_current_progress = 0.0
        self._sync_started: float | None = None
        self._sync_work: dict[str, tuple[float, float]] = {}
        self._sync_durations: dict[str, int] = {}
        self._sync_order: list[str] = []
        self._sync_phase: str | None = None
        self._stage_rates: dict[str, float | None] = {
            "download": None, "conversion": None,
        }
        self._stage_samples: dict[tuple[str, str], tuple[float, float]] = {}
        self._stage_started: dict[tuple[str, str], float] = {}
        self._eta_value: float | None = None
        self._eta_at: float | None = None

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
        progress = self._overall_progress()
        eta = self._estimated_eta() if self._syncing and 0 < progress < 1 else None
        download_progress = self._stage_progress("download")
        conversion_progress = self._stage_progress("conversion")
        downloads_ready = sum(
            progress[0] >= 1 and key not in self._download_failed_keys
            for key, progress in self._sync_work.items()
        )
        conversions_ready = sum(
            progress[1] >= 1 and key not in self._sync_failed_keys
            for key, progress in self._sync_work.items()
        )
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
            "sync_total": self._sync_total,
            "sync_complete": self._sync_complete,
            "sync_failed": self._sync_failed,
            "sync_progress": progress,
            "sync_eta_seconds": eta,
            "sync_phase": self._sync_phase,
            "download_complete": downloads_ready,
            "download_failed": len(self._download_failed_keys),
            "download_progress": download_progress,
            "download_eta_seconds": self._download_eta() if self._syncing and download_progress < 1 else None,
            "conversion_complete": conversions_ready,
            "conversion_failed": len(self._conversion_failed_keys),
            "conversion_progress": conversion_progress,
            "conversion_eta_seconds": eta,
            "conversion_current_key": self._conversion_current_key,
            "conversion_current_progress": self._conversion_current_progress,
            "conversion_current_eta_seconds": self._conversion_current_eta(),
        }

    async def configure(
        self, *, enabled: bool, retention_days: int, keep_forever: bool,
        interval_minutes: int, daily_time: str, sync_now: bool = True,
    ) -> dict:
        self.settings.save_dvr_config(
            enabled=enabled, retention_days=retention_days, keep_forever=keep_forever,
            interval_minutes=interval_minutes, daily_time=daily_time,
        )
        await self.stop()
        if enabled:
            self.start(initial_sync=sync_now)
        self._emit()
        return self.config()

    def start(self, *, initial_sync: bool = False) -> None:
        if self._task is None or self._task.done():
            log.info(
                "starting DVR worker (initial_sync=%s, schedule=%s)",
                initial_sync, self._schedule_label(),
            )
            self._task = asyncio.create_task(
                self._run(initial_sync=initial_sync), name="dvr-sync"
            )

    async def stop(self) -> None:
        pending = self.gateway.cancel_dvr() if self.gateway is not None else []
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
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    def trigger_sync(self) -> bool:
        if self._manual_task and not self._manual_task.done():
            log.info("DVR manual sync request skipped: a manual sync is already queued")
            return False
        log.info("DVR manual sync requested")
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
            log.info("DVR history sync skipped: DVR is disabled or camera is not configured")
            return
        async with self._sync_lock:
            started = time.monotonic()
            days = max(1, int(self.settings.dvr_retention_days or 1))
            end = datetime.now()
            start = end - timedelta(days=days - 1)
            log.info(
                "DVR history sync started (range=%s..%s, retention_days=%d, keep_forever=%s)",
                start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), days,
                bool(self.settings.dvr_keep_forever),
            )
            dates, clips = await self._camera_clips(start, end)
            self._camera_oldest = min(dates) if dates else None
            failed = await self._download_missing(clips)
            if not self.settings.dvr_keep_forever:
                self._purge_before(end.date() - timedelta(days=days - 1))
            self._last_error = f"{failed} download(s) failed" if failed else None
            self._last_sync = datetime.now().timestamp()
            log.info(
                "DVR history sync finished (dates=%d, clips=%d, failed=%d, elapsed=%.1fs)",
                len(dates), len(clips), failed, time.monotonic() - started,
            )
            self._emit()

    async def sync_previous_day(self) -> None:
        """Daily incremental pass: ask only for yesterday's recordings."""
        if not self.settings.configured() or not self.settings.dvr_enabled:
            log.info("DVR daily sync skipped: DVR is disabled or camera is not configured")
            return
        async with self._sync_lock:
            started = time.monotonic()
            yesterday = datetime.now().date() - timedelta(days=1)
            point = datetime.combine(yesterday, datetime.min.time())
            log.info("DVR daily sync started (date=%s)", yesterday.strftime("%Y%m%d"))
            dates, clips = await self._camera_clips(point, point)
            failed = await self._download_missing(clips)
            days = max(1, int(self.settings.dvr_retention_days or 1))
            if not self.settings.dvr_keep_forever:
                self._purge_before(datetime.now().date() - timedelta(days=days - 1))
            self._last_error = f"{failed} download(s) failed" if failed else None
            self._last_sync = datetime.now().timestamp()
            log.info(
                "DVR daily sync finished (dates=%d, clips=%d, failed=%d, elapsed=%.1fs)",
                len(dates), len(clips), failed, time.monotonic() - started,
            )
            self._emit()

    async def sync_recent(self) -> None:
        """Interval pass: recheck today and yesterday, downloading only new clips."""
        if not self.settings.configured() or not self.settings.dvr_enabled:
            log.info("DVR interval sync skipped: DVR is disabled or camera is not configured")
            return
        async with self._sync_lock:
            started = time.monotonic()
            end = datetime.now()
            start = end - timedelta(days=1)
            log.info(
                "DVR interval sync started (range=%s..%s)",
                start.strftime("%Y%m%d"), end.strftime("%Y%m%d"),
            )
            dates, clips = await self._camera_clips(start, end)
            failed = await self._download_missing(clips)
            days = max(1, int(self.settings.dvr_retention_days or 1))
            if not self.settings.dvr_keep_forever:
                self._purge_before(end.date() - timedelta(days=days - 1))
            self._last_error = f"{failed} download(s) failed" if failed else None
            self._last_sync = datetime.now().timestamp()
            log.info(
                "DVR interval sync finished (dates=%d, clips=%d, failed=%d, elapsed=%.1fs)",
                len(dates), len(clips), failed, time.monotonic() - started,
            )
            self._emit()

    async def _camera_clips(self, start: datetime, end: datetime):
        start_key, end_key = start.strftime("%Y%m%d"), end.strftime("%Y%m%d")

        async def list_dates(_tapo, _cancel):
            raw_dates = await asyncio.to_thread(
                self.camera_call,
                lambda t: t.getRecordingsList(
                    start_date=start.strftime("%Y%m%d"),
                    end_date=end.strftime("%Y%m%d"),
                ),
            )
            return [d for d in parse_dates(raw_dates) if start_key <= d <= end_key]

        dates = await self.gateway.submit_dvr_query(
            f"dates:{start_key}:{end_key}", list_dates,
            label=f"DVR dates {start_key}-{end_key}",
        )
        log.info(
            "DVR camera index returned %d date(s) for %s..%s",
            len(dates), start_key, end_key,
        )
        clips = []
        for date in dates:
            async def list_clips(_tapo, _cancel, *, requested_date=date):
                raw = await asyncio.to_thread(
                    self.camera_call, lambda t: t.getRecordings(requested_date)
                )
                return parse_clips(raw, requested_date)

            # Do not swallow a failed date. A partial index must be reported as
            # a failed sync so the next run retries the missing day.
            date_clips = await self.gateway.submit_dvr_query(
                f"clips:{date}", list_clips, label=f"DVR recordings {date}",
            )
            log.info("DVR camera index returned %d clip(s) for %s", len(date_clips), date)
            clips.extend(date_clips)
        return dates, clips

    async def _download_missing(self, clips) -> int:
        failed = 0
        skipped = 0
        downloaded = 0
        missing = []
        for clip in clips:
            out = self.paths.recording(clip)
            if await cached_media_is_valid(out, clip.duration):
                skipped += 1
                log.info("DVR clip is valid locally; skipping camera retrieval for %s", clip.key)
                continue
            if out.exists():
                log.warning("DVR cached clip is corrupt; retrieving replacement for %s", clip.key)
            missing.append(clip)

        self._sync_total = len(missing)
        self._sync_complete = 0
        self._sync_failed = 0
        self._sync_failed_keys = set()
        self._download_failed_keys = set()
        self._conversion_failed_keys = set()
        self._conversion_current_key = None
        self._conversion_current_progress = 0.0
        self._sync_started = time.monotonic()
        self._sync_work = {clip.key: (0.0, 0.0) for clip in missing}
        self._sync_durations = {clip.key: max(1, clip.duration) for clip in missing}
        self._sync_order = [clip.key for clip in missing]
        self._stage_rates = {"download": None, "conversion": None}
        self._stage_samples = {}
        self._stage_started = {}
        self._eta_value = None
        self._eta_at = None
        self._sync_phase = "downloading" if missing else "complete"
        self._emit()
        conversions: list[tuple[object, asyncio.Task]] = []
        for clip in missing:
            staged = self.paths.recording(clip).with_suffix(".download.ts")
            if staged.exists() and staged.stat().st_size > 1024:
                log.info("DVR resuming queued conversion for %s", clip.key)
                self._sync_work[clip.key] = (1.0, 0.0)
                conversions.append((clip, self.conversions.submit(
                    clip, self.paths, staged, operation="dvr",
                    on_progress=self._emit_operation,
                )))
                continue
            try:
                log.info("DVR download started for %s", clip.key)
                for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
                    try:
                        staged = await self.gateway.submit_dvr_download(
                            clip, make_fetch_runner(
                                clip, self.paths, operation="dvr",
                                on_progress=self._emit_operation,
                            ),
                            label=f"DVR {clip.key} (attempt {attempt})",
                        )
                        break
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        if attempt == DOWNLOAD_ATTEMPTS:
                            raise
                        delay = 2 ** attempt
                        log.warning(
                            "DVR download attempt %d/%d failed for %s; retrying in %ds",
                            attempt, DOWNLOAD_ATTEMPTS, clip.key, delay,
                            exc_info=True,
                        )
                        self._sync_phase = "retrying camera connection"
                        self._emit()
                        await asyncio.sleep(delay)
                conversions.append((clip, self.conversions.submit(
                    clip, self.paths, staged, operation="dvr",
                    on_progress=self._emit_operation,
                )))
                log.info("DVR download staged for %s; conversion queued", clip.key)
            except Exception:
                failed += 1
                self._sync_failed += 1
                self._sync_failed_keys.add(clip.key)
                self._download_failed_keys.add(clip.key)
                self._sync_work[clip.key] = (1.0, 1.0)
                log.exception("DVR download failed for %s", clip.key)
        self._sync_phase = "converting" if conversions else "complete"
        self._emit()
        for clip, task in conversions:
            try:
                await task
                downloaded += 1
                self._sync_complete += 1
                self._sync_work[clip.key] = (1.0, 1.0)
                log.info("DVR conversion finished for %s", clip.key)
                self._emit()
            except Exception:
                failed += 1
                self._sync_failed += 1
                self._sync_failed_keys.add(clip.key)
                self._conversion_failed_keys.add(clip.key)
                self._sync_work[clip.key] = (1.0, 1.0)
                log.exception("DVR conversion failed for %s", clip.key)
                self._emit()
        self._sync_phase = "complete"
        self._emit()
        log.info(
            "DVR download pass finished (discovered=%d, downloaded=%d, already_local=%d, failed=%d)",
            len(clips), downloaded, skipped, failed,
        )
        return failed

    def _purge_before(self, cutoff) -> None:
        """Remove only local MP4 files older than the configured window."""
        root = self.paths.recordings
        if not root.exists():
            log.info("DVR local purge skipped: recordings directory does not exist")
            return
        removed = 0
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
                    removed += 1
                    log.info("removed expired local DVR file %s", path)
                except OSError:
                    log.warning("could not remove old local DVR file %s", path)
            try:
                date_dir.rmdir()
            except OSError:
                pass
        log.info("DVR local purge finished (cutoff=%s, removed=%d)", cutoff, removed)

    async def _run(self, *, initial_sync: bool) -> None:
        self._running = True
        self._emit()
        try:
            if initial_sync:
                self._syncing = True
                self._emit()
                try:
                    await self.sync_once()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._last_error = f"{type(exc).__name__}: {exc}"
                    log.exception("DVR history sync failed")
                    self._emit()
                finally:
                    self._syncing = False
                    self._emit()
            while self.settings.dvr_enabled:
                interval = int(self.settings.dvr_interval_minutes or 1440)
                delay = self._seconds_until_next_run() if interval == 1440 else interval * 60
                log.info("DVR worker waiting %.0f seconds for %s", delay, self._schedule_label())
                await asyncio.sleep(delay)
                self._syncing = True
                self._emit()
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
                    self._syncing = False
                    self._emit()
        finally:
            self._running = False
            log.info("DVR worker stopped")
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

    def _emit_operation(self, event: dict) -> None:
        key = event.get("key")
        if key in self._sync_work:
            download, conversion = self._sync_work[key]
            value = min(1.0, max(0.0, float(event.get("progress") or 0)))
            stage = event.get("stage")
            now = time.monotonic()
            if stage in self._stage_rates:
                sample_key = (key, stage)
                self._stage_started.setdefault(sample_key, now)
                media_seconds = event.get("mediaSeconds")
                if isinstance(media_seconds, (int, float)):
                    previous = self._stage_samples.get(sample_key)
                    if previous and now > previous[0] and media_seconds > previous[1]:
                        instant = (media_seconds - previous[1]) / (now - previous[0])
                        if 0.02 <= instant <= 100:
                            old = self._stage_rates[stage]
                            self._stage_rates[stage] = instant if old is None else old * 0.8 + instant * 0.2
                    self._stage_samples[sample_key] = (now, float(media_seconds))
                if value >= 1:
                    elapsed = now - self._stage_started[sample_key]
                    if elapsed > 0:
                        effective = self._sync_durations[key] / elapsed
                        old = self._stage_rates[stage]
                        self._stage_rates[stage] = effective if old is None else old * 0.8 + effective * 0.2
            if stage == "download":
                download = value
            elif stage == "conversion":
                conversion = value
                if event.get("phase") in ("complete", "error", "cancelled"):
                    self._conversion_current_key = None
                    self._conversion_current_progress = 0.0
                else:
                    self._conversion_current_key = key
                    self._conversion_current_progress = value
            self._sync_work[key] = (download, conversion)
            if event.get("phase") not in ("complete", "error"):
                self._sync_phase = event.get("phase") or self._sync_phase
            self._emit()
        if self.on_event:
            self.on_event(event)

    def _overall_progress(self) -> float:
        total = sum(self._sync_durations.values()) * 2
        if total <= 0:
            return 1.0 if self._sync_total == 0 and self._sync_phase == "complete" else 0.0
        done = sum(
            self._sync_durations[key] * (download + conversion)
            for key, (download, conversion) in self._sync_work.items()
        )
        return round(min(1.0, max(0.0, done / total)), 4)

    def _stage_progress(self, stage: str) -> float:
        total = sum(self._sync_durations.values())
        if total <= 0:
            return 1.0 if self._sync_phase == "complete" else 0.0
        index = 0 if stage == "download" else 1
        done = sum(
            self._sync_durations[key] * progress[index]
            for key, progress in self._sync_work.items()
        )
        return round(min(1.0, max(0.0, done / total)), 4)

    def _download_eta(self) -> int | None:
        if not self._sync_order:
            return None
        rate = self._stage_rates["download"] or 1.0
        remaining = sum(
            self._sync_durations[key] * (1 - self._sync_work[key][0])
            for key in self._sync_order
        )
        return max(1, round(remaining / rate)) if remaining > 0 else 0

    def _conversion_current_eta(self) -> int | None:
        key = self._conversion_current_key
        if not key or key not in self._sync_durations:
            return None
        remaining = self._sync_durations[key] * (1 - self._conversion_current_progress)
        if remaining <= 0:
            return 0
        rate = self._stage_rates["conversion"] or 1.0
        return max(1, round(remaining / rate))

    def _estimated_eta(self) -> int | None:
        """Estimate the two-stage pipeline's makespan from measured rates.

        Downloads and conversions are each serialized but overlap each other,
        so summing all remaining work is wrong. Simulating the two lanes also
        accounts for the final conversion that cannot begin until its download
        has arrived.
        """
        if not self._sync_order:
            return None
        download_rate = self._stage_rates["download"] or 1.0
        conversion_rate = self._stage_rates["conversion"] or 1.0
        download_finish = 0.0
        conversion_finish = 0.0
        for key in self._sync_order:
            duration = self._sync_durations[key]
            download, conversion = self._sync_work[key]
            download_finish += duration * (1 - download) / download_rate
            conversion_work = duration * (1 - conversion) / conversion_rate
            if conversion_work:
                conversion_finish = max(conversion_finish, download_finish) + conversion_work
        raw = max(download_finish, conversion_finish)
        now = time.monotonic()
        if self._eta_value is None or self._eta_at is None:
            smoothed = raw
        else:
            countdown = max(0.0, self._eta_value - (now - self._eta_at))
            smoothed = countdown * 0.8 + raw * 0.2
        self._eta_value = smoothed
        self._eta_at = now
        return max(1, round(smoothed)) if raw > 0 else 0
