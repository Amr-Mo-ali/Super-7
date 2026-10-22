"""RED-G: bounded literal inspection of the inactive Slice 3 migration.

This is not a SQL engine, database fake, or PostgreSQL semantics proof. Only the
small approved DDL/predicate surface is inspected. Migration source is never
imported or executed; Yoyo discovery is used without loading ``depends``/steps.
Object names and harmless predicate/statement ordering are not frozen.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import re
import tomllib
from dataclasses import dataclass, field
from importlib import import_module, resources
from pathlib import Path

import pytest
from yoyo import read_migrations  # type: ignore[import-untyped]

_ROOT = Path(__file__).resolve().parents[1]
_DIRECTORY = _ROOT / "src/adapters/migrations/postgres"
_REVISION = "sprint2_slice3_worker_attempt"
_DEPENDENCY = "sprint2_slice2_analysis_job"
_CATALOG = (
    "sprint2_slice1_foundation",
    _DEPENDENCY,
    _REVISION,
    "sprint2_slice4_result_outbox",
    "sprint2_slice5_video_integrity",
)
_PATH = _DIRECTORY / f"{_REVISION}.py"
_RED = "Slice 3 RED-G: canonical worker-attempt migration is absent; GREEN-G must add it."
_SHAPE = "RED-G literal migration surface is not supported."
_SQL = tuple[str, ...]
_LEXEME = re.compile(
    r"\s+|'(?:''|[^'])*'|\$[A-Za-z_][A-Za-z0-9_]*\$|[A-Za-z_][A-Za-z0-9_]*|[0-9]+|>=|<=|[(),;=~><]"
)
_JOB_COLUMNS = {
    "ANALYSIS_VERSION": "TEXT NOT NULL",
    "ATTEMPT_LIMIT": "INTEGER NOT NULL",
    "ATTEMPT_COUNT": "INTEGER NOT NULL DEFAULT 0",
}
_ATTEMPT_COLUMNS = {
    "JOB_ID": "UUID NOT NULL",
    "ATTEMPT_NUMBER": "INTEGER NOT NULL",
    "WORKER_SLOT_ID": "UUID NOT NULL",
    "LEASE_ID": "UUID NOT NULL",
    "STATUS": "TEXT NOT NULL",
    "STARTED_AT": "TIMESTAMPTZ NOT NULL",
    "HEARTBEAT_AT": "TIMESTAMPTZ NOT NULL",
    "LEASE_EXPIRES_AT": "TIMESTAMPTZ NULL",
    "FINISHED_AT": "TIMESTAMPTZ NULL",
    "SAFE_FAILURE_CODE": "TEXT NULL",
}
_JOB_CHECKS = (
    "analysis_version ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$'",
    "attempt_limit > 0",
    "attempt_count >= 0",
    "attempt_count <= attempt_limit",
)
_STATUSES = ("ACTIVE", "INTERRUPTED", "RETRYABLE_FAILURE", "TERMINAL_FAILURE", "EXHAUSTED")
_INDEXES = (
    (True, ("JOB_ID",)),
    (True, ("WORKER_SLOT_ID",)),
    (False, ("LEASE_EXPIRES_AT", "JOB_ID", "ATTEMPT_NUMBER")),
)
_ROLLBACK_LOCK = "LOCK TABLE analysis_jobs, analysis_job_attempts IN ACCESS EXCLUSIVE MODE;"

# Synthetic literals characterize the inspector, not an alternate migration.
# They are never submitted to a database or evaluated as Python migration code.
_APPLY = """DO $guard$
BEGIN
    IF EXISTS (SELECT 1 FROM analysis_jobs) THEN
        RAISE EXCEPTION 'Cannot apply Slice 3 while analysis_jobs contains rows.';
    END IF;
