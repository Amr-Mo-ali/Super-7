"""Database-free review guards for the inactive Slice 4 migration literals."""

from __future__ import annotations

import ast
from importlib import resources
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_PATH = _ROOT / "src/adapters/migrations/postgres/sprint2_slice4_result_outbox.py"


def _literals() -> tuple[str, str]:
    tree = ast.parse(_PATH.read_text(encoding="utf-8"))
    assignments = [node for node in tree.body if isinstance(node, ast.Assign)]
    assert len(assignments) == 2
    dependency, steps = assignments
    assert ast.literal_eval(dependency.value) == {"sprint2_slice3_worker_attempt"}
    assert isinstance(steps.value, ast.List) and len(steps.value.elts) == 1
    call = steps.value.elts[0]
    assert isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
    assert call.func.id == "step" and not call.keywords and len(call.args) == 2
    apply, rollback = (ast.literal_eval(arg) for arg in call.args)
    assert type(apply) is str and type(rollback) is str
    return apply, rollback


def test_packaged_bytes_are_current_and_prior_migrations_are_not_duplicated() -> None:
    resource = resources.files("adapters").joinpath("migrations/postgres", _PATH.name)
    assert resource.read_bytes() == _PATH.read_bytes()


def test_apply_locks_before_preflight_and_adds_only_success_result_and_event_storage() -> None:
    apply, _ = _literals()
    assert apply.index("SET LOCAL lock_timeout = '5s'") < apply.index(
        "LOCK TABLE analysis_jobs IN ACCESS EXCLUSIVE MODE"
    )
    assert (
        apply.index("LOCK TABLE analysis_jobs")
        < apply.index("LOCK TABLE analysis_job_attempts")
        < apply.index("IF EXISTS")
    )
    assert "state IN ('COMPLETED', 'FAILED', 'CANCELLED')" in apply
    assert "j.attempt_count <> (" in apply
    assert "status = 'SUCCEEDED'" in apply
    assert "CREATE TABLE analysis_job_terminals" in apply
    assert "CREATE TABLE analysis_callback_outbox" in apply
    assert "DEFAULT gen_random_uuid()" in apply
    assert "job_id UUID NOT NULL UNIQUE" in apply
    assert "REFERENCES analysis_job_terminals(job_id) ON DELETE RESTRICT" in apply
    assert "FOREIGN KEY (job_id, attempt_number)" in apply
    assert "JSONB" in apply and "analysis_callback_snapshot_v1" in apply
    assert "INSERT INTO" not in apply and "UPDATE analysis_jobs" not in apply


def test_downgrade_locks_all_four_tables_and_refuses_data_before_any_drop() -> None:
    _, rollback = _literals()
    locks = (
        "LOCK TABLE analysis_jobs",
        "LOCK TABLE analysis_job_attempts",
        "LOCK TABLE analysis_job_terminals",
        "LOCK TABLE analysis_callback_outbox",
    )
    positions = tuple(rollback.index(value) for value in locks)
    assert positions == tuple(sorted(positions))
    assert rollback.index("WHERE status = 'SUCCEEDED'") < rollback.index(
        "DROP TABLE analysis_callback_outbox"
    )
    assert rollback.index("RAISE EXCEPTION") < rollback.index("DROP TABLE analysis_job_terminals")
    assert rollback.index("DROP TABLE analysis_callback_outbox") < rollback.index(
        "DROP TABLE analysis_job_terminals"
    )
    assert (
        "status = 'SUCCEEDED'"
        not in rollback[rollback.index("ALTER TABLE analysis_job_attempts ADD CONSTRAINT") :]
    )
