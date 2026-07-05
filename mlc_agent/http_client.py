from __future__ import annotations

import httpx


DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


class IdempotentGetRetryTransport(httpx.BaseTransport):
    """Retry only transient transport failures for idempotent GET requests."""

    def __init__(self, *, retries: int = 2) -> None:
        self._transport = httpx.HTTPTransport(retries=0)
        self._retries = retries

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        attempts = self._retries + 1 if request.method == "GET" else 1
        for attempt in range(attempts):
            try:
                return self._transport.handle_request(request)
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.RemoteProtocolError):
                if attempt == attempts - 1:
                    raise
        raise AssertionError("unreachable retry state")

    def close(self) -> None:
        self._transport.close()


def build_http_client(timeout_seconds: float = 20.0) -> httpx.Client:
    return httpx.Client(
        headers=DEFAULT_HEADERS,
        timeout=httpx.Timeout(timeout_seconds),
        follow_redirects=True,
        transport=IdempotentGetRetryTransport(retries=2),
    )
