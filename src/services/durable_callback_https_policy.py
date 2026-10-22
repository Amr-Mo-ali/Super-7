"""Optional HTTPS-only callback restriction for separated durable roles."""

from collections.abc import Mapping

from pydantic import HttpUrl

SETTING = "SUPER7_DURABLE_CALLBACK_HTTPS_ONLY"


def https_only_from_mapping(values: Mapping[str, str]) -> bool:
    value = values.get(SETTING, "0")
    if value not in {"0", "1"}:
        raise ValueError(f"{SETTING} must be 0 or 1.")
    return value == "1"


def require_https_callback(url: HttpUrl) -> None:
    if url.scheme != "https":
        raise ValueError("Callback URL must use HTTPS.")
