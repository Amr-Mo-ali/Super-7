"""RED-B contract for the immutable AnalysisJob PostgreSQL migration."""

from __future__ import annotations

import ast
import hashlib
import inspect
import textwrap
import tomllib
from importlib import import_module, resources
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Any, Never

import pytest
from yoyo import read_migrations  # type: ignore[import-untyped]

_FOUNDATION_REVISION = "sprint2_slice1_foundation"
_ANALYSIS_JOB_REVISION = "sprint2_slice2_analysis_job"
_EXPECTED_CATALOG = (_FOUNDATION_REVISION, _ANALYSIS_JOB_REVISION)
_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_MIGRATION_DIRECTORY = _REPOSITORY_ROOT / "src" / "adapters" / "migrations" / "postgres"
_FOUNDATION_PATH = _MIGRATION_DIRECTORY / f"{_FOUNDATION_REVISION}.py"
_ANALYSIS_JOB_PATH = _MIGRATION_DIRECTORY / f"{_ANALYSIS_JOB_REVISION}.py"
_FOUNDATION_SHA256 = "9CCC2E1347B4B9D582923A6385866A9B0CFB7B3FC581E32E780A3A1D5195EEA4"
_RED_SOURCE_MISSING = (
    "Slice 2 RED-B: the canonical AnalysisJob source resource is absent; GREEN-B must add it."
)
_RED_PACKAGE_MISSING = "Slice 2 RED-B: the packaged AnalysisJob resource is absent."
_SHAPE_FAILURE = "Slice 2 RED-B: migration source is outside the canonical immutable contract."

_EXPECTED_APPLY_SQL = """CREATE TABLE analysis_jobs (
    job_id UUID PRIMARY KEY,
    idempotency_lookup BYTEA NOT NULL UNIQUE
        CHECK (octet_length(idempotency_lookup) > 0),
    request_fingerprint BYTEA NOT NULL
        CHECK (octet_length(request_fingerprint) = 32),
    video_id TEXT NOT NULL,
    player_id TEXT NOT NULL,
    video_reference TEXT NOT NULL,
    callback_url TEXT NOT NULL,
    state TEXT NOT NULL
        CHECK (
            state IN (
                'QUEUED',
                'RUNNING',
                'COMPLETED',
                'FAILED',
                'CANCELLED'
            )
        ),
    accepted_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX analysis_jobs_queued_accepted_at_job_id_idx
    ON analysis_jobs (accepted_at, job_id)
    WHERE state = 'QUEUED';"""

_EXPECTED_ROLLBACK_SQL = """DO $super7$
BEGIN
    IF EXISTS (SELECT 1 FROM analysis_jobs) THEN
        RAISE EXCEPTION
            'Cannot roll back sprint2_slice2_analysis_job while analysis_jobs contains rows.';
    END IF;
END
$super7$;

DROP TABLE analysis_jobs;"""


def _packaged_migration_directory() -> Traversable:
    return resources.files("adapters").joinpath("migrations/postgres")


def _analysis_job_source_resource() -> Path:
    if not _ANALYSIS_JOB_PATH.is_file():
        pytest.fail(_RED_SOURCE_MISSING, pytrace=False)
    return _ANALYSIS_JOB_PATH


def _analysis_job_package_resource() -> Traversable:
    resource = _packaged_migration_directory().joinpath(f"{_ANALYSIS_JOB_REVISION}.py")
    if not resource.is_file():
        pytest.fail(_RED_PACKAGE_MISSING, pytrace=False)
    return resource


def _source_migration_catalog() -> list[Any]:
    return list(read_migrations(str(_MIGRATION_DIRECTORY)))


def _package_migration_catalog() -> list[Any]:
    return list(read_migrations("package:adapters:migrations/postgres"))


def _analysis_job_source_migration() -> Any:
    migration = next(
        (
            candidate
            for candidate in _source_migration_catalog()
            if str(candidate.id) == _ANALYSIS_JOB_REVISION
        ),
        None,
    )
    if migration is None:
        pytest.fail(
            "Slice 2 RED-B: the source migration catalog omits AnalysisJob.",
            pytrace=False,
        )
    return migration


def _fail_shape() -> Never:
    pytest.fail(_SHAPE_FAILURE, pytrace=False)


def _single_assignment(statement: ast.stmt, target: str) -> ast.expr:
    if not isinstance(statement, ast.Assign) or len(statement.targets) != 1:
        _fail_shape()
    assigned = statement.targets[0]
    if not isinstance(assigned, ast.Name) or assigned.id != target:
        _fail_shape()
    return statement.value


def _literal_string(node: ast.expr) -> str:
    if not isinstance(node, ast.Constant) or type(node.value) is not str:
        _fail_shape()
    try:
        value = ast.literal_eval(node)
    except (TypeError, ValueError):
        _fail_shape()
    if type(value) is not str:
        _fail_shape()
    return value


