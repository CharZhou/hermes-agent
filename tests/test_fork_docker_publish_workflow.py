"""Structural contract for the fork-only GHCR publishing workflow."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "docker-publish-fork.yml"
PINNED_ACTION_RE = re.compile(r"^[^@]+@[0-9a-f]{40}$")


def _workflow() -> dict[str, Any]:
    assert WORKFLOW_PATH.exists(), f"missing fork Docker workflow: {WORKFLOW_PATH}"
    parsed = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)
    return parsed


def _triggers(workflow: dict[str, Any]) -> dict[str, Any]:
    triggers = workflow.get("on", workflow.get(True))
    assert isinstance(triggers, dict)
    return triggers


def _steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    steps = job.get("steps")
    assert isinstance(steps, list)
    assert all(isinstance(step, dict) for step in steps)
    return steps


def _step_using(job: dict[str, Any], action: str) -> dict[str, Any]:
    matches = [step for step in _steps(job) if str(step.get("uses", "")).startswith(action)]
    assert len(matches) == 1, f"expected one {action} step, found {len(matches)}"
    return matches[0]


def _step_named(job: dict[str, Any], name: str) -> dict[str, Any]:
    matches = [step for step in _steps(job) if step.get("name") == name]
    assert len(matches) == 1, f"expected one {name!r} step, found {len(matches)}"
    return matches[0]


def _matrix_rows(job: dict[str, Any]) -> dict[str, dict[str, Any]]:
    strategy = job["strategy"]
    assert strategy["fail-fast"] is False
    rows = strategy["matrix"]["include"]
    assert isinstance(rows, list)
    return {row["arch"]: row for row in rows}


def _assert_official_matrix(job: dict[str, Any]) -> None:
    rows = _matrix_rows(job)
    assert rows == {
        "amd64": {
            "arch": "amd64",
            "runner": "ubuntu-latest",
            "platform": "linux/amd64",
            "cache-from": "type=gha,scope=fork-docker-amd64",
            "cache-to": "type=gha,mode=max,scope=fork-docker-amd64",
        },
        "arm64": {
            "arch": "arm64",
            "runner": "ubuntu-24.04-arm",
            "platform": "linux/arm64",
            "cache-from": "type=gha,scope=fork-docker-arm64",
            "cache-to": "type=gha,mode=max,scope=fork-docker-arm64",
        },
    }
    assert job["runs-on"] == "${{ matrix.runner }}"


def _assert_buildx_retry(job: dict[str, Any]) -> None:
    buildx_steps = [
        step
        for step in _steps(job)
        if str(step.get("uses", "")).startswith("docker/setup-buildx-action@")
    ]
    assert len(buildx_steps) == 2
    first, retry = buildx_steps
    assert first.get("id") == "buildx"
    assert first.get("continue-on-error") is True
    assert retry.get("if") == "steps.buildx.outcome == 'failure'"
    assert first["uses"] == retry["uses"]


def _assert_normalizes_image_name(job: dict[str, Any]) -> None:
    step = next(step for step in _steps(job) if step.get("id") == "image")
    script = step["run"]
    assert "tr '[:upper:]' '[:lower:]'" in script
    assert "GITHUB_OUTPUT" in script
    assert "image_name=" in script


def test_trigger_permissions_concurrency_and_fork_gates() -> None:
    workflow = _workflow()
    triggers = _triggers(workflow)
    assert set(triggers) == {"push", "workflow_dispatch"}
    assert triggers["push"] == {"branches": ["main"]}
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"] == {
        "group": "docker-publish-fork-${{ github.ref }}",
        "cancel-in-progress": True,
    }
    assert "github.run_id" not in workflow["concurrency"]["group"]

    jobs = workflow["jobs"]
    assert set(jobs) == {"build", "publish", "merge"}
    for job in jobs.values():
        assert "github.repository != 'NousResearch/hermes-agent'" in job["if"]

    assert jobs["publish"]["needs"] == ["build"]
    assert jobs["merge"]["needs"] == ["publish"]
    for name in ("publish", "merge"):
        assert jobs[name]["permissions"] == {
            "contents": "read",
            "packages": "write",
        }
        condition = jobs[name]["if"]
        assert "github.event_name == 'workflow_dispatch'" in condition
        assert "github.event_name == 'push'" in condition
        assert "github.ref == 'refs/heads/main'" in condition
    assert jobs["build"].get("permissions", {"contents": "read"}) == {
        "contents": "read"
    }


def test_image_name_is_fork_configurable_and_normalized_in_every_job() -> None:
    workflow = _workflow()
    image_name = workflow["env"]["IMAGE_NAME"]
    assert "vars.FORK_IMAGE_NAME" in image_name
    assert "ghcr.io/{0}/hermes-agent" in image_name
    assert "github.repository_owner" in image_name
    for job in workflow["jobs"].values():
        _assert_normalizes_image_name(job)

    content = WORKFLOW_PATH.read_text(encoding="utf-8")
    assert "${{ env.IMAGE_NAME }}:test" not in content
    assert "name=${{ env.IMAGE_NAME }}" not in content


def test_build_is_credential_free_and_runs_full_docker_gate() -> None:
    build = _workflow()["jobs"]["build"]
    _assert_official_matrix(build)
    _assert_buildx_retry(build)

    uses = [str(step.get("uses", "")) for step in _steps(build)]
    assert not any(item.startswith("docker/login-action@") for item in uses)
    assert build.get("permissions", {"contents": "read"}) == {"contents": "read"}

    image = _step_using(build, "docker/build-push-action@")
    config = image["with"]
    assert config["load"] is True
    assert config["platforms"] == "${{ matrix.platform }}"
    assert config["tags"] == "${{ steps.image.outputs.image_name }}:test"
    assert "HERMES_GIT_SHA=${{ github.sha }}" in config["build-args"]
    assert config["cache-from"] == "${{ matrix.cache-from }}"
    assert config["cache-to"] == "${{ matrix.cache-to }}"
    assert config.get("push") is not True
    assert "push-by-digest=true" not in str(config)

    setup_uv = _step_using(build, "astral-sh/setup-uv@")
    assert setup_uv["with"]["version"] == "0.9.28"
    retry_commands = {
        step["with"]["command"]
        for step in _steps(build)
        if step.get("uses") == "./.github/actions/retry"
    }
    assert retry_commands == {
        "uv python install 3.11",
        "uv sync --locked --python 3.11 --extra dev",
    }

    test_step = _step_named(build, "Run docker integration tests")
    assert "scripts/run_tests.sh tests/docker/ --file-timeout 600" in test_step["run"]
    assert test_step["env"] == {
        "HERMES_TEST_IMAGE": "${{ steps.image.outputs.image_name }}:test",
        "OPENROUTER_API_KEY": "",
        "OPENAI_API_KEY": "",
        "NOUS_API_KEY": "",
    }


def test_publish_is_credentialed_digest_only_and_test_free() -> None:
    publish = _workflow()["jobs"]["publish"]
    _assert_official_matrix(publish)
    _assert_buildx_retry(publish)

    login = _step_using(publish, "docker/login-action@")
    assert login["with"] == {
        "registry": "ghcr.io",
        "username": "${{ github.actor }}",
        "password": "${{ secrets.GITHUB_TOKEN }}",
    }

    push = _step_using(publish, "docker/build-push-action@")
    config = push["with"]
    assert config["platforms"] == "${{ matrix.platform }}"
    assert config["outputs"] == (
        "type=image,name=${{ steps.image.outputs.image_name }},"
        "push-by-digest=true,name-canonical=true,push=true"
    )
    assert "tags" not in config
    assert "org.opencontainers.image.revision=${{ github.sha }}" in config["labels"]
    assert (
        "org.opencontainers.image.source=${{ github.server_url }}/${{ github.repository }}"
        in config["labels"]
    )
    assert config["cache-from"] == "${{ matrix.cache-from }}"
    assert config["cache-to"] == "${{ matrix.cache-to }}"

    upload = _step_using(publish, "actions/upload-artifact@")
    assert upload["with"] == {
        "name": "digest-${{ matrix.arch }}",
        "path": "/tmp/digests/*",
        "if-no-files-found": "error",
        "retention-days": 1,
    }
    publish_text = yaml.safe_dump(publish)
    assert "scripts/run_tests.sh" not in publish_text
    assert "docker run" not in publish_text


def test_merge_requires_two_digests_and_gates_mutable_tags() -> None:
    merge = _workflow()["jobs"]["merge"]
    _assert_buildx_retry(merge)
    login = _step_using(merge, "docker/login-action@")
    assert login["with"]["registry"] == "ghcr.io"
    assert login["with"]["username"] == "${{ github.actor }}"
    assert login["with"]["password"] == "${{ secrets.GITHUB_TOKEN }}"

    download = _step_using(merge, "actions/download-artifact@")
    assert download["with"] == {
        "path": "/tmp/digests",
        "pattern": "digest-*",
        "merge-multiple": True,
    }

    manifest = _step_named(merge, "Create and inspect manifest")
    assert manifest["working-directory"] == "/tmp/digests"
    assert manifest["env"]["IMAGE_NAME"] == "${{ steps.image.outputs.image_name }}"
    script = manifest["run"]
    assert "set -euo pipefail" in script
    assert 'if [ "${#digest_files[@]}" -ne 2 ]' in script
    assert '"${IMAGE_NAME}@sha256:${digest_file}"' in script
    assert 'tags=(-t "${IMAGE_NAME}:sha-${GITHUB_SHA}")' in script
    assert '"${GITHUB_EVENT_NAME}" = "push"' in script
    assert '"${GITHUB_REF}" = "refs/heads/main"' in script
    assert 'tags+=(-t "${IMAGE_NAME}:main" -t "${IMAGE_NAME}:latest")' in script
    assert "for attempt in 1 2 3" in script
    assert "docker buildx imagetools create" in script
    assert "sleep 20" in script
    assert 'docker buildx imagetools inspect "${IMAGE_NAME}:sha-${GITHUB_SHA}"' in script
    assert "GITHUB_SHA::" not in script
    assert re.search(r'\$\{IMAGE_NAME\}:\$\{GITHUB_SHA(?:::[^}]*)?\}', script) is None


def test_actions_are_pinned_and_local_actions_exist() -> None:
    workflow = _workflow()
    external_actions: list[str] = []
    for job in workflow["jobs"].values():
        for step in _steps(job):
            uses = step.get("uses")
            if not uses:
                continue
            uses = str(uses)
            if uses.startswith("./"):
                action_dir = REPO_ROOT / uses.removeprefix("./")
                assert any(
                    (action_dir / name).exists()
                    for name in ("action.yml", "action.yaml", "Dockerfile")
                ), f"missing local action entrypoint: {action_dir}"
            else:
                external_actions.append(uses)
    assert external_actions
    assert all(PINNED_ACTION_RE.fullmatch(action) for action in external_actions)


def test_no_internal_variants_or_specs_are_restored() -> None:
    content = WORKFLOW_PATH.read_text(encoding="utf-8").lower()
    for forbidden in ("fork-feishu", "overlay", "hermes-aio", "variant"):
        assert forbidden not in content
    assert not (REPO_ROOT / ".github" / "docker" / "fork-feishu-overlay.Dockerfile").exists()
    assert not (REPO_ROOT / ".github" / "docker" / "fork-aio-overlay.Dockerfile").exists()
    assert not (REPO_ROOT / ".github" / "actions" / "hermes-aio-smoke-test").exists()
