"""Validated, optional expected bytes for an R2 video."""

from __future__ import annotations

import re
from dataclasses import dataclass

_SHA256 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True, slots=True)
class VideoIntegrity:
    size_bytes: int
    sha256: str

    def __post_init__(self) -> None:
        if type(self.size_bytes) is not int or not 0 < self.size_bytes <= 2**63 - 1:
            raise ValueError("Video size is outside the supported range.")
        if type(self.sha256) is not str or not _SHA256.fullmatch(self.sha256):
            raise ValueError("Video SHA-256 must be lowercase hexadecimal.")