def _canonical_migration_literals(source: str) -> tuple[str, str, frozenset[str]]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        _fail_shape()
    if len(tree.body) != 3:
        _fail_shape()

    import_statement, dependency_statement, steps_statement = tree.body
    if not (
        isinstance(import_statement, ast.ImportFrom)
        and import_statement.level == 0
        and import_statement.module == "yoyo"
        and len(import_statement.names) == 1
        and import_statement.names[0].name == "step"
        and import_statement.names[0].asname is None
    ):
        _fail_shape()

    dependency_node = _single_assignment(dependency_statement, "__depends__")
    if not isinstance(dependency_node, ast.Set) or len(dependency_node.elts) != 1:
        _fail_shape()
    try:
        dependency_value = ast.literal_eval(dependency_node)
    except (TypeError, ValueError):
        _fail_shape()
    if dependency_value != {_FOUNDATION_REVISION}:
        _fail_shape()

    steps_node = _single_assignment(steps_statement, "steps")
    if not isinstance(steps_node, ast.List) or len(steps_node.elts) != 1:
        _fail_shape()
    call = steps_node.elts[0]
    if not (
        isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and call.func.id == "step"
        and len(call.args) == 2
        and not call.keywords
    ):
        _fail_shape()
    apply_sql, rollback_sql = (_literal_string(argument) for argument in call.args)
    return apply_sql, rollback_sql, frozenset(dependency_value)


def _normalize_sql_literal(sql: str) -> str:
    normalized_newlines = sql.replace("\r\n", "\n").replace("\r", "\n")
    return textwrap.dedent(normalized_newlines).strip("\n")


def _canonical_source(apply_sql: str, rollback_sql: str) -> str:
    return (
        "from yoyo import step\n\n"
        f"__depends__ = {{{_FOUNDATION_REVISION!r}}}\n\n"
        "steps = [\n"
        "    step(\n"
        f"        {apply_sql!r},\n"
        f"        {rollback_sql!r},\n"
        "    ),\n"
        "]\n"
    )


def _assert_shape_rejected(source: str) -> None:
    with pytest.raises(pytest.fail.Exception, match=f"^{_SHAPE_FAILURE}$"):
        _canonical_migration_literals(source)


def test_red_b_ast_contract_accepts_only_one_public_literal_yoyo_step() -> None:
    source = _canonical_source(_EXPECTED_APPLY_SQL, _EXPECTED_ROLLBACK_SQL)
    apply_sql, rollback_sql, dependencies = _canonical_migration_literals(source)
    steps_offset = source.index("steps = [")
    conditional_source = (
        source[:steps_offset] + "if enabled:\n" + textwrap.indent(source[steps_offset:], "    ")
    )

    assert apply_sql == _EXPECTED_APPLY_SQL
    assert rollback_sql == _EXPECTED_ROLLBACK_SQL
    assert dependencies == frozenset({_FOUNDATION_REVISION})

    rejected_sources = (
        source.replace("from yoyo import step", "from yoyo import step as migration_step"),
        source.replace("from yoyo import step", "import yoyo"),
        source.replace("step(\n", "yoyo.step(\n"),
        source.replace(f"{_EXPECTED_APPLY_SQL!r},", "APPLY_SQL,"),
        source.replace(f"{_EXPECTED_APPLY_SQL!r},", "'prefix' + 'suffix',"),
        source.replace(f"{_EXPECTED_APPLY_SQL!r},", "f'{generated_sql}',"),
        source.replace(f"        {_EXPECTED_ROLLBACK_SQL!r},\n", ""),
        source.replace(
            f"        {_EXPECTED_ROLLBACK_SQL!r},\n",
            f"        {_EXPECTED_ROLLBACK_SQL!r},\n        'third argument',\n",
        ),
        source.replace(
            "    ),\n]",
            f"    ),\n    step({_EXPECTED_APPLY_SQL!r}, {_EXPECTED_ROLLBACK_SQL!r}),\n]",
        ),
        source.replace("steps = [", "step = replacement\n\nsteps = ["),
        conditional_source,
        source.replace("steps = [", "def helper():\n    return None\n\nsteps = ["),
        source.replace("steps = [", "raise RuntimeError('must not execute')\n\nsteps = ["),
        source.replace(_FOUNDATION_REVISION, "wrong_dependency", 1),
    )
    for rejected in rejected_sources:
        _assert_shape_rejected(rejected)


