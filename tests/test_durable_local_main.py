"""The separate local durable ASGI factory is fail-closed by default."""

from __future__ import annotations

import pytest

from durable_local_main import create_app


def test_local_durable_factory_requires_explicit_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SUPER7_DURABLE_LOCAL_ENABLED", raising=False)
    with pytest.raises(RuntimeError, match="not enabled"):
        create_app()
