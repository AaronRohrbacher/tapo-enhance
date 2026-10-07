#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE = ROOT / "VERSION"
SIMPLE_VERSION = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)$")


def next_version(current: str) -> str:
    """Return the next major.minor version; legacy values restart at 0.1."""
    match = SIMPLE_VERSION.fullmatch(current.strip())
    if not match:
        return "0.1"
    major, minor = (int(part) for part in match.groups())
    return f"{major}.{minor + 1}"


def choose_version(suggested: str) -> str:
    entered = input(f"Release version [{suggested}]: ").strip()
    version = entered or suggested
    if not SIMPLE_VERSION.fullmatch(version):
        raise ValueError("version must use major.minor format, for example 0.1")
    return version


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Choose and record the next release version. Does not deploy."
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="show the selected version without changing VERSION",
    )
    args = parser.parse_args()

    current = VERSION_FILE.read_text().strip() if VERSION_FILE.exists() else ""
    suggested = next_version(current)
    try:
        selected = choose_version(suggested)
    except (EOFError, KeyboardInterrupt):
        print("\nRelease preparation cancelled.")
        return 130
    except ValueError as exc:
        print(f"Invalid version: {exc}")
        return 2

    if args.dry_run:
        print(f"Would set VERSION to {selected}")
        return 0

    VERSION_FILE.write_text(selected + "\n")
    print(f"VERSION set to {selected}")
    print(f"Next step: review the changes and publish GitHub Release v{selected}.")
    print("No Git commands, image pushes, workflow triggers, or deployments were run.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
