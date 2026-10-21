"""Keep the active child analysis import graph independent of HTTP modules."""

from __future__ import annotations

import ast
from collections import deque
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "src"
ROOTS = ("services.analysis_pipeline", "services.process_entrypoint")
# Composition is deliberately traversed: its YOLO adapters are legitimate children
# of the analysis graph, but an HTTP import anywhere beneath them must still fail.
COMPOSITION = "services.analysis_composition"


def _source(module: str) -> Path | None:
    stem = SOURCE.joinpath(*module.split("."))
    file = stem.with_suffix(".py")
    if file.is_file():
        return file
    package = stem / "__init__.py"
    return package if package.is_file() else None


def _imports(module: str, path: Path) -> set[str]:
    package = module if path.name == "__init__.py" else module.rpartition(".")[0]
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = package.split(".")
                prefix = ".".join(parts[: len(parts) - node.level + 1])
                base = f"{prefix}.{node.module}" if node.module else prefix
            else:
                base = node.module or ""
            found.add(base)
            found.update(f"{base}.{alias.name}" for alias in node.names if alias.name != "*")
    return found


def test_child_pipeline_transitive_imports_do_not_reach_http() -> None:
    pending: deque[tuple[str, tuple[str, ...]]] = deque((root, (root,)) for root in ROOTS)
    seen: set[str] = set()
    while pending:
        module, chain = pending.popleft()
        if module in seen:
            continue
        seen.add(module)
        assert module != "api" and not module.startswith("api."), " -> ".join(chain)
        assert module != "fastapi" and not module.startswith("fastapi."), " -> ".join(chain)
        path = _source(module)
        if path is None:
            continue
        for imported in _imports(module, path):
            if _source(imported) is not None or imported.split(".", 1)[0] in {
                "api",
                "fastapi",
            }:
                pending.append((imported, (*chain, imported)))
    assert COMPOSITION in seen


def test_durable_projection_consumers_do_not_import_concrete_presentation() -> None:
    prohibited = (
        "adapters.analysis_result_projection",
        "adapters.analysis_callback_presentation",
        "api",
        "fastapi",
    )
    for module in (
        "services.analysis_job_coordinator",
        "adapters.psycopg_analysis_job_worker_repository",
    ):
        path = _source(module)
        assert path is not None
        for imported in _imports(module, path):
            assert all(
                imported != prefix and not imported.startswith(f"{prefix}.")
                for prefix in prohibited
            ), (module, imported)
