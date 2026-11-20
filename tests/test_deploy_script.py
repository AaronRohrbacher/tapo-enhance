import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).parent.parent / "scripts" / "deploy.py"
SPEC = importlib.util.spec_from_file_location("deploy_script", SCRIPT)
deploy = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(deploy)


def test_invalid_legacy_version_starts_at_0_1_0():
    assert deploy.next_version("1.0b") == "0.1.0"


def test_two_part_version_migrates_and_increments_patch():
    assert deploy.next_version("0.1") == "0.1.1"
    assert deploy.next_version("2.9") == "2.9.1"


def test_semantic_version_increments_patch_number():
    assert deploy.next_version("0.1.1") == "0.1.2"
    assert deploy.next_version("2.9.9") == "2.9.10"


def test_choose_version_accepts_semantic_override(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _prompt: "0.1.2")
    assert deploy.choose_version("0.1.1") == "0.1.2"
