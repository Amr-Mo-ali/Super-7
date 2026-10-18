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


def test_manifest_is_isolated_and_separates_runtime_roles() -> None:
    manifest = yaml.safe_load((setup.ROOT / "docker-compose.durable-trial.yml").read_text())
    services = manifest["services"]
    assert manifest["name"] == setup.PROJECT
    assert set(services) == {
        "postgres",
        "bootstrap",
        "durable-api",
        "durable-worker",
        "durable-dispatcher",
        "inspect",
    }
    assert services["postgres"]["image"] == setup.POSTGRES_IMAGE
    assert services["postgres"]["pull_policy"] == "never"
    assert "ports" not in services["postgres"]
    assert services["postgres"]["volumes"] == ["trial_pgdata:/var/lib/postgresql/data"]
    assert services["durable-api"]["ports"] == ["127.0.0.1:18080:8000"]
    assert services["durable-api"]["image"] == setup.APP_IMAGE
    assert services["durable-api"]["pull_policy"] == "never"
    assert services["durable-api"]["command"] == [
        "uvicorn",
        "durable_api_main:create_app",
        "--factory",
        "--host",
        "0.0.0.0",
        "--port",
        "8000",
    ]
    assert "volumes" not in services["durable-api"]
    assert "volumes" not in services["durable-dispatcher"]
    assert services["durable-worker"]["volumes"] == [
        "./models:/models:ro",
        "./dataset/raw/social_media:/videos:ro",
    ]
    assert services["durable-worker"]["command"] == ["python", "-m", "durable_worker_main"]
    assert services["durable-dispatcher"]["command"] == ["python", "-m", "durable_dispatcher_main"]
    for role in ("durable-worker", "durable-dispatcher"):
        assert "ports" not in services[role]
        assert services[role]["healthcheck"] == {"disable": True}
        assert services[role]["restart"] == "no"
        assert services[role]["depends_on"] == {"postgres": {"condition": "service_healthy"}}
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
    assert services["durable-api"]["env_file"] == ["./.local-durable-compose/api.env"]
    assert "secrets" not in services["durable-api"]
    for role in ("worker", "dispatcher"):
        assert services[f"durable-{role}"]["env_file"] == [f"./.local-durable-compose/{role}.env"]
        assert "secrets" not in services[f"durable-{role}"]
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


def test_role_environments_have_only_consumed_settings() -> None:
    config = setup._new_config()
    roles = {
        role: dict(line.split("=", 1) for line in setup._role_env(config, role).splitlines())
        for role in ("api", "worker", "dispatcher")
    }
    for role, values in roles.items():
        assert values[f"SUPER7_DURABLE_{role.upper()}_ENABLED"] == "1"
        assert values["POSTGRES_USER"] == config["runtime_user"]
        assert values["POSTGRES_PASSWORD"] == config["runtime_password"]
        assert "SUPER7_DURABLE_LOCAL_ENABLED" not in values
        assert config["bootstrap_password"] not in values.values()
        assert config["migration_password"] not in values.values()
    assert roles["api"]["SUPER7_APEX_BEARER_SECRET"] == config["apex_bearer_secret"]
    assert (
        roles["api"]["SUPER7_IDEMPOTENCY_HMAC_SECRET_HEX"] == config["idempotency_hmac_secret_hex"]
    )
    assert "MODEL_PATH" not in roles["api"]
    assert "VIDEO_STORAGE_ROOT" not in roles["api"]
    assert roles["worker"]["MODEL_PATH"] == "/models/yolo11n.pt"
    assert roles["worker"]["VIDEO_STORAGE_ROOT"] == "/videos"
    assert "SUPER7_APEX_BEARER_SECRET" not in roles["worker"]
    assert "SUPER7_IDEMPOTENCY_HMAC_SECRET_HEX" not in roles["worker"]
    database_keys = {
        key for key in setup._runtime_env(config).splitlines() if key.startswith("POSTGRES_")
    }
    assert set(roles["dispatcher"]) == {
        "SUPER7_DURABLE_DISPATCHER_ENABLED",
        *(line.split("=", 1)[0] for line in database_keys),
    }


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
    monkeypatch.setattr(setup, "API_ENV", directory / "api.env")
    monkeypatch.setattr(setup, "WORKER_ENV", directory / "worker.env")
    monkeypatch.setattr(setup, "DISPATCHER_ENV", directory / "dispatcher.env")
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
    for role in ("api", "worker", "dispatcher"):
        assert (directory / f"{role}.env").read_text(encoding="utf-8") == setup._role_env(
            config, role
        )
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


