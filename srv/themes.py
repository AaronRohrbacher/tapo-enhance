"""Load user-editable YAML themes and expose only known CSS variables."""

from __future__ import annotations

from pathlib import Path

import yaml


ALLOWED_VARIABLES = {
    "bg", "bg-elev", "bg-elev-2", "fg", "fg-bright", "fg-dim", "fg-faint",
    "line", "line-bright", "primary", "primary-dim", "green", "warn",
    "warn-glow", "glow", "glow-soft", "select",
}


def load(directory: Path) -> list[dict]:
    themes: list[dict] = []
    if not directory.exists():
        return themes
    for path in sorted(directory.rglob("*.y*ml")):
        try:
            if path.stat().st_size > 64 * 1024:
                raise ValueError("theme file exceeds 64 KiB")
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if not isinstance(raw, dict) or not isinstance(raw.get("colors"), dict):
                raise ValueError("theme must contain a colors mapping")
            theme_id = str(raw.get("id") or path.stem).strip()
            if not theme_id.replace("-", "").replace("_", "").isalnum():
                raise ValueError("theme id may contain only letters, numbers, - and _")
            colors = {
                f"--{key}": str(value)
                for key, value in raw["colors"].items()
                if key in ALLOWED_VARIABLES and isinstance(value, (str, int, float))
            }
            themes.append({
                "id": theme_id,
                "name": str(raw.get("name") or theme_id),
                "colors": colors,
            })
        except (OSError, ValueError, yaml.YAMLError) as exc:
            themes.append({"id": path.stem, "name": path.stem, "colors": {}, "error": str(exc)})
    return themes
