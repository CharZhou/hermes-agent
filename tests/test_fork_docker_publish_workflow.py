"""Tests for the fork-specific Docker publish workflow.

This workflow exists specifically so forks can publish their own container
images without editing the upstream-only docker-publish.yml.
"""

from __future__ import annotations

import pathlib

import pytest
import yaml


REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


class TestForkDockerPublishWorkflow:
    WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "docker-publish-fork.yml"

    def test_workflow_exists(self):
        assert self.WORKFLOW_PATH.exists(), (
            f"Fork Docker publish workflow missing: {self.WORKFLOW_PATH}"
        )

    def test_workflow_yaml_is_valid(self):
        content = self.WORKFLOW_PATH.read_text(encoding="utf-8")
        try:
            parsed = yaml.safe_load(content)
        except yaml.YAMLError as exc:
            pytest.fail(f"docker-publish-fork.yml is not valid YAML: {exc}")
        assert isinstance(parsed, dict)
        assert "jobs" in parsed

    def test_has_manual_dispatch_and_main_push(self):
        parsed = yaml.safe_load(self.WORKFLOW_PATH.read_text(encoding="utf-8"))
        triggers = parsed.get("on", parsed.get(True))
        assert "workflow_dispatch" in triggers
        assert "push" in triggers
        assert triggers["push"]["branches"] == ["main"]

    def test_uses_fork_image_variable_and_ghcr(self):
        content = self.WORKFLOW_PATH.read_text(encoding="utf-8")
        assert "FORK_IMAGE_NAME" in content
        assert "ghcr.io" in content
        assert "tr '[:upper:]' '[:lower:]'" in content
        assert "docker/login-action" in content
        assert "docker/build-push-action" in content

    def test_emits_short_sha_tag(self):
        content = self.WORKFLOW_PATH.read_text(encoding="utf-8")
        assert 'short_sha="${GITHUB_SHA::12}"' in content
        assert 'short_sha6="${GITHUB_SHA::6}"' in content
        assert '-t "${IMAGE_NAME}:main"' in content
        assert '-t "${IMAGE_NAME}:latest"' in content
        assert '-t "${IMAGE_NAME}:sha-${short_sha}"' in content
        assert '-t "${IMAGE_NAME}:${short_sha6}"' in content

    def test_builds_both_linux_platforms(self):
        parsed = yaml.safe_load(self.WORKFLOW_PATH.read_text(encoding="utf-8"))
        build_job = parsed["jobs"]["build-and-publish"]
        matrix = build_job["strategy"]["matrix"]["include"]

        assert {entry["platform"] for entry in matrix} == {
            "linux/amd64",
            "linux/arm64",
        }
        assert {entry["arch"] for entry in matrix} == {"amd64", "arm64"}
        assert build_job["strategy"]["fail-fast"] is False

    def test_merges_architecture_digests_into_published_tags(self):
        parsed = yaml.safe_load(self.WORKFLOW_PATH.read_text(encoding="utf-8"))
        jobs = parsed["jobs"]
        build_steps = jobs["build-and-publish"]["steps"]
        build_content = "\n".join(step.get("run", "") for step in build_steps)
        build_uses = "\n".join(step.get("uses", "") for step in build_steps)
        build_with = "\n".join(str(step.get("with", {})) for step in build_steps)

        assert "push-by-digest=true" in build_with
        assert "actions/upload-artifact" in build_uses
        assert "digest-${{ matrix.arch }}" in "\n".join(
            step.get("with", {}).get("name", "") for step in build_steps
        )
        assert "docker run --rm" in build_content

        merge_job = jobs["merge"]
        assert merge_job["needs"] == ["build-and-publish"]
        merge_content = "\n".join(
            step.get("run", "") for step in merge_job["steps"]
        )
        assert "docker buildx imagetools create" in merge_content
        assert "docker buildx imagetools inspect" in merge_content

    def test_no_overlay_variants_remain(self):
        """The fork publishes the plain upstream image: lazy backends install
        at runtime via HERMES_LAZY_INSTALL_TARGET, so no overlay layers."""
        content = self.WORKFLOW_PATH.read_text(encoding="utf-8")
        assert "aio" not in content
        assert "fork-feishu" not in content
        assert not (REPO_ROOT / ".github" / "docker").exists()
        assert not (REPO_ROOT / ".github" / "actions" / "hermes-aio-smoke-test").exists()

    def test_local_actions_exist(self):
        parsed = yaml.safe_load(self.WORKFLOW_PATH.read_text(encoding="utf-8"))
        steps = parsed["jobs"]["build-and-publish"]["steps"]

        for step in steps:
            uses = step.get("uses")
            if not uses or not str(uses).startswith("./"):
                continue

            action_dir = REPO_ROOT / str(uses)[2:]
            has_entrypoint = any(
                (action_dir / name).exists()
                for name in ("action.yml", "action.yaml", "Dockerfile")
            )
            assert has_entrypoint, (
                f"Local action path is missing an entrypoint: {action_dir}"
            )
