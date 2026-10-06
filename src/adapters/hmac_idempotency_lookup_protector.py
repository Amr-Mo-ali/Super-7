"""Inactive HMAC protection for caller-scoped idempotency lookups."""

import hashlib as _hashlib
import hmac as _hmac

__all__ = ["HmacIdempotencyLookupProtector"]


class HmacIdempotencyLookupProtector:
    """Derive deterministic opaque lookup bytes from a caller-scoped raw key."""

    __slots__ = ("_secret",)

    def __init__(self, secret: bytes) -> None:
        if type(secret) is not bytes:
            raise TypeError("HMAC protector secret must be exact bytes.")
        if len(secret) < 32:
            raise ValueError("HMAC protector secret must be at least 32 bytes.")
        self._secret = secret

    def protect(
        self,
        *,
        caller_scope: str,
        raw_idempotency_key: str,
    ) -> bytes:
        """Return the raw HMAC-SHA256 digest for the exact framed inputs."""
        if type(caller_scope) is not str:
            raise TypeError("caller_scope must be an exact string.")
        if not caller_scope:
            raise ValueError("caller_scope must not be empty.")

        scope_utf8: bytes | None = None
        try:
            scope_utf8 = caller_scope.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            pass
        if scope_utf8 is None:
            raise ValueError("caller_scope must be valid UTF-8.") from None

        if type(raw_idempotency_key) is not str:
            raise TypeError("raw_idempotency_key must be an exact string.")
        if not raw_idempotency_key:
            raise ValueError("raw_idempotency_key must not be empty.")

        key_utf8: bytes | None = None
        try:
            key_utf8 = raw_idempotency_key.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            pass
        if key_utf8 is None:
            raise ValueError("raw_idempotency_key must be valid UTF-8.") from None

        framed_message = (
            b"super7:idempotency-lookup:v1\x00"
            + len(scope_utf8).to_bytes(8, "big")
            + scope_utf8
            + len(key_utf8).to_bytes(8, "big")
            + key_utf8
        )
        return _hmac.new(self._secret, framed_message, _hashlib.sha256).digest()