def test_derive_role_files_preserves_existing_credentials_and_refuses_collisions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / ".local-durable-compose"
    directory.mkdir()
    names = (
        "CONFIG",
        "POSTGRES_PASSWORD",
        "COMPOSE_ENV",
        "RUNTIME_ENV",
        "BOOTSTRAP_CONFIG",
        "INSPECT_ENV",
        "API_ENV",
        "WORKER_ENV",
        "DISPATCHER_ENV",
    )
    filenames = (
        "config.json",
        "postgres-password.secret",
        "compose.env",
        "runtime.env",
        "bootstrap-config.json",
        "inspect.env",
        "api.env",
        "worker.env",
        "dispatcher.env",
    )
    paths = dict(zip(names, (directory / name for name in filenames), strict=True))
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
    paths["BOOTSTRAP_CONFIG"].write_text(setup._bootstrap_config(config), encoding="utf-8")
    paths["INSPECT_ENV"].write_text(setup._inspect_env(config), encoding="utf-8")
    original = {name: path.read_bytes() for name, path in paths.items() if path.exists()}
    monkeypatch.setattr(
        subprocess, "run", lambda *_, **__: subprocess.CompletedProcess(["git"], 0, "", "")
    )
    setup.derive_role_files()
    assert {name: paths[name].read_bytes() for name in original} == original
    for role in ("api", "worker", "dispatcher"):
        assert paths[f"{role.upper()}_ENV"].read_text(encoding="utf-8") == setup._role_env(
            config, role
        )
    with pytest.raises(setup.TrialSetupError, match="already exists"):
        setup.derive_role_files()
    assert {name: paths[name].read_bytes() for name in original} == original


def test_derive_role_files_refuses_partial_or_inconsistent_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = tmp_path / "api.env"
    worker = tmp_path / "worker.env"
    dispatcher = tmp_path / "dispatcher.env"
    monkeypatch.setattr(setup, "API_ENV", api)
    monkeypatch.setattr(setup, "WORKER_ENV", worker)
    monkeypatch.setattr(setup, "DISPATCHER_ENV", dispatcher)
    monkeypatch.setattr(setup, "CONFIG", tmp_path / "missing-config.json")
    for collision in (api, worker, dispatcher):
        collision.write_text("existing", encoding="utf-8")
        with pytest.raises(setup.TrialSetupError, match="already exists"):
            setup.derive_role_files()
        assert collision.read_text(encoding="utf-8") == "existing"
        collision.unlink()
    with pytest.raises(setup.TrialSetupError, match="missing or inconsistent"):
        setup.derive_role_files()
    assert not any(path.exists() for path in (api, worker, dispatcher))


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