def test_red_b_sql_normalization_is_limited_to_layout_artifacts() -> None:
    indented_crlf = (
        "\r\n"
        + "\r\n".join(f"        {line}" if line else "" for line in _EXPECTED_APPLY_SQL.split("\n"))
        + "\r\n"
    )
    assert _normalize_sql_literal(indented_crlf) == _EXPECTED_APPLY_SQL
    indented_rollback = "\n    " + _EXPECTED_ROLLBACK_SQL.replace("\n", "\n    ") + "\n"
    assert _normalize_sql_literal(indented_rollback) == _EXPECTED_ROLLBACK_SQL

    noncanonical = (
        _EXPECTED_APPLY_SQL.replace("> 0", ">0", 1),
        _EXPECTED_APPLY_SQL.replace("'QUEUED'", "'queued'", 1),
        _EXPECTED_APPLY_SQL.replace("job_id UUID", "job_id uuid.fake", 1),
        _EXPECTED_APPLY_SQL.replace("video_id TEXT NOT NULL", "video_id TEXT", 1),
        _EXPECTED_APPLY_SQL + "\nGRANT SELECT ON analysis_jobs TO PUBLIC;",
        _EXPECTED_APPLY_SQL + "\n-- approved text in a comment",
    )
    assert all(_normalize_sql_literal(value) != _EXPECTED_APPLY_SQL for value in noncanonical)


def test_slice2_canonical_source_has_exact_immutable_literals() -> None:
    source = _analysis_job_source_resource().read_text(encoding="utf-8")
    apply_sql, rollback_sql, dependencies = _canonical_migration_literals(source)

    assert dependencies == frozenset({_FOUNDATION_REVISION})
    assert _normalize_sql_literal(apply_sql) == _EXPECTED_APPLY_SQL
    assert _normalize_sql_literal(rollback_sql) == _EXPECTED_ROLLBACK_SQL


def test_slice2_public_yoyo_metadata_is_exact() -> None:
    resource = _analysis_job_source_resource()
    migration = _analysis_job_source_migration()

    assert resource.name == f"{_ANALYSIS_JOB_REVISION}.py"
    assert str(migration.id) == _ANALYSIS_JOB_REVISION
    assert {str(dependency.id) for dependency in migration.depends} == {_FOUNDATION_REVISION}


def test_source_catalog_order_is_foundation_then_analysis_job() -> None:
    assert (
        tuple(str(migration.id) for migration in _source_migration_catalog()) == _EXPECTED_CATALOG
    )


def test_package_resource_and_catalog_match_source_boundary() -> None:
    package_resource = _analysis_job_package_resource()
    source_resource = _analysis_job_source_resource()
    packaged_resources = tuple(
        sorted(
            item.name
            for item in _packaged_migration_directory().iterdir()
            if item.is_file() and item.name.endswith(".py")
        )
    )

    assert packaged_resources == tuple(f"{revision}.py" for revision in _EXPECTED_CATALOG)
    assert (
        tuple(str(migration.id) for migration in _package_migration_catalog()) == _EXPECTED_CATALOG
    )
    assert source_resource.read_bytes() == package_resource.read_bytes()


def test_slice1_foundation_is_first_byte_identical_and_domain_free() -> None:
    source = _FOUNDATION_PATH.read_text(encoding="utf-8")
    migration_ids = tuple(str(migration.id) for migration in _source_migration_catalog())

    assert hashlib.sha256(_FOUNDATION_PATH.read_bytes()).hexdigest().upper() == _FOUNDATION_SHA256
    assert migration_ids[0] == _FOUNDATION_REVISION
    assert "analysis_jobs" not in source.lower()
    assert "create table" not in " ".join(source.lower().split())


def test_migration_resource_tree_is_canonical_and_packaged() -> None:
    configuration = tomllib.loads((_REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    wheel = configuration["tool"]["hatch"]["build"]["targets"]["wheel"]
    duplicate_root = _REPOSITORY_ROOT / "migrations"
    duplicate_resources = tuple(
        path
        for path in duplicate_root.rglob("*")
        if path.is_file() and path.suffix.lower() in {".py", ".sql"}
    )

    assert _MIGRATION_DIRECTORY.is_dir()
    assert _FOUNDATION_PATH.is_file()
    assert duplicate_resources == ()
    assert wheel["only-include"] == ["src"]
    assert wheel["sources"] == ["src"]
    assert "packages" not in wheel


def test_current_application_runtime_has_no_slice2_migration_activation() -> None:
    main = import_module("main")
    parameters = inspect.signature(main.create_app).parameters
    source = inspect.getsource(main)

    assert "analysis_job_repository" not in parameters
    assert "idempotency_lookup_protector" not in parameters
    assert not hasattr(main.app.state, "analysis_job_repository")
    assert _ANALYSIS_JOB_REVISION not in source
    assert str(_ANALYSIS_JOB_PATH.relative_to(_REPOSITORY_ROOT)).replace("\\", "/") not in source
