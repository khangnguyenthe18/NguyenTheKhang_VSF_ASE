"""Chính sách retry cho HTTP (Teams) và SMTP (Mailpit / SMTP công ty) dùng tenacity."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

import aiosmtplib
import httpx
from tenacity import (
    AsyncRetrying,
    RetryCallState,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

T = TypeVar("T")

MAX_ATTEMPTS = 4
MAX_RETRY_AFTER_SECONDS = 60.0
_EXP_JITTER = wait_exponential_jitter(multiplier=0.5, max=10.0, jitter=0.5)


async def async_sleep(seconds: float) -> None:
    """Wrapper bọc `asyncio.sleep` để unit test dễ patch thời gian chờ."""
    await asyncio.sleep(seconds)


def should_retry_http(exc: BaseException) -> bool:
    """Retry 429, 5xx và lỗi mạng/timeout; không retry các lỗi 4xx khác (400, 401, 403, 404...)."""
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return code == 429 or 500 <= code < 600
    return isinstance(exc, (httpx.TransportError, httpx.TimeoutException, TimeoutError))


def should_retry_smtp(exc: BaseException) -> bool:
    """Retry lỗi tạm thời SMTP (4xx, timeout).

    Không retry lỗi xác thực 5xx (vd 535) và lỗi kết nối (fail-fast).
    """
    if isinstance(exc, (aiosmtplib.SMTPAuthenticationError, ConnectionRefusedError)):
        return False
    if isinstance(exc, aiosmtplib.SMTPConnectError):
        return False
    if isinstance(exc, OSError) and getattr(exc, "errno", None) == 111:
        return False
    if isinstance(exc, aiosmtplib.SMTPResponseException):
        return 400 <= exc.code < 500
    return isinstance(
        exc,
        (
            aiosmtplib.SMTPTimeoutError,
            aiosmtplib.SMTPServerDisconnected,
            TimeoutError,
            OSError,
        ),
    )


def wait_http(retry_state: RetryCallState) -> float:
    """Tôn trọng `Retry-After` khi gặp 429 (trần 60s); mặc định dùng backoff + jitter."""
    if retry_state.outcome is not None and retry_state.outcome.failed:
        exc = retry_state.outcome.exception()
        if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429:
            retry_after = exc.response.headers.get("Retry-After")
            if retry_after is not None:
                try:
                    return min(MAX_RETRY_AFTER_SECONDS, max(0.0, float(retry_after.strip())))
                except ValueError:
                    pass
    return float(_EXP_JITTER(retry_state))


def wait_smtp(retry_state: RetryCallState) -> float:
    return float(_EXP_JITTER(retry_state))


def http_retrying(
    sleep: Callable[[float], Awaitable[None]] = async_sleep,
    max_attempts: int = MAX_ATTEMPTS,
) -> AsyncRetrying:
    return AsyncRetrying(
        stop=stop_after_attempt(max_attempts),
        wait=wait_http,
        retry=retry_if_exception(should_retry_http),
        sleep=sleep,
        reraise=True,
    )


def smtp_retrying(
    sleep: Callable[[float], Awaitable[None]] = async_sleep,
    max_attempts: int = MAX_ATTEMPTS,
) -> AsyncRetrying:
    return AsyncRetrying(
        stop=stop_after_attempt(max_attempts),
        wait=wait_smtp,
        retry=retry_if_exception(should_retry_smtp),
        sleep=sleep,
        reraise=True,
    )
