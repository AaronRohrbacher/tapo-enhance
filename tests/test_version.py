from pathlib import Path

from srv import version


def test_repository_version_is_the_runtime_fallback(monkeypatch):
    monkeypatch.delenv("APP_VERSION", raising=False)
    expected = (Path(__file__).parent.parent / "VERSION").read_text().strip()
    assert version._version() == expected


def test_image_version_overrides_repository_version(monkeypatch):
    monkeypatch.setenv("APP_VERSION", "v2.3.4")
    assert version._version() == "2.3.4"
