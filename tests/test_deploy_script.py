import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).parent.parent / "scripts" / "deploy.py"
SPEC = importlib.util.spec_from_file_location("deploy_script", SCRIPT)
deploy = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(deploy)


def test_legacy_version_starts_at_0_1():
    assert deploy.next_version("1.0b") == "0.1"


def test_simple_version_increments_minor_number():
    assert deploy.next_version("0.1") == "0.2"
    assert deploy.next_version("2.9") == "2.10"
