"""Offline request, fingerprint, and persisted metadata contract checks."""

from __future__ import annotations

import hashlib
import json

import pytest
from test_analysis_job_repository_contract import _row
from test_analysis_job_worker_repository_contract import _j

from adapters._analysis_job_persistence import _decode_job
from adapters.psycopg_analysis_job_repository import (
    AnalysisJobPersistenceInvariantError,
    _decode_row,
)
from domain.video_integrity import VideoIntegrity
from schemas.analysis import DurableAnalyzeRequest
from services.analysis_job_admission import fingerprint_analysis_request
from services.analysis_job_worker import AnalysisJobWorkerPersistenceInvariantError


def test_legacy_fingerprint_keeps_exact_v1_bytes_and_signed_callback_query() -> None:
    values = {
        "fingerprintVersion": "super7.analysis-job-fingerprint.v1",
        "callerScope": "apex",
        "videoId": "video",
        "playerId": "player",
        "videoReference": "clip.mp4",
        "callbackUrl": "https://example.com/callback?sig=a%2Fb",
    }
    expected = hashlib.sha256(
        json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).digest()
    assert (
        fingerprint_analysis_request(
            caller_scope="apex",
            video_id="video",
            player_id="player",
            video_reference="clip.mp4",
            callback_url="https://example.com/callback?sig=a%2Fb",
        )
        == expected
    )


def test_verified_fingerprint_binds_size_and_digest_without_changing_legacy() -> None:
    def fingerprint(integrity: VideoIntegrity | None = None) -> bytes:
        return fingerprint_analysis_request(
            caller_scope="apex",
            video_id="video",
            player_id="player",
            video_reference="clip.mp4",
            callback_url="https://example.com/callback?sig=a%2Fb",
            video_integrity=integrity,
        )

    digest = hashlib.sha256(b"video").hexdigest()
    legacy = fingerprint()
    verified = fingerprint(VideoIntegrity(5, digest))
    assert verified != legacy
    assert verified != fingerprint(VideoIntegrity(6, digest))
    assert verified != fingerprint(VideoIntegrity(5, "0" * 64))
    assert fingerprint() == legacy


def test_verified_request_requires_exact_key_and_full_metadata() -> None:
    legacy = {
        "videoId": "video",
        "playerId": "player",
        "videoUrl": "clip.mp4",
        "callbackUrl": "https://example.com/callback?sig=a%2Fb",
    }
    assert DurableAnalyzeRequest.model_validate(legacy).expected_integrity() is None
    verified = {
        **legacy,
        "videoKey": "videos/clip.mp4",
        "videoSizeBytes": 5,
        "videoSha256": hashlib.sha256(b"video").hexdigest(),
        "hashAlgorithm": "sha256",
    }
    parsed = DurableAnalyzeRequest.model_validate(verified)
    assert parsed.expected_integrity() == VideoIntegrity(5, str(verified["videoSha256"]))
    assert str(parsed.callback_url) == legacy["callbackUrl"]
    for changed in (
        {**verified, "videoKey": "other/clip.mp4"},
        {**verified, "videoSizeBytes": 0},
        {**verified, "videoSizeBytes": "5"},
        {**verified, "videoSha256": "A" * 64},
        {**verified, "hashAlgorithm": "SHA256"},
        {**verified, "hashAlgorithm": None},
    ):
        with pytest.raises(ValueError):
            DurableAnalyzeRequest.model_validate(changed)


@pytest.mark.parametrize(
    ("size", "digest"),
    ((None, b"x" * 32), (5, None), (0, b"x" * 32), (5, b"x" * 31)),
)
def test_both_database_readers_reject_corrupted_video_metadata(
    size: object, digest: object
) -> None:
    with pytest.raises(AnalysisJobPersistenceInvariantError):
        _decode_row((*_row()[:-2], size, digest))
    with pytest.raises(AnalysisJobWorkerPersistenceInvariantError):
        _decode_job((*_j()[:-2], size, digest))
