"""Metadata-only checks for Super-7 durable persistence tables."""

import persistence.models  # noqa: F401
from persistence.base import Base


def test_required_super7_tables_and_columns_exist() -> None:
    expected = {
        "super7_analysis_jobs": {"id", "analysis_status", "callback_status", "max_attempts"},
        "super7_idempotency_bindings": {"caller_scope", "key_digest", "job_id"},
        "super7_execution_attempts": {"job_id", "attempt_number", "lease_token"},
        "super7_analysis_results": {"job_id", "terminal_status", "payload"},
        "super7_dispatch_intents": {"job_id", "status", "available_at"},
        "super7_callback_outbox": {"id", "job_id", "delivery_status", "next_attempt_at"},
    }
    assert set(Base.metadata.tables) == set(expected)
    for table_name, columns in expected.items():
        assert columns <= set(Base.metadata.tables[table_name].columns)


def test_constraints_indexes_and_foreign_keys_match_the_durable_contract() -> None:
    jobs = Base.metadata.tables["super7_analysis_jobs"]
    bindings = Base.metadata.tables["super7_idempotency_bindings"]
    attempts = Base.metadata.tables["super7_execution_attempts"]
    results = Base.metadata.tables["super7_analysis_results"]
    dispatch = Base.metadata.tables["super7_dispatch_intents"]
    outbox = Base.metadata.tables["super7_callback_outbox"]

    assert {index.name for index in jobs.indexes} >= {
        "ix_super7_analysis_jobs_claim",
        "ix_super7_analysis_jobs_lease_recovery",
    }
    assert {index.name for index in attempts.indexes} >= {
        "ix_super7_execution_attempts_job_id",
        "ix_super7_execution_attempts_lease_expires_at",
    }
    assert {index.name for index in dispatch.indexes} == {"ix_super7_dispatch_intents_due"}
    assert {index.name for index in outbox.indexes} == {"ix_super7_callback_outbox_due"}
    assert any(
        constraint.name == "uq_super7_idempotency_bindings_scoped_key_digest"
        for constraint in bindings.constraints
    )
    assert any(
        constraint.name == "uq_super7_execution_attempts_job_attempt_number"
        for constraint in attempts.constraints
    )
    assert any(
        constraint.name == "uq_super7_analysis_results_result_job"
        for constraint in results.constraints
    )
    assert any(
        constraint.name == "uq_super7_callback_outbox_callback_job"
        for constraint in outbox.constraints
    )

    foreign_key_targets = {
        foreign_key.target_fullname
        for table in Base.metadata.tables.values()
        for foreign_key in table.foreign_keys
    }
    assert foreign_key_targets == {"super7_analysis_jobs.id"}
