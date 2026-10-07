"""Recording metadata: parse Tapo's getRecordingsList / getRecordings shapes,
and own the on-disk path conventions for cached clips, thumbs, previews.

Pure functions only — no I/O against the camera; no asyncio. This is the
boring code that the rest of the app builds on, so it gets exhaustive
test coverage of every shape the camera firmware has been observed
returning."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Clip:
    date: str  # "YYYYMMDD"
    start: int
    end: int

    @property
    def key(self) -> str:
        return f"{self.date}/{self.start}/{self.end}"

    @property
    def duration(self) -> int:
        return max(0, self.end - self.start)

    def to_json(self) -> dict:
        return {"date": self.date, "startTime": self.start, "endTime": self.end}


def parse_dates(raw) -> list[str]:
    """Flatten Tapo's getRecordingsList response into a sorted list of
    YYYYMMDD strings. Observed shapes:
      [{"date": "20260101"}, ...]
      [{"search_results_0": {"date": "20260101"}}, ...]
      ["20260101", ...]
    """
    out: set[str] = set()
    items = raw if isinstance(raw, list) else [raw]
    for item in items:
        if isinstance(item, str) and item.isdigit() and len(item) == 8:
            out.add(item)
        elif isinstance(item, dict):
            d = item.get("date")
            if isinstance(d, str) and d.isdigit() and len(d) == 8:
                out.add(d)
            else:
                for v in item.values():
                    if isinstance(v, dict):
                        vd = v.get("date")
                        if isinstance(vd, str) and vd.isdigit() and len(vd) == 8:
                            out.add(vd)
    return sorted(out, reverse=True)


def parse_clips(raw, date: str) -> list[Clip]:
    """Flatten Tapo's getRecordings(date) response into Clip objects.
    Observed shapes:
      [{"video_results": [{"startTime": ..., "endTime": ...}, ...]}, ...]
      [{"startTime": ..., "endTime": ...}, ...]
      [{"search_video_results_N": {"startTime": ..., "endTime": ...}}, ...]
    Invalid entries are silently skipped.
    """
    out: list[Clip] = []
    seen: set[tuple[int, int]] = set()
    items = raw if isinstance(raw, list) else [raw]

    def _push(s, e):
        try:
            si, ei = int(s), int(e)
        except (TypeError, ValueError):
            return
        if ei <= si:
            return
        key = (si, ei)
        if key in seen:
            return
        seen.add(key)
        out.append(Clip(date, si, ei))

    for item in items:
        if not isinstance(item, dict):
            continue
        if "video_results" in item and isinstance(item["video_results"], list):
            for vr in item["video_results"]:
                if isinstance(vr, dict):
                    _push(vr.get("startTime"), vr.get("endTime"))
        elif "startTime" in item:
            _push(item.get("startTime"), item.get("endTime"))
        else:
            for key, val in item.items():
                if (
                    isinstance(key, str)
                    and key.startswith("search_video_results")
                    and isinstance(val, dict)
                ):
                    _push(val.get("startTime"), val.get("endTime"))
    # Stable order: chronological.
    out.sort(key=lambda c: c.start)
    return out


# ── On-disk path conventions ───────────────────────────────────────────────


class Paths:
    """All cache paths derived from a single root, so the user can wipe
    `cache/` to clear everything and tests can point at a tmp dir."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.recordings = self.root / "recordings"
        self.stream = self.root / "stream"
        self.thumbs = self.root / "thumbs"
        self.previews = self.root / "previews"
        self.playback = self.root / "playback"

    def ensure(self) -> None:
        for d in (
            self.root,
            self.recordings,
            self.stream,
            self.thumbs,
            self.previews,
            self.playback,
        ):
            d.mkdir(parents=True, exist_ok=True)

    def recording(self, c: Clip) -> Path:
        return self.recordings / c.date / f"{c.start}_{c.end}.mp4"

    def thumb(self, c: Clip) -> Path:
        return self.thumbs / c.date / f"{c.start}_{c.end}.jpg"

    def preview(self, c: Clip) -> Path:
        return self.previews / c.date / f"{c.start}_{c.end}.mp4"

    def cache_buckets(self) -> dict[str, Path]:
        return {
            "recordings": self.recordings,
            "stream": self.stream,
            "thumbs": self.thumbs,
            "previews": self.previews,
            "playback": self.playback,
        }


def list_local(paths: Paths) -> list[dict]:
    """List every cached recording on disk, sorted newest first."""
    out: list[dict] = []
    if not paths.recordings.exists():
        return out
    for date_dir in sorted(paths.recordings.iterdir(), reverse=True):
        if not date_dir.is_dir():
            continue
        for f in sorted(date_dir.iterdir(), reverse=True):
            if f.suffix != ".mp4":
                continue
            try:
                start, end = (int(part) for part in f.stem.split("_", 1))
            except (TypeError, ValueError):
                continue
            try:
                size = f.stat().st_size
            except OSError:
                continue
            out.append(
                {
                    "date": date_dir.name,
                    "startTime": start,
                    "endTime": end,
                    "key": f"{date_dir.name}/{start}/{end}",
                    "file": f.name,
                    "path": f"/recordings/{date_dir.name}/{f.name}",
                    "size_mb": round(size / 1024 / 1024, 1),
                }
            )
    return out


def cache_usage(paths: Paths) -> dict:
    """Per-bucket size + file count for the UI's purge panel."""
    out: dict = {}
    total = 0
    for name, d in paths.cache_buckets().items():
        files = 0
        size = 0
        if d.exists():
            for f in d.rglob("*"):
                try:
                    if f.is_file():
                        files += 1
                        size += f.stat().st_size
                except OSError:
                    pass
        out[name] = {
            "path": str(d.relative_to(paths.root.parent)) if paths.root.parent in d.parents else str(d),
            "bytes": size,
            "files": files,
        }
        total += size
    out["total_bytes"] = total
    return out
