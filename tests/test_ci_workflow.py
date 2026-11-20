from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "container.yml"


def load_workflow():
    return yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader)


def test_ci_runs_for_main_pull_requests_and_pushes():
    workflow = load_workflow()
    assert workflow["on"] == {
        "pull_request": {"branches": ["main"]},
        "push": {"branches": ["main"]},
    }


def test_dockerhub_publish_waits_for_tests_and_has_traceable_tags():
    workflow = load_workflow()
    publish = workflow["jobs"]["publish"]
    assert publish["needs"] == "test"
    assert publish["if"] == "github.event_name == 'push'"
    rendered = WORKFLOW.read_text()
    assert "docker.io/${{ env.DOCKERHUB_IMAGE }}" in rendered
    assert "type=raw,value=latest" in rendered
    assert "type=raw,value=${{ steps.version.outputs.value }}" in rendered
    assert "type=sha,format=long" in rendered
    assert "ghcr.io" not in rendered


def test_dockerhub_overview_uses_repository_readme():
    workflow = load_workflow()
    steps = workflow["jobs"]["publish"]["steps"]
    description = next(
        step for step in steps
        if step.get("name") == "Update Docker Hub overview from README"
    )
    assert description["with"]["readme-filepath"] == "./README.md"
    assert description["with"]["enable-url-completion"] == "true"
