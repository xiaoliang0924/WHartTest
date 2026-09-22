"""Rate-limit helpers for API test execution."""

from __future__ import annotations

import os
import re
import time
from typing import Any

DEFAULT_CASE_DELAY_SECONDS = 1.0
DEFAULT_RATE_LIMIT_MAX_RETRIES = 3
DEFAULT_RATE_LIMIT_WAIT_SECONDS = 2.0
DEFAULT_RATE_LIMIT_MAX_WAIT_SECONDS = 65.0


def get_case_delay_seconds() -> float:
    raw = os.environ.get(
        "WHARTTEST_API_CASE_DELAY_SECONDS",
        str(DEFAULT_CASE_DELAY_SECONDS),
    )
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return DEFAULT_CASE_DELAY_SECONDS


def get_rate_limit_max_retries() -> int:
    raw = os.environ.get(
        "WHARTTEST_API_RATE_LIMIT_MAX_RETRIES",
        str(DEFAULT_RATE_LIMIT_MAX_RETRIES),
    )
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_RATE_LIMIT_MAX_RETRIES


def sleep_between_cases() -> None:
    delay = get_case_delay_seconds()
    if delay > 0:
        time.sleep(delay)


def is_rate_limited_response(status_code: int, body: Any = None) -> bool:
    if status_code == 429:
        return True
    if status_code != 400 or not isinstance(body, dict):
        return False
    if body.get("code") == "RATE_LIMIT_EXCEEDED":
        return True
    message = str(body.get("message") or "")
    return "rate limit exceeded" in message.lower()


def parse_rate_limit_wait_seconds(
    *,
    status_code: int,
    headers: Any = None,
    body: Any = None,
    default: float = DEFAULT_RATE_LIMIT_WAIT_SECONDS,
    max_wait: float = DEFAULT_RATE_LIMIT_MAX_WAIT_SECONDS,
) -> float:
    headers = headers or {}
    retry_after = None
    if isinstance(headers, dict):
        for key, value in headers.items():
            if str(key).lower() == "retry-after":
                retry_after = value
                break
    if retry_after is not None:
        try:
            return min(float(retry_after), max_wait)
        except (TypeError, ValueError):
            pass

    message = ""
    if isinstance(body, dict):
        message = str(body.get("message") or body.get("code") or "")
    elif body is not None:
        message = str(body)

    match = re.search(r"retry in (\d+)", message, flags=re.IGNORECASE)
    if match:
        return min(float(match.group(1)) + 1.0, max_wait)

    if status_code == 429 or is_rate_limited_response(status_code, body):
        return min(default, max_wait)

    return min(default, max_wait)
