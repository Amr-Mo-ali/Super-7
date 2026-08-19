"""Structural and opt-in live checks for the initial Alembic migration."""

import importlib.util
import os
from pathlib import Path
from types import ModuleType

import pytest
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = ROOT / "alembic" / "versions" / "20260819_01_durable_job_foundation.py"


def _migration_module() -> ModuleType:
    specification = importlib.util.spec_from_file_location("phase3_migration", MIGRATION_PATH)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_specification(specification)
    specification.loader.exec_module(module)
    return module


def test_initial_migration_is_explicit_and_targets_only_super7_objects() -> None:
    migration = _migration_module()
    source = MIGRATION_PATH.read_text(encoding="utf-8")
    assert migration.revision == "20260819_01"
    assert callable(migration.upgrade)
    assert callable(migration.downgrade)
    assert "super7_analysis_jobs" in source
    assert "super7_callback_outbox" in source
    assert "apex_" not in source.lower()
    assert "drop_table" in source


def test_live_migration_smoke_requires_a_disposable_explicit_test_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    test_url = os.environ.get("TEST_DATABASE_URL")
    if not test_url:
        pytest.skip(
            "TEST_DATABASE_URL is not configured; live PostgreSQL migration smoke test skipped."
        )
    runtime_url = os.environ.get("DATABASE_URL")
    if runtime_url and test_url == runtime_url:
        pytest.fail("TEST_DATABASE_URL must not equal DATABASE_URL.")
    database_name = make_url(test_url).database or ""
    if not database_name.startswith("super7_test"):
        pytest.fail("TEST_DATABASE_URL must name an explicitly disposable super7_test database.")

    from alembic.config import Config

    from alembic import command

    monkeypatch.setenv("PERSISTENCE_ENABLED", "true")
    monkeypatch.setenv("DATABASE_URL", test_url)
    config = Config(str(ROOT / "alembic.ini"))
    command.upgrade(config, "head")
    command.downgrade(config, "base")
