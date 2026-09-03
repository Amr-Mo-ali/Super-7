"""Tests-first contract for manual, reviewed-SHA production deployment."""

import re
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "deploy.yml"
SSH_ACTION = "appleboy/ssh-action@v1.2.0"


def _workflow() -> dict[str, Any]:
    parsed = yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert isinstance(parsed, dict)
    return parsed


def _trigger(workflow: dict[str, Any]) -> dict[str, Any]:
    trigger = workflow.get("on")
    assert isinstance(trigger, dict)
    return trigger


def _jobs(workflow: dict[str, Any]) -> dict[str, Any]:
    jobs = workflow.get("jobs")
    assert isinstance(jobs, dict)
    return jobs


def _run_steps(job: dict[str, Any]) -> list[str]:
    steps = job.get("steps")
    assert isinstance(steps, list)
    return [step["run"] for step in steps if isinstance(step, dict) and "run" in step]


def _ssh_step(job: dict[str, Any]) -> dict[str, Any]:
    steps = job.get("steps")
    assert isinstance(steps, list)
    for step in steps:
        if isinstance(step, dict) and step.get("uses") == SSH_ACTION:
            return step
    raise AssertionError("approved SSH action step is missing")


def _all_shell(workflow: dict[str, Any]) -> str:
    chunks: list[str] = []
    for job in _jobs(workflow).values():
        if not isinstance(job, dict):
            continue
        chunks.extend(_run_steps(job))
        try:
            chunks.append(_ssh_step(job)["with"]["script"])
        except (AssertionError, KeyError, TypeError):
            pass
    return "\n".join(chunks)


def test_deployment_has_only_manual_dispatch_trigger() -> None:
    trigger = _trigger(_workflow())
    assert set(trigger) == {"workflow_dispatch"}


def test_manual_dispatch_requires_deploy_sha_input() -> None:
    dispatch = _trigger(_workflow())["workflow_dispatch"]
    assert isinstance(dispatch, dict)
    inputs = dispatch["inputs"]
    assert isinstance(inputs, dict)
    deploy_sha = inputs["deploy_sha"]
    assert isinstance(deploy_sha, dict)
    assert deploy_sha["required"] == "true"
    assert deploy_sha["type"] == "string"


def test_deploy_sha_is_validated_as_full_commit_sha_before_ssh() -> None:
    jobs = _jobs(_workflow())
    validate = jobs["validate"]
    assert isinstance(validate, dict)
    shell = "\n".join(_run_steps(validate))
    assert re.search(r"\^\[0-9a-f\]\{40\}\$", shell)
    assert shell.index("deploy_sha") < shell.index("GITHUB_OUTPUT")


def test_main_workflow_ref_is_required_for_validation_and_deployment() -> None:
    jobs = _jobs(_workflow())
    for name in ("validate", "deploy"):
        job = jobs[name]
        assert isinstance(job, dict)
        assert "github.ref == 'refs/heads/main'" in str(job.get("if", ""))


def test_requested_sha_is_checked_for_main_ancestry_after_explicit_fetch() -> None:
    workflow = _workflow()
    jobs = _jobs(workflow)
    validate = jobs["validate"]
    deploy = jobs["deploy"]
    assert isinstance(validate, dict) and isinstance(deploy, dict)
    expected = "git fetch --no-tags --quiet origin main:refs/remotes/origin/main"
    validate_shell = "\n".join(_run_steps(validate))
    remote_shell = _ssh_step(deploy)["with"]["script"]
    assert expected in validate_shell
    assert expected in remote_shell
    assert "git merge-base --is-ancestor" in validate_shell
    assert "git merge-base --is-ancestor" in remote_shell
    assert "refs/remotes/origin/main" in validate_shell
    assert "refs/remotes/origin/main" in remote_shell


def test_exact_ci_success_is_checked_for_requested_sha() -> None:
    workflow = _workflow()
    jobs = _jobs(workflow)
    validate = jobs["validate"]
    assert isinstance(validate, dict)
    shell = "\n".join(_run_steps(validate))
    assert "ci.yml" in shell
    assert "--method GET" in shell
    assert ".head_sha == $sha" in shell
    assert '.head_branch == "main"' in shell
    assert '.event == "push"' in shell
    assert '.status == "completed"' in shell
    assert '.conclusion == "success"' in shell
    assert "deploy_sha" in shell


def test_deployment_depends_on_validation_job() -> None:
    deploy = _jobs(_workflow())["deploy"]
    assert isinstance(deploy, dict)
    assert deploy["needs"] == ["validate"]


def test_deployment_consumes_validated_output_not_raw_input() -> None:
    workflow = _workflow()
    deploy = _jobs(workflow)["deploy"]
    assert isinstance(deploy, dict)
    serialized = repr(deploy)
    assert "needs.validate.outputs.deploy_sha" in serialized
    assert "github.event.inputs.deploy_sha" not in serialized
    assert "inputs.deploy_sha" not in serialized


def test_exact_validated_sha_is_passed_to_server() -> None:
    deploy = _jobs(_workflow())["deploy"]
    assert isinstance(deploy, dict)
    env = deploy["env"]
    assert isinstance(env, dict)
    assert env["DEPLOY_SHA"] == "${{ needs.validate.outputs.deploy_sha }}"
    ssh = _ssh_step(deploy)
    assert ssh["with"]["envs"] == "DEPLOY_SHA"


def test_latest_main_pull_is_absent_from_parsed_shell() -> None:
    assert "git pull --ff-only origin main" not in _all_shell(_workflow())


def test_server_checks_out_and_verifies_exact_sha_before_compose_mutation() -> None:
    deploy = _jobs(_workflow())["deploy"]
    assert isinstance(deploy, dict)
    script = _ssh_step(deploy)["with"]["script"]
    for marker in (
        "git fetch --no-tags --quiet origin main:refs/remotes/origin/main",
        "git cat-file -e",
        "git merge-base --is-ancestor",
        "git checkout --detach",
        "git rev-parse HEAD",
        "docker compose config --quiet",
    ):
        assert marker in script
        assert script.index(marker) < script.index("docker compose up -d")


def test_production_environment_is_preserved() -> None:
    deploy = _jobs(_workflow())["deploy"]
    assert isinstance(deploy, dict)
    assert deploy["environment"] == "production"


def test_production_concurrency_is_preserved() -> None:
    workflow = _workflow()
    concurrency = workflow["concurrency"]
    assert isinstance(concurrency, dict)
    assert concurrency["group"] == "production-deploy"
    assert concurrency["cancel-in-progress"] == "false"


def test_validation_precedes_production_ssh_mutation() -> None:
    workflow = _workflow()
    jobs = _jobs(workflow)
    deploy = jobs["deploy"]
    assert isinstance(deploy, dict)
    assert deploy["needs"] == ["validate"]
    validate = jobs["validate"]
    assert isinstance(validate, dict)
    assert _run_steps(validate)
    assert _ssh_step(deploy)["with"]["script"].index("docker compose up -d") > 0


def test_critical_contract_markers_are_parsed_not_comment_matches() -> None:
    workflow = _workflow()
    assert set(_trigger(workflow)) == {"workflow_dispatch"}
    assert set(_jobs(workflow)) == {"validate", "deploy"}
    assert _ssh_step(_jobs(workflow)["deploy"])["with"]["envs"] == "DEPLOY_SHA"
