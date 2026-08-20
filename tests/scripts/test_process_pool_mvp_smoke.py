"""Deterministic coverage for the standalone process-pool admission smoke driver."""

import argparse
import importlib.util
import io
import json
import sys
from datetime import datetime
from email.message import Message
from pathlib import Path
from urllib.error import HTTPError

import pytest

_PATH = Path(__file__).parents[2] / "scripts" / "process_pool_mvp_smoke.py"
_SPEC = importlib.util.spec_from_file_location("process_pool_mvp_smoke", _PATH)
assert _SPEC is not None and _SPEC.loader is not None
smoke = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = smoke
_SPEC.loader.exec_module(smoke)


def _args(**values: object) -> argparse.Namespace:
    defaults: dict[str, object] = {
        "request_count": 2,
        "concurrency": 1,
        "timeout": 1.0,
        "callback_url": "https://approved.example/callback",
        "video_reference": ["one.mp4", "two.mp4"],
        "video_prefix": "video",
        "player_prefix": "player",
    }
    defaults.update(values)
    return argparse.Namespace(**defaults)


@pytest.mark.parametrize(
    "values",
    [
        {"request_count": 0},
        {"request_count": -1},
        {"concurrency": 0},
        {"concurrency": -1},
        {"concurrency": 3},
        {"timeout": 0},
        {"timeout": -1},
        {"callback_url": ""},
        {"video_reference": ["dir/file.mp4"]},
        {"video_reference": ["dir\\file.mp4"]},
        {"video_reference": ["C:\\file.mp4"]},
        {"video_reference": ["../file.mp4"]},
    ],
)
def test_invalid_options_fail(values: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        smoke.validate_options(_args(**values))


def test_valid_options_and_rotating_payloads() -> None:
    args = _args()
    smoke.validate_options(args)
    assert smoke.build_payload(0, args) == {
        "videoId": "video-0",
        "playerId": "player-0",
        "videoUrl": "one.mp4",
        "callbackUrl": "https://approved.example/callback",
    }
    assert smoke.build_payload(3, args)["videoUrl"] == "two.mp4"


class _Response:
    def __init__(self, status: int, body: bytes) -> None:
        self.status, self._body = status, body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_: object) -> None:
        return None


@pytest.mark.parametrize(
    ("status", "body", "category"),
    [
        (202, b'{"analysisId":"ok"}', None),
        (202, b"bad", "MalformedResponse"),
        (202, b"{}", "MalformedResponse"),
        (202, b'{"analysisId":" "}', "MalformedResponse"),
        (202, b'{"analysisId":1}', "MalformedResponse"),
    ],
)
def test_submit_validates_admitted_identity(
    monkeypatch: pytest.MonkeyPatch, status: int, body: bytes, category: str | None
) -> None:
    monkeypatch.setattr(smoke, "urlopen", lambda *_args, **_kwargs: _Response(status, body))
    result = smoke.submit("http://server", smoke.build_payload(0, _args()), 1)
    assert result.error_category == category


def test_submit_handles_http_rejection_and_transport_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = HTTPError("http://server", 503, "busy", Message(), io.BytesIO(b'{"detail":"busy"}'))
    monkeypatch.setattr(smoke, "urlopen", lambda *_args, **_kwargs: (_ for _ in ()).throw(error))
    assert smoke.submit("http://server", smoke.build_payload(0, _args()), 1).status_code == 503
    monkeypatch.setattr(
        smoke, "urlopen", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("secret"))
    )
    assert smoke.submit("http://server", smoke.build_payload(0, _args()), 1).status_code is None


def test_summary_and_url_sanitization() -> None:
    results = [
        smoke.RequestResult(0, "v", "p", "a.mp4", 202, 1, "id", None, None),
        smoke.RequestResult(1, "v", "p", "b.mp4", 503, 3, None, "HTTP503", "admission rejected"),
        smoke.RequestResult(2, "v", "p", "c.mp4", None, 2, None, "OSError", "transport failure"),
    ]
    assert smoke.summarize(results, 9) == {
        "attempted": 3,
        "admitted": 1,
        "rejected": 1,
        "transport_failures": 1,
        "status_codes": {"202": 1, "503": 1, "transport": 1},
        "admission_latency_ms": {"min": 1, "mean": 2, "max": 3},
        "driver_duration_ms": 9,
    }
    sanitized = smoke._sanitize_url("https://user:token@[::1]:8443/path?secret=x#frag")
    assert sanitized == "https://[::1]:8443/path"
    assert all(secret not in sanitized for secret in ("user", "token", "secret", "frag"))


def test_main_writes_sanitized_json_and_exit_codes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    result = smoke.RequestResult(0, "v", "p", "safe.mp4", 202, 1, "id", None, None)
    monkeypatch.setattr(smoke, "submit", lambda *_: result)
    output = tmp_path / "report.json"
    code = smoke.main(
        [
            "--base-url",
            "https://u:p@example.com/x?q=t#f",
            "--callback-url",
            "https://callback.example/token",
            "--video-reference",
            "safe.mp4",
            "--json-output",
            str(output),
        ]
    )
    rendered = capsys.readouterr().out
    report = json.loads(output.read_text())
    assert code == 0 and report["metadata"]["driver"] == "process_pool_mvp_smoke"
    assert report["metadata"]["schema_version"] == "process-pool-mvp-smoke-v1"
    assert datetime.fromisoformat(report["metadata"]["created_at_utc"]).tzinfo is not None
    assert report["requests"] and report["summary"]
    assert "callback.example" not in rendered and "callback.example" not in output.read_text()
    assert all(secret not in rendered for secret in ("u:p", "q=t", "#f"))


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (
            smoke.RequestResult(
                0, "v", "p", "safe.mp4", 503, 1, None, "HTTP503", "admission rejected"
            ),
            0,
        ),
        (
            smoke.RequestResult(
                0, "v", "p", "safe.mp4", None, 1, None, "OSError", "transport failure"
            ),
            1,
        ),
        (
            smoke.RequestResult(
                0,
                "v",
                "p",
                "safe.mp4",
                202,
                1,
                None,
                "MalformedResponse",
                "admitted response lacks analysis identity",
            ),
            1,
        ),
    ],
)
def test_main_exit_codes(monkeypatch: pytest.MonkeyPatch, result: object, expected: int) -> None:
    monkeypatch.setattr(smoke, "submit", lambda *_: result)
    assert (
        smoke.main(
            [
                "--base-url",
                "http://server",
                "--callback-url",
                "https://callback.example/path",
                "--video-reference",
                "safe.mp4",
            ]
        )
        == expected
    )
