"""Database-free boundaries for the separate persistent Compose trial."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml  # type: ignore[import-untyped]

from scripts import durable_compose_setup as setup


def test_manifest_is_isolated_and_keeps_the_runtime_single_process() -> None:
    manifest = yaml.safe_load((setup.ROOT / "docker-compose.durable-trial.yml").read_text())
    services = manifest["services"]
    assert manifest["name"] == setup.PROJECT
    assert set(services) == {"postgres", "bootstrap", "durable-api", "inspect"}
    assert services["postgres"]["image"] == setup.POSTGRES_IMAGE
    assert services["postgres"]["pull_policy"] == "never"
    assert "ports" not in services["postgres"]
    assert services["postgres"]["volumes"] == ["trial_pgdata:/var/lib/postgresql/data"]
    assert services["durable-api"]["ports"] == ["127.0.0.1:18080:8000"]
    assert services["durable-api"]["image"] == setup.APP_IMAGE
    assert services["durable-api"]["pull_policy"] == "never"
    assert services["durable-api"]["command"] == [
        "uvicorn",
        "durable_local_main:create_app",
        "--factory",
        "--host",
        "0.0.0.0",
        "--port",
        "8000",
    ]
    assert services["durable-api"]["volumes"] == [
        "./models:/models:ro",
        "./dataset/raw/social_media:/videos:ro",
    ]
    assert "health/ready" in services["durable-api"]["healthcheck"]["test"][1]
    assert services["bootstrap"]["profiles"] == ["bootstrap"]
    assert services["inspect"]["profiles"] == ["inspect"]
    assert services["postgres"]["secrets"] == ["postgres_password"]
    assert "env_file" not in services["postgres"]
    assert set(services["postgres"]["environment"]) == {
        "POSTGRES_USER",
        "POSTGRES_DB",
        "POSTGRES_PASSWORD_FILE",
    }
    assert services["bootstrap"]["secrets"] == ["postgres_password", "bootstrap_config"]
    assert "env_file" not in services["bootstrap"]
    assert "environment" not in services["bootstrap"]
    assert services["durable-api"]["env_file"] == ["./.local-durable-compose/runtime.env"]
    assert "secrets" not in services["durable-api"]
    assert services["inspect"]["env_file"] == ["./.local-durable-compose/inspect.env"]
    assert "secrets" not in services["inspect"]
    assert "environment" not in services["inspect"]
    assert manifest["secrets"]["bootstrap_config"]["file"] == (
        "./.local-durable-compose/bootstrap-config.json"
    )
    for one_shot in ("bootstrap", "inspect"):
        assert services[one_shot]["volumes"] == [
            "./scripts/durable_compose_setup.py:/app/src/durable_compose_setup.py:ro"
        ]
    assert manifest["volumes"]["trial_pgdata"]["name"] == setup.VOLUME


def test_runtime_configuration_uses_only_runtime_credentials() -> None:
    config = setup._new_config()
    values = dict(line.split("=", 1) for line in setup._runtime_env(config).splitlines())
    assert values["POSTGRES_HOST"] == "postgres"
    assert values["POSTGRES_PORT"] == "5432"
    assert values["POSTGRES_USER"] == config["runtime_user"]
    assert values["POSTGRES_PASSWORD"] == config["runtime_password"]
    assert values["SUPER7_DURABLE_LOCAL_ENABLED"] == "1"
    assert values["SUPER7_DURABLE_MODEL_ID"] == config["model_id"]
    assert values["VIDEO_STORAGE_ROOT"] == "/videos"
    assert values["MODEL_PATH"] == "/models/yolo11n.pt"
    assert config["bootstrap_password"] not in values.values()
    assert config["migration_password"] not in values.values()
    assert config["runtime_password"] != config["migration_password"]
    assert config["runtime_password"] != config["bootstrap_password"]


def test_utility_configuration_excludes_api_and_unneeded_credentials() -> None:
    config = setup._new_config()
    bootstrap = json.loads(setup._bootstrap_config(config))
    inspect = dict(line.split("=", 1) for line in setup._inspect_env(config).splitlines())
    assert set(bootstrap) == {
        "project",
        "volume",
        "database",
        "bootstrap_user",
        "migration_user",
        "migration_password",
        "runtime_user",
        "runtime_password",
    }
    assert bootstrap["migration_password"] == config["migration_password"]
    assert bootstrap["runtime_password"] == config["runtime_password"]
    assert inspect == {
        "POSTGRES_DATABASE": config["database"],
        "POSTGRES_USER": config["runtime_user"],
        "POSTGRES_PASSWORD": config["runtime_password"],
    }
    for secret in (
        config["apex_bearer_secret"],
        config["idempotency_hmac_secret_hex"],
        config["bootstrap_password"],
    ):
        assert secret not in setup._bootstrap_config(config)
        assert secret not in setup._inspect_env(config)


def test_prepare_refuses_existing_credentials_before_docker_or_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "secrets"
    directory.mkdir()
    marker = directory / "config.json"
    marker.write_text("existing", encoding="utf-8")
    monkeypatch.setattr(setup, "SECRET_DIR", directory)
    monkeypatch.setattr(setup, "_docker", lambda *_: pytest.fail("Docker must not be reached"))
    with pytest.raises(setup.TrialSetupError, match="already exists"):
        setup.prepare()
    assert marker.read_text(encoding="utf-8") == "existing"


def test_main_reports_fixed_prepare_refusal_without_exposing_unexpected_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = tmp_path / "existing-secrets"
    directory.mkdir()
    monkeypatch.setattr(setup, "SECRET_DIR", directory)
    monkeypatch.setattr(sys, "argv", ["durable_compose_setup.py", "prepare"])
    monkeypatch.setattr(setup, "_docker", lambda *_: pytest.fail("Docker must not be reached"))
    assert setup.main() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Trial credential directory already exists; refusing to adopt it." in captured.err

    private_detail = "private-token-must-not-appear"

    def unexpected_prepare() -> None:
        raise RuntimeError(private_detail)

    monkeypatch.setattr(setup, "prepare", unexpected_prepare)
    assert setup.main() == 1
    captured = capsys.readouterr()
    assert private_detail not in captured.err
    assert "RuntimeError" in captured.err


def test_prepare_writes_new_ignored_configuration_without_starting_docker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = tmp_path / "new-secrets"
    monkeypatch.setattr(setup, "SECRET_DIR", directory)
    monkeypatch.setattr(setup, "CONFIG", directory / "config.json")
    monkeypatch.setattr(setup, "POSTGRES_PASSWORD", directory / "postgres-password.secret")
    monkeypatch.setattr(setup, "COMPOSE_ENV", directory / "compose.env")
    monkeypatch.setattr(setup, "RUNTIME_ENV", directory / "runtime.env")
    monkeypatch.setattr(setup, "BOOTSTRAP_CONFIG", directory / "bootstrap-config.json")
    monkeypatch.setattr(setup, "INSPECT_ENV", directory / "inspect.env")
    original_check = setup._check_prepare_prerequisites
    monkeypatch.setattr(setup, "_check_prepare_prerequisites", lambda: None)
    monkeypatch.setattr(setup, "_docker", lambda *_: pytest.fail("Docker must not be started"))
    setup.prepare()
    config = json.loads((directory / "config.json").read_text(encoding="utf-8"))
    assert config["project"] == setup.PROJECT
    assert config["volume"] == setup.VOLUME
    assert (directory / "postgres-password.secret").read_text(encoding="utf-8").strip() == (
        config["bootstrap_password"]
    )
    assert "TRIAL_BOOTSTRAP_USER=" in (directory / "compose.env").read_text(encoding="utf-8")
    assert "POSTGRES_PASSWORD=" in (directory / "runtime.env").read_text(encoding="utf-8")
    assert json.loads((directory / "bootstrap-config.json").read_text(encoding="utf-8")) == (
        json.loads(setup._bootstrap_config(config))
    )
    assert (directory / "inspect.env").read_text(encoding="utf-8") == setup._inspect_env(config)
    output = capsys.readouterr().out
    assert config["bootstrap_password"] not in output
    assert config["runtime_password"] not in output
    assert config["apex_bearer_secret"] not in output
    monkeypatch.setattr(setup, "_check_prepare_prerequisites", original_check)
    with pytest.raises(setup.TrialSetupError, match="already exists"):
        setup.prepare()


def test_derive_service_files_preserves_existing_credentials_and_refuses_collisions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = tmp_path / "existing-secrets"
    directory.mkdir()
    paths = {
        "CONFIG": directory / "config.json",
        "POSTGRES_PASSWORD": directory / "postgres-password.secret",
        "COMPOSE_ENV": directory / "compose.env",
        "RUNTIME_ENV": directory / "runtime.env",
        "BOOTSTRAP_CONFIG": directory / "bootstrap-config.json",
        "INSPECT_ENV": directory / "inspect.env",
    }
    for name, path in paths.items():
        monkeypatch.setattr(setup, name, path)
    config = setup._new_config()
    paths["CONFIG"].write_text(json.dumps(config) + "\n", encoding="utf-8")
    paths["POSTGRES_PASSWORD"].write_text(config["bootstrap_password"] + "\n", encoding="utf-8")
    paths["COMPOSE_ENV"].write_text(
        f"TRIAL_BOOTSTRAP_USER={config['bootstrap_user']}\nTRIAL_OWNER_ID={config['owner_id']}\n",
        encoding="utf-8",
    )
    paths["RUNTIME_ENV"].write_text(setup._runtime_env(config), encoding="utf-8")
    original = {name: path.read_bytes() for name, path in paths.items() if path.exists()}
    monkeypatch.setattr(
        subprocess, "run", lambda *_, **__: subprocess.CompletedProcess(["git"], 0, "", "")
    )
    setup.derive_service_files()
    assert {name: paths[name].read_bytes() for name in original} == original
    assert paths["BOOTSTRAP_CONFIG"].read_text(encoding="utf-8") == setup._bootstrap_config(config)
    assert paths["INSPECT_ENV"].read_text(encoding="utf-8") == setup._inspect_env(config)
    output = capsys.readouterr().out
    for secret in (config["apex_bearer_secret"], config["idempotency_hmac_secret_hex"]):
        assert secret not in output
    with pytest.raises(setup.TrialSetupError, match="already exists"):
        setup.derive_service_files()
    assert {name: paths[name].read_bytes() for name in original} == original


def test_derive_service_files_refuses_either_output_collision_before_source_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(setup, "CONFIG", tmp_path / "missing-config.json")
    bootstrap = tmp_path / "bootstrap-config.json"
    inspect = tmp_path / "inspect.env"
    monkeypatch.setattr(setup, "BOOTSTRAP_CONFIG", bootstrap)
    monkeypatch.setattr(setup, "INSPECT_ENV", inspect)
    for collision, other in ((bootstrap, inspect), (inspect, bootstrap)):
        collision.write_text("existing", encoding="utf-8")
        with pytest.raises(setup.TrialSetupError, match="already exists"):
            setup.derive_service_files()
        assert collision.read_text(encoding="utf-8") == "existing"
        assert not other.exists()
        collision.unlink()


def test_derive_service_files_mismatch_has_safe_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = "private-value-must-not-appear"
    config = setup._new_config()
    config["apex_bearer_secret"] = secret
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setattr(setup, "CONFIG", config_path)
    monkeypatch.setattr(setup, "POSTGRES_PASSWORD", tmp_path / "missing-password.secret")
    monkeypatch.setattr(setup, "BOOTSTRAP_CONFIG", tmp_path / "bootstrap-config.json")
    monkeypatch.setattr(setup, "INSPECT_ENV", tmp_path / "inspect.env")
    monkeypatch.setattr(sys, "argv", ["durable_compose_setup.py", "derive-service-files"])
    assert setup.main() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "missing or inconsistent" in captured.err
    assert secret not in captured.err
    assert not (tmp_path / "bootstrap-config.json").exists()
    assert not (tmp_path / "inspect.env").exists()


def test_bootstrap_refuses_wrong_trial_identity_before_connecting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text('{"project":"not-this-trial","volume":"wrong"}', encoding="utf-8")
    monkeypatch.setattr(setup, "CONTAINER_CONFIG", config_path)
    monkeypatch.setattr(setup, "_connect", lambda *_, **__: pytest.fail("Must not connect"))
    with pytest.raises(setup.TrialSetupError, match="mismatched"):
        setup._container_config()


def test_prepare_rejects_image_mismatch_and_volume_collision_before_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    (root / "models").mkdir(parents=True)
    (root / "models" / "yolo11n.pt").write_bytes(b"model")
    (root / "dataset" / "raw" / "social_media").mkdir(parents=True)
    directory = root / ".local-durable-compose"
    monkeypatch.setattr(setup, "ROOT", root)
    monkeypatch.setattr(setup, "SECRET_DIR", directory)
    monkeypatch.setattr(setup, "CONFIG", directory / "config.json")
    monkeypatch.setattr(setup, "POSTGRES_PASSWORD", directory / "postgres-password.secret")
    monkeypatch.setattr(setup, "COMPOSE_ENV", directory / "compose.env")
    monkeypatch.setattr(setup, "RUNTIME_ENV", directory / "runtime.env")
    monkeypatch.setattr(setup, "BOOTSTRAP_CONFIG", directory / "bootstrap-config.json")
    monkeypatch.setattr(setup, "INSPECT_ENV", directory / "inspect.env")
    monkeypatch.setattr(
        subprocess, "run", lambda *_, **__: subprocess.CompletedProcess(["git"], 0, "", "")
    )
    monkeypatch.setattr(
        setup,
        "_docker",
        lambda *args: (
            json.dumps({"Os": "linux", "Architecture": "amd64", "RepoDigests": []})
            if args[:2] == ("image", "inspect")
            else pytest.fail("No later Docker inspection expected")
        ),
    )
    with pytest.raises(setup.TrialSetupError, match="not cached"):
        setup.prepare()
    assert not directory.exists()

    def volume_collision(*args: str) -> str:
        if args[:2] == ("image", "inspect"):
            return json.dumps(
                {
                    "Os": "linux",
                    "Architecture": "amd64",
                    "RepoDigests": [setup.POSTGRES_DIGEST],
                }
            )
        if args[:2] == ("volume", "ls"):
            return setup.VOLUME
        pytest.fail("No later Docker inspection expected")

    monkeypatch.setattr(setup, "_docker", volume_collision)
    with pytest.raises(setup.TrialSetupError, match="volume name"):
        setup.prepare()
    assert not directory.exists()


def test_bootstrap_refuses_existing_role_before_ddl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = setup._new_config()
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setattr(setup, "CONTAINER_CONFIG", config_path)
    monkeypatch.setattr(
        setup, "read_migrations", lambda *_: [SimpleNamespace(id=item) for item in setup.CATALOG]
    )
    statements: list[str] = []

    class ExistingRoleConnection:
        def __enter__(self) -> ExistingRoleConnection:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def execute(self, query: str, _parameters: object) -> SimpleNamespace:
            statements.append(query)
            return SimpleNamespace(fetchone=lambda: (1,))

    monkeypatch.setattr(setup, "_connect", lambda *_, **__: ExistingRoleConnection())
    with pytest.raises(setup.TrialSetupError, match="already exist"):
        setup.bootstrap()
    assert statements == ["SELECT 1 FROM pg_roles WHERE rolname = ANY(%s)"]


def test_volume_verification_requires_the_generated_owner_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = setup._new_config()
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setattr(setup, "CONFIG", config_path)
    labels = {"com.super7.durable-compose-trial.owner": config["owner_id"]}
    volume = {"Name": setup.VOLUME, "Labels": labels}
    monkeypatch.setattr(setup, "_docker", lambda *_: json.dumps(volume))
    setup.verify_volume()
    assert (
        capsys.readouterr().out == "Trial persistent volume identity verified; no data was read.\n"
    )
    labels["com.super7.durable-compose-trial.owner"] = "somebody-else"
    with pytest.raises(setup.TrialSetupError, match="does not match"):
        setup.verify_volume()
