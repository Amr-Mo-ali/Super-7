"""Synthetic-only Compose resolution for the local separated trial."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from scripts import durable_compose_setup as setup


def test_compose_resolves_scoped_roles_with_synthetic_credentials(tmp_path: Path) -> None:
    if shutil.which("docker") is None:
        pytest.skip("Docker Compose CLI is unavailable")
    (tmp_path / "docker-compose.durable-trial.yml").write_bytes(
        (setup.ROOT / "docker-compose.durable-trial.yml").read_bytes()
    )
    secret_dir = tmp_path / ".local-durable-compose"
    secret_dir.mkdir()
    config = setup._new_config()
    (secret_dir / "compose.env").write_text(
        f"TRIAL_BOOTSTRAP_USER={config['bootstrap_user']}\nTRIAL_OWNER_ID={config['owner_id']}\n",
        encoding="utf-8",
    )
    (secret_dir / "postgres-password.secret").write_text("synthetic\n", encoding="utf-8")
    (secret_dir / "bootstrap-config.json").write_text(
        setup._bootstrap_config(config), encoding="utf-8"
    )
    (secret_dir / "inspect.env").write_text(setup._inspect_env(config), encoding="utf-8")
    for role in ("api", "worker", "dispatcher"):
        (secret_dir / f"{role}.env").write_text(setup._role_env(config, role), encoding="utf-8")
    result = subprocess.run(
        (
            "docker",
            "compose",
            "--env-file",
            str(secret_dir / "compose.env"),
            "-f",
            str(tmp_path / "docker-compose.durable-trial.yml"),
            "config",
            "--format",
            "json",
        ),
        cwd=tmp_path,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, "Synthetic Compose resolution failed"
    resolved = json.loads(result.stdout)
    services = resolved["services"]
    assert services["durable-api"]["command"][1] == "durable_api_main:create_app"
    assert services["durable-worker"]["command"] == ["python", "-m", "durable_worker_main"]
    assert services["durable-dispatcher"]["command"] == ["python", "-m", "durable_dispatcher_main"]
    assert services["durable-api"]["ports"][0]["host_ip"] == "127.0.0.1"
    assert services["durable-api"]["ports"][0]["published"] == "18080"
    assert "ports" not in services["postgres"]
    assert "volumes" not in services["durable-api"]
    assert "volumes" not in services["durable-dispatcher"]
    worker_mounts = services["durable-worker"]["volumes"]
    assert {(mount["target"], mount["read_only"]) for mount in worker_mounts} == {
        ("/models", True),
        ("/videos", True),
    }
    assert services["durable-worker"]["healthcheck"]["disable"] is True
    assert services["durable-dispatcher"]["healthcheck"]["disable"] is True
    assert set(services["durable-api"]["environment"]) == set(
        dict(line.split("=", 1) for line in setup._role_env(config, "api").splitlines())
    )
    for role in ("worker", "dispatcher"):
        environment = services[f"durable-{role}"]["environment"]
        assert "SUPER7_APEX_BEARER_SECRET" not in environment
        assert "SUPER7_IDEMPOTENCY_HMAC_SECRET_HEX" not in environment
    assert services["postgres"]["volumes"][0]["source"] == "trial_pgdata"
    assert resolved["volumes"]["trial_pgdata"]["name"] == setup.VOLUME