END
$guard$;
ALTER TABLE analysis_jobs
    ADD COLUMN analysis_version TEXT NOT NULL
        CHECK (analysis_version ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$'),
    ADD COLUMN attempt_limit INTEGER NOT NULL CHECK (attempt_limit > 0),
    ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    ADD CHECK (attempt_count <= attempt_limit);
CREATE TABLE analysis_job_attempts (
    job_id UUID NOT NULL,
    attempt_number INTEGER NOT NULL CHECK (attempt_number > 0),
    worker_slot_id UUID NOT NULL,
    lease_id UUID NOT NULL,
    status TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL,
    heartbeat_at TIMESTAMPTZ NOT NULL,
    lease_expires_at TIMESTAMPTZ NULL,
    finished_at TIMESTAMPTZ NULL,
    safe_failure_code TEXT NULL,
    PRIMARY KEY (job_id, attempt_number),
    FOREIGN KEY (job_id) REFERENCES analysis_jobs(job_id) ON DELETE RESTRICT,
    CHECK (heartbeat_at >= started_at),
    CHECK (finished_at >= heartbeat_at),
    CHECK (safe_failure_code IS NULL OR safe_failure_code ~ '^[a-z][a-z0-9_]{0,63}$'),
    CHECK (
        (status = 'ACTIVE' AND finished_at IS NULL AND safe_failure_code IS NULL
            AND lease_expires_at IS NOT NULL AND lease_expires_at > heartbeat_at)
        OR (status = 'INTERRUPTED' AND finished_at IS NOT NULL
            AND lease_expires_at IS NULL AND safe_failure_code IS NULL)
        OR (status IN ('RETRYABLE_FAILURE', 'TERMINAL_FAILURE')
            AND finished_at IS NOT NULL AND lease_expires_at IS NULL
            AND safe_failure_code IS NOT NULL)
        OR (status = 'EXHAUSTED' AND finished_at IS NOT NULL AND lease_expires_at IS NULL)
    )
);
CREATE UNIQUE INDEX active_job ON analysis_job_attempts (job_id) WHERE status = 'ACTIVE';
CREATE UNIQUE INDEX active_slot ON analysis_job_attempts (worker_slot_id) WHERE status = 'ACTIVE';
CREATE INDEX active_expiry ON analysis_job_attempts
    (lease_expires_at, job_id, attempt_number) WHERE status = 'ACTIVE';
"""
_ROLLBACK = """DO $guard$
BEGIN
    LOCK TABLE analysis_jobs, analysis_job_attempts IN ACCESS EXCLUSIVE MODE;
    IF EXISTS (SELECT 1 FROM analysis_jobs)
        OR EXISTS (SELECT 1 FROM analysis_job_attempts) THEN
        RAISE EXCEPTION 'Cannot downgrade Slice 3 while durable rows exist.';
    END IF;
END
$guard$;
DROP TABLE analysis_job_attempts;
ALTER TABLE analysis_jobs DROP COLUMN analysis_version,
    DROP COLUMN attempt_limit, DROP COLUMN attempt_count;
"""


def _tokens(literal: str) -> _SQL:
    """Tokenize only inspected literals; reject comments and unsupported syntax."""
    result: list[str] = []
    position = 0
    while position < len(literal):
        match = _LEXEME.match(literal, position)
        assert match is not None, _SHAPE
        token = match.group()
        if not token.isspace():
            result.append(token if token.startswith(("'", "$")) else token.upper())
        position = match.end()
    return tuple(result)


def _split(tokens: _SQL, delimiter: str) -> tuple[_SQL, ...]:
    """Split a bounded literal list, respecting parentheses and quoted tokens."""
    assert tokens, _SHAPE
    result: list[_SQL] = []
    depth = 0
    start = 0
    for index, token in enumerate(tokens):
        depth += (token == "(") - (token == ")")
        assert depth >= 0, _SHAPE
        if token == delimiter and depth == 0:
            assert index > start, _SHAPE
            result.append(tokens[start:index])
            start = index + 1
    assert depth == 0, _SHAPE
    assert start < len(tokens), _SHAPE
    result.append(tokens[start:])
    return tuple(result)


def _statements(tokens: _SQL) -> tuple[_SQL, ...]:
    """Only a statement list permits one optional final semicolon."""
    return _split(tokens[:-1] if tokens[-1:] == (";",) else tokens, ";")


def _unparen(tokens: _SQL) -> _SQL:
    while tokens[:1] == ("(",) and tokens[-1:] == (")",):
        depth = 0
        for _index, token in enumerate(tokens):
            depth += (token == "(") - (token == ")")
            if depth == 0:
                break
        if _index != len(tokens) - 1:
            break
        tokens = tokens[1:-1]
    return tokens


def _checks(definition: _SQL) -> tuple[_SQL, tuple[_SQL, ...]]:
    """Retain the header intact; inspect only trailing CHECK constraints."""
    assert definition, _SHAPE
    index = next(
        (index for index, token in enumerate(definition) if token in {"CHECK", "CONSTRAINT"}),
        len(definition),
    )
    core = definition[:index]
    if index == len(definition):
        return core, ()
    if core:
        column = core[1:] if core[:1] == ("COLUMN",) else core
        columns = _JOB_COLUMNS if core[:1] == ("COLUMN",) else _ATTEMPT_COLUMNS
        assert column and column[0] in columns, _SHAPE
    predicates: list[_SQL] = []
    while index < len(definition):
        if definition[index] == "CONSTRAINT":
            assert index + 2 < len(definition), _SHAPE
            assert re.fullmatch(r"[A-Z_][A-Z0-9_]*", definition[index + 1]), _SHAPE
            index += 2
            key = definition[index : index + 2] in (("PRIMARY", "KEY"), ("FOREIGN", "KEY"))
            if not core and not predicates and key:
                assert not {"CHECK", "CONSTRAINT"}.intersection(definition[index:]), _SHAPE
                return definition[index:], ()
        assert definition[index] == "CHECK", _SHAPE
        assert definition[index + 1 : index + 2] == ("(",), _SHAPE
        start = index + 2
        depth = 1
        index = start
        while index < len(definition) and depth:
            depth += (definition[index] == "(") - (definition[index] == ")")
            index += 1
        assert depth == 0, _SHAPE
        predicate = _unparen(definition[start : index - 1])
        assert predicate, _SHAPE
        predicates.append(predicate)
    assert core or len(predicates) == 1, _SHAPE
    return core, tuple(predicates)


def _literals(source: str) -> tuple[frozenset[str], tuple[tuple[str, str], ...]]:
    """Public Yoyo literal steps only; no exec/import of inspected source."""
    body = ast.parse(source).body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        assert isinstance(body[0].value.value, str), _SHAPE
        body = body[1:]
    assert len(body) == 3, _SHAPE
    imported, dependency, steps = body
    assert isinstance(imported, ast.ImportFrom) and imported.module == "yoyo", _SHAPE
    assert imported.level == 0 and len(imported.names) == 1, _SHAPE
    assert imported.names[0].name == "step" and imported.names[0].asname is None, _SHAPE
    assert isinstance(dependency, ast.Assign) and len(dependency.targets) == 1, _SHAPE
    assert isinstance(dependency.targets[0], ast.Name), _SHAPE
    assert dependency.targets[0].id == "__depends__", _SHAPE
    assert isinstance(dependency.value, ast.Set), _SHAPE
    assert all(
        isinstance(item, ast.Constant) and type(item.value) is str for item in dependency.value.elts
    ), _SHAPE
    dependencies = ast.literal_eval(dependency.value)
    assert dependencies == {_DEPENDENCY}, _SHAPE
    assert isinstance(steps, ast.Assign) and len(steps.targets) == 1, _SHAPE
    assert isinstance(steps.targets[0], ast.Name) and steps.targets[0].id == "steps", _SHAPE
    assert isinstance(steps.value, ast.List) and steps.value.elts, _SHAPE
    pairs: list[tuple[str, str]] = []
    for item in steps.value.elts:
        assert isinstance(item, ast.Call) and isinstance(item.func, ast.Name), _SHAPE
        assert item.func.id == "step" and len(item.args) == 2 and not item.keywords, _SHAPE
        assert all(isinstance(arg, ast.Constant) and type(arg.value) is str for arg in item.args), (
            _SHAPE
        )
        apply, rollback = (ast.literal_eval(arg) for arg in item.args)
        assert apply.strip() and rollback.strip(), _SHAPE
        pairs.append((apply, rollback))
    return frozenset(dependencies), tuple(pairs)


def _source() -> str:
    if not _PATH.is_file():
        pytest.fail(_RED, pytrace=False)
    return _PATH.read_text(encoding="utf-8", errors="strict")


def _migration_sql() -> tuple[str, str]:
    _, pairs = _literals(_source())
    # Yoyo rolls reversible steps back in reverse order.
    return "\n".join(pair[0] for pair in pairs), "\n".join(pair[1] for pair in reversed(pairs))


def _refusal_tail(
    literal: str, required: frozenset[str], *, lock_durable_tables: bool = False
) -> _SQL:
    tokens = _tokens(literal)
    assert tokens[:1] == ("DO",) and re.fullmatch(r"\$[A-Za-z_][A-Za-z0-9_]*\$", tokens[1]), _SHAPE
    tag = tokens[1]
    closing = tokens.index(tag, 2)
    assert tokens[closing + 1 : closing + 2] == (";",), _SHAPE
    body = tokens[2:closing]
    assert body[:1] == ("BEGIN",), _SHAPE
    body = body[1:]
    if lock_durable_tables:
        lock = _tokens(_ROLLBACK_LOCK)
        assert body[: len(lock)] == lock, _SHAPE
        body = body[len(lock) :]
    found: set[str] = set()
    while body[:1] == ("IF",):
        then = body.index("THEN")
        for condition in _split(_unparen(body[1:then]), "OR"):
            matches = [
                table
                for table in required
                if _unparen(condition) == _tokens(f"EXISTS (SELECT 1 FROM {table})")
            ]
            assert len(matches) == 1, _SHAPE
            found.add(matches[0])
        body = body[then + 1 :]
        assert body[:2] == ("RAISE", "EXCEPTION"), _SHAPE
        assert len(body) >= 7 and body[2].startswith("'") and len(body[2]) > 2, _SHAPE
        assert body[3:7] == (";", "END", "IF", ";"), _SHAPE
        body = body[7:]
    assert body in (("END",), ("END", ";")), _SHAPE
    assert found == set(required), _SHAPE
    return tokens[closing + 2 :]


@dataclass
class _DDL:
    """Inspected literal definitions only, with no row/state evaluation."""

    job_columns: dict[str, _SQL] = field(default_factory=dict)
    job_checks: list[_SQL] = field(default_factory=list)
    attempt_columns: dict[str, _SQL] = field(default_factory=dict)
    attempt_checks: list[_SQL] = field(default_factory=list)
    keys: list[_SQL] = field(default_factory=list)
    indexes: dict[str, tuple[bool, _SQL]] = field(default_factory=dict)


def _inspect_apply(literal: str) -> _DDL:
    ddl = _DDL()
    statements = _statements(_refusal_tail(literal, frozenset({"ANALYSIS_JOBS"})))
    tables = 0
    for statement in statements:
        if statement[:3] == ("ALTER", "TABLE", "ANALYSIS_JOBS"):
            for addition in _split(statement[3:], ","):
                assert addition[:1] == ("ADD",), _SHAPE
                core, checks = _checks(addition[1:])
                ddl.job_checks.extend(checks)
                if core[:1] == ("COLUMN",):
                    name = core[1]
                    assert name not in ddl.job_columns, _SHAPE
                    ddl.job_columns[name] = core[2:]
                else:
                    assert not core and checks, _SHAPE
        elif statement[:3] == ("CREATE", "TABLE", "ANALYSIS_JOB_ATTEMPTS"):
            tables += 1
            assert statement[3:4] == ("(",) and statement[-1:] == (")",), _SHAPE
            for definition in _split(statement[4:-1], ","):
                core, checks = _checks(definition)
                ddl.attempt_checks.extend(checks)
                if core and core[0] in _ATTEMPT_COLUMNS:
                    assert core[0] not in ddl.attempt_columns, _SHAPE
                    ddl.attempt_columns[core[0]] = core[1:]
                elif core:
                    ddl.keys.append(core)
        else:
            unique = statement[:2] == ("CREATE", "UNIQUE")
            prefix = ("CREATE", "UNIQUE", "INDEX") if unique else ("CREATE", "INDEX")
            assert statement[: len(prefix)] == prefix, _SHAPE
            rest = statement[len(prefix) :]
            name = rest[0]
            assert re.fullmatch(r"[A-Z_][A-Z0-9_]*", name) and name not in ddl.indexes, _SHAPE
            assert rest[1:4] == ("ON", "ANALYSIS_JOB_ATTEMPTS", "("), _SHAPE
            close = rest.index(")", 4)
            columns = _split(rest[4:close], ",")
            assert all(len(column) == 1 for column in columns), _SHAPE
            assert rest[close + 1 : close + 2] == ("WHERE",), _SHAPE
            assert _unparen(rest[close + 2 :]) == _tokens("status = 'ACTIVE'"), _SHAPE
            ddl.indexes[name] = (unique, tuple(column[0] for column in columns))
    assert tables == 1, _SHAPE
    return ddl


def _atoms(checks: list[_SQL]) -> frozenset[_SQL]:
    atoms = [_unparen(atom) for check in checks for atom in _split(_unparen(check), "AND")]
    assert len(atoms) == len(set(atoms)), _SHAPE
    return frozenset(atoms)


def _selected_statuses(selector: _SQL) -> frozenset[str]:
    values: tuple[str, ...]
    if selector[:2] == ("STATUS", "=") and len(selector) == 3:
        values = (selector[2],)
    else:
        assert selector[:3] == ("STATUS", "IN", "(") and selector[-1:] == (")",), _SHAPE
        parts = _split(selector[3:-1], ",")
        assert all(len(part) == 1 for part in parts), _SHAPE
        values = tuple(part[0] for part in parts)
    assert len(values) == len(set(values)) and all(
        value in {repr(status) for status in _STATUSES} for value in values
    ), _SHAPE
    return frozenset(value[1:-1] for value in values)


def _assert_attempt_checks(ddl: _DDL) -> None:
    """Match only approved flat conjunction/disjunction atoms, not arbitrary SQL."""
    atoms = set(_atoms(ddl.attempt_checks))
    positive = _tokens("attempt_number > 0")
    assert positive in atoms
    atoms.remove(positive)
    heartbeat = _tokens("heartbeat_at >= started_at")
    finished = _tokens("finished_at >= heartbeat_at")
    global_order = {order for order in (heartbeat, finished) if order in atoms}
    atoms.difference_update(global_order)
    code = _tokens("safe_failure_code ~ '^[a-z][a-z0-9_]{0,63}$'")
    nullable_code = _tokens(
        "safe_failure_code IS NULL OR safe_failure_code ~ '^[a-z][a-z0-9_]{0,63}$'"
    )
    assert (code in atoms) != (nullable_code in atoms)
    atoms.difference_update({code, nullable_code})
    vocabulary = [
        atom for atom in atoms if atom[:2] == ("STATUS", "IN") and len(_split(atom, "OR")) == 1
    ]
    for atom in vocabulary:
        assert _selected_statuses(atom) == frozenset(_STATUSES)
        atoms.remove(atom)
    assert len(atoms) == 1, _SHAPE
    shape = next(iter(atoms))
    actual: dict[str, set[frozenset[_SQL]]] = {status: set() for status in _STATUSES}
    for branch in _split(_unparen(shape), "OR"):
        parts = [_unparen(part) for part in _split(_unparen(branch), "AND")]
        selectors = [part for part in parts if part[:1] == ("STATUS",)]
        assert len(selectors) == 1, _SHAPE
        selector = selectors[0]
        parts.remove(selector)
        assert len(parts) == len(set(parts)), _SHAPE
        selected = _selected_statuses(selector)
        for status in selected:
            required_order = {heartbeat} if status == "ACTIVE" else {heartbeat, finished}
            assert required_order.issubset(set(parts) | global_order)
            parts_without_order = set(parts) - required_order
            parts_without_order.difference_update(
                {_tokens("started_at IS NOT NULL"), _tokens("heartbeat_at IS NOT NULL")}
            )
            actual[status].add(frozenset(parts_without_order))
    active = frozenset(
        _tokens(value)
        for value in (
            "finished_at IS NULL",
            "safe_failure_code IS NULL",
            "lease_expires_at IS NOT NULL",
            "lease_expires_at > heartbeat_at",
        )
    )
    resolved = frozenset(
        _tokens(value) for value in ("finished_at IS NOT NULL", "lease_expires_at IS NULL")
    )
    null_code = _tokens("safe_failure_code IS NULL")
    nonnull_code = _tokens("safe_failure_code IS NOT NULL")
    assert actual["ACTIVE"] == {active}
    assert actual["INTERRUPTED"] == {resolved | {null_code}}
    for status in ("RETRYABLE_FAILURE", "TERMINAL_FAILURE"):
        assert actual[status] == {resolved | {nonnull_code}}
    assert actual["EXHAUSTED"] in ({resolved}, {resolved | {null_code}, resolved | {nonnull_code}})


def _assert_apply(literal: str) -> _DDL:
    ddl = _inspect_apply(literal)
    assert ddl.job_columns == {name: _tokens(value) for name, value in _JOB_COLUMNS.items()}
    assert _atoms(ddl.job_checks) == frozenset(_tokens(check) for check in _JOB_CHECKS)
    # NULL may be omitted on nullable PostgreSQL columns, but NOT NULL may not.
    assert set(ddl.attempt_columns) == set(_ATTEMPT_COLUMNS)
    for name, value in _ATTEMPT_COLUMNS.items():
        expected = _tokens(value)
        assert ddl.attempt_columns[name] in (
            (expected, expected[:-1])
            if expected[-1] == "NULL" and "NOT" not in expected
            else (expected,)
        )
    assert set(ddl.keys) == {
        _tokens("PRIMARY KEY (job_id, attempt_number)"),
        _tokens("FOREIGN KEY (job_id) REFERENCES analysis_jobs(job_id) ON DELETE RESTRICT"),
    }
    assert len(ddl.keys) == 2
    _assert_attempt_checks(ddl)
    assert len(ddl.indexes) == 3 and set(ddl.indexes.values()) == set(_INDEXES)
    return ddl


def _assert_rollback(literal: str, owned_indexes: frozenset[str]) -> None:
    statements = _statements(
        _refusal_tail(
            literal,
            frozenset({"ANALYSIS_JOBS", "ANALYSIS_JOB_ATTEMPTS"}),
            lock_durable_tables=True,
        )
    )
    dropped: list[str] = []
    tables = 0
    removed_indexes: set[str] = set()
    for statement in statements:
        if statement == _tokens("DROP TABLE analysis_job_attempts"):
            tables += 1
        elif statement[:3] == ("ALTER", "TABLE", "ANALYSIS_JOBS"):
            for operation in _split(statement[3:], ","):
                assert operation[:2] == ("DROP", "COLUMN") and len(operation) == 3, _SHAPE
                dropped.append(operation[2])
        else:
            assert statement[:2] == ("DROP", "INDEX") and len(statement) == 3, _SHAPE
            assert statement[2] in owned_indexes and statement[2] not in removed_indexes, _SHAPE
            removed_indexes.add(statement[2])
    assert tables == 1 and len(dropped) == 3 and set(dropped) == set(_JOB_COLUMNS)


def _fixture_source(apply: str = _APPLY, rollback: str = _ROLLBACK) -> str:
    return f"from yoyo import step\n__depends__ = {{{_DEPENDENCY!r}}}\nsteps = [step({apply!r}, {rollback!r})]\n"


def test_literal_guard_accepts_public_yoyo_steps_without_executing_source() -> None:
    dependencies, pairs = _literals(_fixture_source())
    assert dependencies == frozenset({_DEPENDENCY})
    assert pairs == ((_APPLY, _ROLLBACK),)
    ddl = _assert_apply(pairs[0][0])
    _assert_rollback(pairs[0][1], frozenset(ddl.indexes))
    assert _assert_apply(_APPLY.rstrip().removesuffix(";")) == ddl
    _assert_rollback(_ROLLBACK.rstrip().removesuffix(";"), frozenset(ddl.indexes))


@pytest.mark.parametrize(
    "suffix",
    (
        "\nraise RuntimeError('must never execute')",
        "\nsteps = []",
        "\n__transactional__ = False",
        "\nimport pathlib",
    ),
    ids=("executable-statement", "rebind-steps", "nontransactional", "unrelated-import"),
)
def test_literal_guard_rejects_extra_executable_surface(suffix: str) -> None:
    with pytest.raises(AssertionError, match=_SHAPE):
        _literals(_fixture_source() + suffix)


@pytest.mark.parametrize(
    ("old", "new"),
    (
        ("from yoyo import step", "from yoyo import step as execute"),
        (f"{{{_DEPENDENCY!r}}}", "{'sprint2_slice1_foundation'}"),
        ("steps = [step(", "steps = [callback("),
        (repr(_APPLY), "build_sql()"),
        (repr(_ROLLBACK), "None"),
    ),
    ids=("aliased-step", "wrong-dependency", "python-callback", "dynamic-sql", "irreversible"),
)
def test_literal_guard_rejects_nonliteral_or_wrong_public_structure(old: str, new: str) -> None:
    with pytest.raises(AssertionError, match=_SHAPE):
        _literals(_fixture_source().replace(old, new, 1))


def test_literal_guard_retains_multiple_steps_and_reverse_rollback_order() -> None:
    source = f"from yoyo import step\n__depends__ = {{{_DEPENDENCY!r}}}\nsteps = [step('apply one', 'undo one'), step('apply two', 'undo two')]\n"
    _, pairs = _literals(source)
    assert pairs == (("apply one", "undo one"), ("apply two", "undo two"))
    assert tuple(pair[1] for pair in reversed(pairs)) == ("undo two", "undo one")


@pytest.mark.parametrize(
    ("old", "new"),
    (
        ("SELECT 1 FROM analysis_jobs", "SELECT 1 FROM analysis_jobs WHERE state = 'RUNNING'"),
        ("RAISE EXCEPTION", "RAISE NOTICE"),
        (
            "ADD COLUMN analysis_version TEXT NOT NULL",
            "ADD COLUMN analysis_version TEXT NOT NULL DEFAULT '1.0.0'",
        ),
        ("attempt_limit > 0", "attempt_limit >= 0"),
        ("attempt_count >= 0", "attempt_count > 0"),
        ("attempt_count <= attempt_limit", "attempt_count < attempt_limit"),
        ("^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$", "[A-Za-z0-9][A-Za-z0-9._-]{0,63}"),
        ("worker_slot_id UUID NOT NULL", "worker_slot_id TEXT NOT NULL"),
        ("lease_id UUID NOT NULL", "lease_id UUID NULL"),
        ("started_at TIMESTAMPTZ NOT NULL", "started_at TIMESTAMPTZ NULL"),
        ("heartbeat_at >= started_at", "heartbeat_at > started_at"),
        ("finished_at >= heartbeat_at", "finished_at >= started_at"),
        ("lease_expires_at > heartbeat_at", "lease_expires_at >= heartbeat_at"),
        (
            "status = 'ACTIVE' AND finished_at IS NULL",
            "status = 'ACTIVE' AND finished_at IS NOT NULL",
        ),
        (
            "status = 'INTERRUPTED' AND finished_at IS NOT NULL",
            "status = 'INTERRUPTED' AND finished_at IS NULL",
        ),
        (
            "status IN ('RETRYABLE_FAILURE', 'TERMINAL_FAILURE')",
            "status IN ('RETRYABLE_FAILURE', 'TERMINAL_FAILURE', 'COMPLETED')",
        ),
        ("safe_failure_code IS NOT NULL)", "safe_failure_code IS NULL)"),
        (
            "status = 'EXHAUSTED' AND finished_at IS NOT NULL",
            "status = 'EXHAUSTED' AND safe_failure_code IS NOT NULL AND finished_at IS NOT NULL",
        ),
        ("^[a-z][a-z0-9_]{0,63}$", "^[a-z][a-z0-9_]*$"),
        ("ON DELETE RESTRICT", "ON DELETE CASCADE"),
        ("CREATE UNIQUE INDEX active_slot", "CREATE INDEX active_slot"),
        (
            "(lease_expires_at, job_id, attempt_number) WHERE",
            "(job_id, lease_expires_at, attempt_number) WHERE",
        ),
        ("WHERE status = 'ACTIVE';", "WHERE status = 'ACTIVE' OR status = 'INTERRUPTED';"),
    ),
    ids=(
        "state-filtered-preflight",
        "notice-not-refusal",
        "version-default",
        "zero-limit",
        "zero-count-rejected",
        "limit-equality-rejected",
        "unanchored-version",
        "slot-type",
        "nullable-lease-id",
        "nullable-start",
        "strict-heartbeat",
        "wrong-finish-order",
        "nonstrict-expiry",
        "active-finish",
        "interrupted-finish",
        "extra-status",
        "classified-null-code",
        "expiry-exhaustion-rejected",
        "unbounded-code",
        "cascading-fk",
        "nonunique-slot",
        "wrong-expiry-key-order",
        "overbroad-index",
    ),
)
def test_ddl_guard_rejects_reviewable_schema_false_negatives(old: str, new: str) -> None:
    assert old in _APPLY
    with pytest.raises(AssertionError):
        _assert_apply(_APPLY.replace(old, new, 1))


@pytest.mark.parametrize(
    ("old", "new"),
    (
        ("CHECK (attempt_limit > 0)", "CHECK (attempt_limit > 0 AND)"),
        ("AND lease_expires_at IS NULL)\n    )", "AND lease_expires_at IS NULL) OR\n    )"),
        (
            "ADD CHECK (attempt_count <= attempt_limit);",
            "ADD CHECK (attempt_count <= attempt_limit), ADD CHECK ();",
        ),
        (
            "(lease_expires_at, job_id, attempt_number) WHERE",
            "(lease_expires_at, job_id, attempt_number,) WHERE",
        ),
        (
            "attempt_number INTEGER NOT NULL CHECK (attempt_number > 0)",
            "CHECK (attempt_number > 0) attempt_number INTEGER NOT NULL",
        ),
    ),
    ids=(
        "trailing-and",
        "trailing-or",
        "empty-check",
        "trailing-index-comma",
        "check-before-column",
    ),
)
def test_complete_apply_rejects_reviewed_empty_elements_and_check_placement(
    monkeypatch: pytest.MonkeyPatch, old: str, new: str
) -> None:
    assert old in _APPLY
    malformed = _APPLY.replace(old, new, 1)
    assert malformed != _APPLY
    monkeypatch.setattr(f"{__name__}._source", lambda: _fixture_source(malformed))
    with pytest.raises(AssertionError, match=_SHAPE):
        test_worker_migration_complete_apply_has_no_backfill_or_unapproved_schema()


@pytest.mark.parametrize(
    "extra",
    (
        "UPDATE analysis_jobs SET attempt_count = 0;",
        "DROP TABLE analysis_jobs;",
        "CREATE TABLE callback_outbox (job_id UUID);",
        "-- CHECK (attempt_count <= attempt_limit)",
        "/* preserved-looking schema */",
    ),
    ids=("backfill", "slice2-drop", "outbox", "comment-spoof", "block-comment"),
)
def test_ddl_guard_rejects_unapproved_writes_and_comment_spoofs(extra: str) -> None:
    with pytest.raises(AssertionError):
        _assert_apply(_APPLY + extra)


@pytest.mark.parametrize(
    ("old", "new"),
    (
        ("OR EXISTS (SELECT 1 FROM analysis_job_attempts)", ""),
        ("SELECT 1 FROM analysis_jobs", "SELECT 1 FROM analysis_jobs WHERE attempt_count > 0"),
        ("RAISE EXCEPTION", "RAISE NOTICE"),
        ("DROP COLUMN attempt_count", "DROP COLUMN callback_url"),
        ("DROP TABLE analysis_job_attempts", "DROP TABLE analysis_jobs"),
    ),
    ids=(
        "missing-attempt-refusal",
        "zero-count-data-loss",
        "notice-not-refusal",
        "slice2-column-loss",
        "slice2-table-loss",
    ),
)
def test_downgrade_guard_rejects_data_loss_and_partial_refusal(old: str, new: str) -> None:
    with pytest.raises(AssertionError):
        _assert_rollback(_ROLLBACK.replace(old, new, 1), frozenset())


@pytest.mark.parametrize(
    "rollback",
    (
        _ROLLBACK.replace(_ROLLBACK_LOCK, "", 1),
        _ROLLBACK.replace(_ROLLBACK_LOCK, "", 1).replace(
            "DROP TABLE", _ROLLBACK_LOCK + "\nDROP TABLE", 1
        ),
        _ROLLBACK.replace("analysis_jobs, analysis_job_attempts", "analysis_jobs", 1),
        _ROLLBACK.replace("analysis_jobs, analysis_job_attempts", "analysis_job_attempts", 1),
        _ROLLBACK.replace("ACCESS EXCLUSIVE", "ROW EXCLUSIVE", 1),
        _ROLLBACK.replace(
            "analysis_jobs, analysis_job_attempts", "analysis_job_attempts, analysis_jobs", 1
        ),
        _ROLLBACK.replace(
            "LOCK TABLE analysis_jobs, analysis_job_attempts",
            "LOCK TABLE ONLY analysis_jobs, ONLY analysis_job_attempts",
            1,
        ),
        _ROLLBACK.replace(_ROLLBACK_LOCK, _ROLLBACK_LOCK + "\n    COMMIT;", 1),
        _ROLLBACK.replace("DROP TABLE", "COMMIT;\nDROP TABLE", 1),
        _ROLLBACK.replace("DROP TABLE", "ROLLBACK;\nDROP TABLE", 1),
    ),
    ids=(
        "no-lock",
        "lock-after-guard",
        "jobs-only",
        "attempts-only",
        "writer-permitting-lock",
        "reversed-lock-order",
        "only-parent-tables",
        "commit-before-checks",
        "commit-before-ddl",
        "rollback-before-ddl",
    ),
)
def test_complete_downgrade_rejects_missing_or_released_pre_guard_locks(
    monkeypatch: pytest.MonkeyPatch, rollback: str
) -> None:
    assert rollback != _ROLLBACK
    monkeypatch.setattr(f"{__name__}._source", lambda: _fixture_source(rollback=rollback))
    with pytest.raises(AssertionError, match=_SHAPE):
        test_worker_migration_empty_downgrade_removes_only_slice3_owned_schema()


def test_ddl_guard_does_not_freeze_index_or_constraint_names() -> None:
    alternative = (
        _APPLY.replace("active_job", "owned_job_index")
        .replace("active_slot", "owned_slot_index")
        .replace("active_expiry", "owned_expiry_index")
    )
    alternative = alternative.replace(
        "CHECK (attempt_limit > 0)", "CONSTRAINT positive_policy CHECK (attempt_limit > 0)"
    )
    alternative = alternative.replace("TIMESTAMPTZ NULL", "TIMESTAMPTZ")
    ddl = _assert_apply(alternative)
    assert set(ddl.indexes) == {"OWNED_JOB_INDEX", "OWNED_SLOT_INDEX", "OWNED_EXPIRY_INDEX"}


def test_ddl_guard_accepts_named_keys_and_separate_alter_statements() -> None:
    alternative = _APPLY.replace("PRIMARY KEY", "CONSTRAINT attempt_pk PRIMARY KEY").replace(
        "FOREIGN KEY", "CONSTRAINT attempt_fk FOREIGN KEY"
    )
    alternative = alternative.replace("ADD CHECK", "ADD CONSTRAINT policy_count CHECK").replace(
        "CHECK (heartbeat_at >= started_at)",
        "CONSTRAINT attempt_heartbeat CHECK (heartbeat_at >= started_at)",
    )
    alternative = alternative.replace(
        ",\n    ADD COLUMN", ";\nALTER TABLE analysis_jobs ADD COLUMN"
    ).replace(",\n    ADD CHECK", ";\nALTER TABLE analysis_jobs ADD CHECK")
    _assert_apply(alternative)


def test_worker_migration_literal_public_structure_and_dependency() -> None:
    dependencies, pairs = _literals(_source())
    assert _PATH.name == f"{_REVISION}.py"
    assert dependencies == frozenset({_DEPENDENCY})
    assert pairs


@pytest.mark.parametrize("name", tuple(_JOB_COLUMNS), ids=str.lower)
def test_worker_migration_exact_job_policy_columns(name: str) -> None:
    apply, _ = _migration_sql()
    assert _inspect_apply(apply).job_columns[name] == _tokens(_JOB_COLUMNS[name])


@pytest.mark.parametrize(
    "predicate",
    _JOB_CHECKS,
    ids=("version-format", "positive-limit", "nonnegative-count", "count-within-limit"),
)
def test_worker_migration_job_policy_check_predicates(predicate: str) -> None:
    apply, _ = _migration_sql()
    assert _tokens(predicate) in _atoms(_inspect_apply(apply).job_checks)


@pytest.mark.parametrize("name", tuple(_ATTEMPT_COLUMNS), ids=str.lower)
def test_worker_migration_exact_attempt_columns(name: str) -> None:
    apply, _ = _migration_sql()
    actual = _inspect_apply(apply).attempt_columns[name]
    expected = _tokens(_ATTEMPT_COLUMNS[name])
    assert actual == expected or (
        expected[-1:] == ("NULL",) and "NOT" not in expected and actual == expected[:-1]
    )


def test_worker_migration_four_part_fence_primary_key_and_restrict_foreign_key() -> None:
    apply, _ = _migration_sql()
    ddl = _inspect_apply(apply)
    assert all(
        name in ddl.attempt_columns
        for name in ("JOB_ID", "ATTEMPT_NUMBER", "WORKER_SLOT_ID", "LEASE_ID")
    )
    assert set(ddl.keys) == {
        _tokens("PRIMARY KEY (job_id, attempt_number)"),
        _tokens("FOREIGN KEY (job_id) REFERENCES analysis_jobs(job_id) ON DELETE RESTRICT"),
    }


def test_worker_migration_complete_status_timestamp_and_failure_code_checks() -> None:
    apply, _ = _migration_sql()
    _assert_attempt_checks(_inspect_apply(apply))


@pytest.mark.parametrize(
    "definition", _INDEXES, ids=("one-active-per-job", "one-active-per-slot", "active-expiry")
)
def test_worker_migration_required_partial_indexes(definition: tuple[bool, _SQL]) -> None:
    apply, _ = _migration_sql()
    assert definition in _inspect_apply(apply).indexes.values()


def test_worker_migration_complete_apply_has_no_backfill_or_unapproved_schema() -> None:
    apply, _ = _migration_sql()
    _assert_apply(apply)


def test_worker_migration_apply_refuses_any_job_before_ddl() -> None:
    apply, _ = _migration_sql()
    assert _refusal_tail(apply, frozenset({"ANALYSIS_JOBS"}))


def test_worker_migration_downgrade_refuses_jobs_and_attempts_before_drops() -> None:
    _, rollback = _migration_sql()
    assert _refusal_tail(
        rollback,
        frozenset({"ANALYSIS_JOBS", "ANALYSIS_JOB_ATTEMPTS"}),
        lock_durable_tables=True,
    )


def test_worker_migration_empty_downgrade_removes_only_slice3_owned_schema() -> None:
    apply, rollback = _migration_sql()
    ddl = _assert_apply(apply)
    _assert_rollback(rollback, frozenset(ddl.indexes))


@pytest.mark.parametrize("source", ("source", "package"))
def test_worker_migration_public_yoyo_discovery_has_exact_ordered_catalog(source: str) -> None:
    _source()
    location = str(_DIRECTORY) if source == "source" else "package:adapters:migrations/postgres"
    migrations = read_migrations(location)
    assert tuple(str(migration.id) for migration in migrations) == _CATALOG
    # Inspection of id/loaded does not invoke Yoyo Migration.load().
    assert all(not migration.loaded for migration in migrations)


def test_worker_migration_source_and_package_resource_bytes_match() -> None:
    _source()
    resource = resources.files("adapters").joinpath(f"migrations/postgres/{_REVISION}.py")
    assert resource.is_file()
    assert resource.read_bytes() == _PATH.read_bytes()
    directory = resources.files("adapters").joinpath("migrations/postgres")
    assert tuple(
        sorted(
            item.name
            for item in directory.iterdir()
            if item.is_file() and item.name.endswith((".py", ".sql"))
        )
    ) == tuple(f"{revision}.py" for revision in _CATALOG)


@pytest.mark.parametrize(
    ("revision", "sha256"),
    (
        (_CATALOG[0], "9ccc2e1347b4b9d582923a6385866a9b0cfb7b3fc581e32e780a3a1d5195eea4"),
        (_DEPENDENCY, "1c2686d4e60e077b44d192dc9075a3bbeb2e9205d2c702f526041380696bd76a"),
    ),
    ids=("slice1", "slice2"),
)
def test_prior_migration_source_and_package_bytes_are_preserved(revision: str, sha256: str) -> None:
    filename = f"{revision}.py"
    source = (_DIRECTORY / filename).read_bytes()
    packaged = resources.files("adapters").joinpath(f"migrations/postgres/{filename}").read_bytes()
    assert source == packaged
    assert hashlib.sha256(source).hexdigest() == sha256


def test_current_packaging_includes_canonical_source_resource_tree_without_duplicates() -> None:
    configuration = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    wheel = configuration["tool"]["hatch"]["build"]["targets"]["wheel"]
    assert wheel["only-include"] == ["src"] and wheel["sources"] == ["src"]
    assert "packages" not in wheel
    assert not tuple(
        path
        for path in (_ROOT / "migrations").rglob("*")
        if path.is_file() and path.suffix in {".py", ".sql"}
    )


def test_current_runtime_remains_uncomposed_without_worker_or_migration_activation() -> None:
    main = import_module("main")
    parameters = inspect.signature(main.create_app).parameters
    assert "analysis_job_repository" not in parameters
    assert "analysis_job_worker_repository" not in parameters
    assert not hasattr(main.app.state, "analysis_job_repository")
    assert not hasattr(main.app.state, "analysis_job_worker_repository")
    for relative in ("src/main.py", "src/api/routes.py", "src/core/config.py"):
        source = (_ROOT / relative).read_text(encoding="utf-8")
        assert _REVISION not in source
        assert "services.analysis_job_worker" not in source
        assert "psycopg_analysis_job_worker_repository" not in source
        assert "claim_next_analysis_job" not in source
