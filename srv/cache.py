"""Cache wipe utility. Targeted bucket wipes preserve sibling buckets."""

from __future__ import annotations

import shutil
from pathlib import Path

from .errors import ApiError, Code
from .recordings import Paths


VALID_TARGETS = ("all", "recordings", "stream", "thumbs", "previews", "playback")


def clear(paths: Paths, target: str) -> dict:
    if target not in VALID_TARGETS:
        raise ApiError(
            Code.BAD_REQUEST,
            f"unknown target {target!r}; expected one of {list(VALID_TARGETS)}",
            status=400,
        )
    buckets = paths.cache_buckets()
    if target == "all":
        dirs = list(buckets.values())
    else:
        dirs = [buckets[target]]

    removed_files = 0
    removed_bytes = 0
    for d in dirs:
        if not d.exists():
            d.mkdir(parents=True, exist_ok=True)
            continue
        for child in d.iterdir():
            try:
                if child.is_file() or child.is_symlink():
                    removed_bytes += child.stat().st_size
                    removed_files += 1
                    child.unlink()
                elif child.is_dir():
                    for f in child.rglob("*"):
                        if f.is_file():
                            try:
                                removed_bytes += f.stat().st_size
                                removed_files += 1
                            except OSError:
                                pass
                    shutil.rmtree(child, ignore_errors=True)
            except OSError:
                pass
        d.mkdir(parents=True, exist_ok=True)
    return {"target": target, "removed_files": removed_files, "removed_bytes": removed_bytes}
