from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parent.parent
PUBLISH_WORKFLOW = ROOT / ".github" / "workflows" / "container.yml"
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


def load_workflow(path):
    return yaml.load(path.read_text(), Loader=yaml.BaseLoader)


def test_ci_runs_for_main_pull_requests_and_pushes():
    workflow = load_workflow(CI_WORKFLOW)
    assert workflow["on"] == {
        "pull_request": {"branches": ["main"]},
        "push": {"branches": ["main"]},
        "workflow_dispatch": "",
    }
    assert set(workflow["jobs"]) == {"tests", "browser"}
    assert workflow["jobs"]["tests"]["steps"][-1]["run"] == (
        ".venv/bin/python -m pytest tests/"
    )
    assert workflow["jobs"]["browser"]["steps"][-1]["run"] == (
        ".venv/bin/python -m pytest tests/test_browser.py -m browser"
    )
    test_install = workflow["jobs"]["tests"]["steps"][-2]["run"]
    browser_install = workflow["jobs"]["browser"]["steps"][-2]["run"]
    assert "apt-get install -y ffmpeg" in test_install
    assert "apt-get install -y ffmpeg" in browser_install
    assert "playwright install --with-deps chromium" in browser_install


def test_dockerhub_publish_is_independent_and_has_traceable_tags():
    workflow = load_workflow(PUBLISH_WORKFLOW)
    assert workflow["on"] == {
        "push": {"branches": ["main"]},
        "workflow_dispatch": "",
    }
    assert set(workflow["jobs"]) == {"publish"}
    publish = workflow["jobs"]["publish"]
    assert "needs" not in publish
    rendered = PUBLISH_WORKFLOW.read_text()
    assert "docker.io/${{ env.DOCKERHUB_IMAGE }}" in rendered
    assert "type=raw,value=latest" in rendered
    assert "type=raw,value=${{ steps.version.outputs.value }}" in rendered
    assert "type=sha,format=long" in rendered
    assert "ghcr.io" not in rendered


def test_dockerhub_overview_uses_repository_readme():
    workflow = load_workflow(PUBLISH_WORKFLOW)
    steps = workflow["jobs"]["publish"]["steps"]
    description = next(
        step for step in steps
        if step.get("name") == "Update Docker Hub overview from README"
    )
    assert description["with"]["readme-filepath"] == "./README.md"
    assert description["with"]["enable-url-completion"] == "true"
