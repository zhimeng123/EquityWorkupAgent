import httpx

from mlc_agent.http_client import IdempotentGetRetryTransport


class _TransientTransport(httpx.BaseTransport):
    def __init__(self):
        self.calls = 0

    def handle_request(self, request):
        self.calls += 1
        if self.calls == 1:
            raise httpx.RemoteProtocolError("disconnected", request=request)
        return httpx.Response(200, request=request)


def test_transport_retries_transient_disconnect_for_get_only():
    transport = IdempotentGetRetryTransport(retries=1)
    inner = _TransientTransport()
    transport._transport = inner

    response = transport.handle_request(httpx.Request("GET", "https://example.com"))

    assert response.status_code == 200
    assert inner.calls == 2


def test_transport_does_not_retry_post():
    transport = IdempotentGetRetryTransport(retries=2)
    inner = _TransientTransport()
    transport._transport = inner

    try:
        transport.handle_request(httpx.Request("POST", "https://example.com"))
    except httpx.RemoteProtocolError:
        pass
    else:
        raise AssertionError("POST transport failure was retried")
    assert inner.calls == 1
