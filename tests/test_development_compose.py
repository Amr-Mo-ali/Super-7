"""Synthetic Compose checks for the existing Development database topology."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _fixture(tmp_path: Path) -> tuple[Path, Path]:
    manifest = tmp_path / "docker-compose.development.yml"
    manifest.write_bytes((ROOT / manifest.name).read_bytes())
    (tmp_path / "models").mkdir()
    private = tmp_path / "private"
    private.mkdir()
    common = "POSTGRES_PASSWORD=dummy-password\n"
    (private / "api.env").write_text(
        common
        + "SUPER7_APEX_BEARER_SECRET=dummy-bearer\n"
        + "SUPER7_IDEMPOTENCY_HMAC_SECRET_HEX="
        + "a" * 64
        + "\n"
        + "SUPER7_DURABLE_ATTEMPT_LIMIT=3\n"
        + "SUPER7_DURABLE_DB_OPERATION_TIMEOUT_MS=5000\n"
        + "SUPER7_DURABLE_DB_LOCK_TIMEOUT_MS=1000\n",
        encoding="utf-8",
    )
    (private / "worker.env").write_text(
        common
        + "SUPER7_R2_ENDPOINT=https://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.r2.cloudflarestorage.com\n"
        + "SUPER7_R2_ACCESS_KEY_ID=dummy-key\n"
        + "SUPER7_R2_SECRET_ACCESS_KEY=dummy-secret\n"
        + "SUPER7_DURABLE_MODEL_ID=dummy-model\n"
        + "SUPER7_DURABLE_RECOVERY_BATCH=2\n"
        + "SUPER7_DURABLE_WORKER_IDLE_SECONDS=0.1\n"
        + "SUPER7_DURABLE_LEASE_SECONDS=180\n"
        + "SUPER7_DURABLE_HEARTBEAT_SECONDS=20\n"
        + "SUPER7_DURABLE_WATCHDOG_SECONDS=25\n"
        + "SUPER7_DURABLE_JITTER_SECONDS=10\n"
        + "SUPER7_DURABLE_FINALIZE_SECONDS=90\n"
        + "SUPER7_DURABLE_FINALIZE_RETRY_ONE_SECONDS=2\n"
        + "SUPER7_DURABLE_FINALIZE_RETRY_TWO_SECONDS=5\n"
        + "SUPER7_DURABLE_DB_OPERATION_TIMEOUT_MS=5000\n"
        + "SUPER7_DURABLE_DB_LOCK_TIMEOUT_MS=1000\n",
        encoding="utf-8",
    )
    (private / "dispatcher.env").write_text(common, encoding="utf-8")
    interpolation = private / "compose.env"
    interpolation.write_text(
        "SUPER7_DEV_IMAGE=football-analysis:dev-dummy\n"
        f"SUPER7_DEV_API_ENV={private / 'api.env'}\n"
        f"SUPER7_DEV_WORKER_ENV={private / 'worker.env'}\n"
        f"SUPER7_DEV_DISPATCHER_ENV={private / 'dispatcher.env'}\n",
        encoding="utf-8",
    )
    return manifest, interpolation


def _config(manifest: Path, interpolation: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        (
            "docker",
            "compose",
            "--env-file",
            str(interpolation),
            "-f",
            str(manifest),
            "config",
            "--format",
            "json",
        ),
        cwd=manifest.parent,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )


def test_development_compose_roles_and_global_capacity(tmp_path: Path) -> None:
    if shutil.which("docker") is None:
        pytest.skip("Docker Compose CLI is unavailable")
    manifest, interpolation = _fixture(tmp_path)
    result = _config(manifest, interpolation)
    assert result.returncode == 0, result.stderr
    resolved = json.loads(result.stdout)
    services = resolved["services"]
    assert set(services) == {"api", "worker", "dispatcher"}
    assert resolved["name"] == "super7-development"
    assert resolved["networks"]["database"]["external"] is True
    assert resolved["networks"]["database"]["name"] == "super7-net"
    assert {service["image"] for service in services.values()} == {"football-analysis:dev-dummy"}
    assert all(service["pull_policy"] == "never" for service in services.values())
    assert all(
        service["environment"]["POSTGRES_DATABASE"] == "super7" for service in services.values()
    )
    assert all(
        service["environment"]["POSTGRES_USER"] == "super7_runtime" for service in services.values()
    )
    assert services["api"]["environment"]["MAX_QUEUED_ANALYSES"] == "300"
    assert services["api"]["ports"][0]["host_ip"] == "127.0.0.1"
    assert services["api"]["ports"][0]["published"] == "18080"
    assert "ports" not in services["worker"]
    assert "ports" not in services["dispatcher"]
    assert "container_name" not in services["worker"]
    assert "volumes" not in services["api"]
    assert "volumes" not in services["dispatcher"]
    assert services["worker"]["volumes"][0]["target"] == "/models"
    assert services["worker"]["volumes"][0]["read_only"] is True
    assert services["worker"]["working_dir"] == "/tmp"
    assert services["worker"]["environment"]["MODEL_DEVICE"] == "cuda:0"
    assert services["worker"]["environment"]["HOME"] == "/tmp"
    assert services["worker"]["environment"]["YOLO_CONFIG_DIR"] == "/tmp/ultralytics"
    assert services["worker"]["environment"]["DEBUG_ARTIFACTS_ENABLED"] == "false"
    assert services["worker"]["environment"]["SUPER7_VIDEO_SOURCE"] == "r2"
    assert "gpus" in services["worker"]
    assert "gpus" not in services["api"]
    assert "gpus" not in services["dispatcher"]
    assert services["worker"]["healthcheck"]["disable"] is True
    assert services["dispatcher"]["healthcheck"]["disable"] is True
    for role in ("worker", "dispatcher"):
        assert "SUPER7_APEX_BEARER_SECRET" not in services[role]["environment"]
        assert "SUPER7_IDEMPOTENCY_HMAC_SECRET_HEX" not in services[role]["environment"]
    for role in ("api", "dispatcher"):
        assert "SUPER7_R2_ACCESS_KEY_ID" not in services[role]["environment"]
        assert "SUPER7_R2_SECRET_ACCESS_KEY" not in services[role]["environment"]


def test_development_compose_requires_image_selection(tmp_path: Path) -> None:
    if shutil.which("docker") is None:
        pytest.skip("Docker Compose CLI is unavailable")
    manifest, interpolation = _fixture(tmp_path)
    interpolation.write_text(
        interpolation.read_text(encoding="utf-8").replace(
            "SUPER7_DEV_IMAGE=football-analysis:dev-dummy\n", ""
        ),
        encoding="utf-8",
    )
    result = _config(manifest, interpolation)
    assert result.returncode != 0
    assert "Set a verified Development image reference" in result.stderr


def test_development_compose_requires_each_role_file(tmp_path: Path) -> None:
    if shutil.which("docker") is None:
        pytest.skip("Docker Compose CLI is unavailable")
    manifest, interpolation = _fixture(tmp_path)
    (tmp_path / "private" / "worker.env").unlink()
    result = _config(manifest, interpolation)
    assert result.returncode != 0
    assert "worker.env" in result.stderr
