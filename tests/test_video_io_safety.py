"""Deterministic resource-safety tests for upload persistence."""

import asyncio
from pathlib import Path
from tempfile import SpooledTemporaryFile
from typing import BinaryIO, cast

import pytest
from fastapi import UploadFile

from core.config import Settings
from core.exceptions import InvalidVideoError, UploadTooLargeError
from services.video_validator import temporary_upload


def test_temporary_upload_removes_file_after_context() -> None:
    temporary = SpooledTemporaryFile()
    temporary.write(b"tiny-video-payload")
    temporary.seek(0)
    upload = UploadFile(cast(BinaryIO, temporary), filename="safe-name.avi")

    async def persist() -> Path:
        async with temporary_upload(upload, Settings()) as path:
            assert path.exists()
            return path

    path = asyncio.run(persist())
    assert not path.exists()


def test_temporary_upload_rejects_path_like_unsupported_filename() -> None:
    temporary = SpooledTemporaryFile()
    upload = UploadFile(cast(BinaryIO, temporary), filename="../../outside.txt")

    async def persist() -> None:
        async with temporary_upload(upload, Settings()):
            raise AssertionError("unsupported upload entered context")

    with pytest.raises(InvalidVideoError):
        asyncio.run(persist())


def test_temporary_upload_enforces_size_before_context_entry() -> None:
    temporary = SpooledTemporaryFile()
    temporary.write(b"12345")
    temporary.seek(0)
    upload = UploadFile(cast(BinaryIO, temporary), filename="video.avi")

    async def persist() -> None:
        async with temporary_upload(upload, Settings(max_upload_bytes=4)):
            raise AssertionError("oversized upload entered context")

    with pytest.raises(UploadTooLargeError):
        asyncio.run(persist())