def test_inspect_backlog_reports_only_cutover_counts(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name, value in (
        ("POSTGRES_USER", "synthetic_user"),
        ("POSTGRES_PASSWORD", "synthetic_password"),
        ("POSTGRES_DATABASE", "synthetic_database"),
    ):
        monkeypatch.setenv(name, value)

    class Connection:
        def __enter__(self) -> Connection:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def execute(self, query: str) -> SimpleNamespace:
            assert "state = 'QUEUED'" in query
            assert "state = 'RUNNING'" in query
            assert "delivery_state IN ('PENDING', 'RETRYING')" in query
            return SimpleNamespace(fetchone=lambda: (0, 0, 0))

    monkeypatch.setattr(setup, "_connect", lambda *_, **__: Connection())
    setup.inspect_backlog()
    assert json.loads(capsys.readouterr().out) == {
        "queued": 0,
        "running": 0,
        "pending_callbacks": 0,
    }


def test_cutover_runbook_gates_old_stop_on_verified_current_image() -> None:
    runbook = (setup.ROOT / "docs/runbooks/durable-compose-local-trial.md").read_text(
        encoding="utf-8"
    )
    cutover = runbook.split("## Later owner cutover from the combined trial", 1)[1].split(
        "## Guarded rollback and restart", 1
    )[0]
    script = cutover.split("```powershell", 1)[1].split("```", 1)[0]
    retain_old = script.index("Invoke-Progress docker @('image', 'tag', $oldImageId, $rollbackTag)")
    build_new = script.index("Invoke-Progress docker @('build', '--pull=false'")
    verify_revision = script.index("$newImage.Config.Labels.'org.opencontainers.image.revision'")
    verify_command = script.index("$help = Invoke-Text docker")
    first_backlog = script.index("\n    Assert-EmptyBacklog\n")
    stop_old = script.index("@('stop', '-t', '180', 'durable-api')")
    confirm_exit = script.index("[int]$stopped.State.ExitCode -ne 0")
    start_dispatcher = script.index("'durable-dispatcher=1', 'durable-dispatcher'")
    assert retain_old < build_new < verify_revision < verify_command < first_backlog
    assert first_backlog < stop_old < confirm_exit < start_dispatcher


def test_cutover_discovers_existing_containers_before_role_file_transition() -> None:
    runbook = (setup.ROOT / "docs/runbooks/durable-compose-local-trial.md").read_text(
        encoding="utf-8"
    )
    cutover = runbook.split("## Later owner cutover from the combined trial", 1)[1].split(
        "## Guarded rollback and restart", 1
    )[0]
    script = cutover.split("```powershell", 1)[1].split("```", 1)[0]
    assert "'container', 'ls', '--all', '--quiet'" in script
    assert '"label=com.docker.compose.project=$project"' in script
    assert '"label=com.docker.compose.service=$Service"' in script
    execution = script.split("    $revision =", 1)[1]
    derive = execution.index("'derive-role-files'")
    before_transition = execution[:derive]
    assert "Get-ComposeIds" not in before_transition
    assert "Assert-EmptyBacklog" not in before_transition
    assert "$oldIds.Count -ne 1" in before_transition
    assert "@(Get-ProjectServiceIds 'durable-worker').Count -ne 0" in before_transition
    assert "@(Get-ProjectServiceIds 'durable-dispatcher').Count -ne 0" in before_transition
    assert "$postgresIds.Count -ne 1" in before_transition
    assert before_transition.index("Get-ProjectServiceIds 'durable-api'") < before_transition.index(
        "Get-DockerObject @('container', 'inspect', $oldIds[0])"
    )
    assert before_transition.index("Get-ProjectServiceIds 'postgres'") < before_transition.index(
        "Get-DockerObject @('container', 'inspect', $postgresIds[0])"
    )
    assert "$old.Config.Labels.'com.docker.compose.project'" in before_transition
    assert "$postgres.Config.Labels.'com.docker.compose.project'" in before_transition
    assert derive < execution.index("@('compose') + $trial + @('config', '--quiet')")
    assert derive < execution.index("\n    Assert-EmptyBacklog\n")


def test_cutover_and_rollback_readiness_requests_have_deadlines() -> None:
    runbook = (setup.ROOT / "docs/runbooks/durable-compose-local-trial.md").read_text(
        encoding="utf-8"
    )
    cutover = runbook.split("## Later owner cutover from the combined trial", 1)[1].split(
        "## Guarded rollback and restart", 1
    )[0]
    rollback = runbook.split("## Guarded rollback and restart", 1)[1].split(
        "## Request and state after staging callback approval", 1
    )[0]
    for section in (cutover, rollback):
        script = section.split("```powershell", 1)[1].split("```", 1)[0]
        requests = [line for line in script.splitlines() if "Invoke-WebRequest" in line]
        assert len(requests) == 1
        assert "-TimeoutSec 5" in requests[0]
        ready_assignment = script.index("$ready = $null")
        assert script.rfind("$deadline = (Get-Date).AddSeconds(90)", 0, ready_assignment) >= 0
        assert ready_assignment < script.index("Invoke-WebRequest")
        assert "} while ((Get-Date) -lt $deadline)" in script
