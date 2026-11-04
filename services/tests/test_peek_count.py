from io import BytesIO
from typing import TYPE_CHECKING, override

import pytest
from requests import PreparedRequest, Request, Response
from requests.adapters import BaseAdapter

from asteroidea_services.peek.count import CountingAdapter, PeekError

if TYPE_CHECKING:
    from requests import _types as _t


class Canned(BaseAdapter):
    def __init__(self, status: int, length: str | None, body: bytes = b"x") -> None:
        super().__init__()
        self.status, self.length, self.body = status, length, body

    @override
    def send(
        self,
        request: PreparedRequest,
        stream: bool = False,
        timeout: _t.TimeoutType = None,
        verify: _t.VerifyType = True,
        cert: _t.CertType = None,
        proxies: dict[str, str] | None = None,
    ) -> Response:
        del request, stream, timeout, verify, cert, proxies
        response = Response()
        response.status_code = self.status
        response.raw = BytesIO(self.body)
        if self.length is not None:
            response.headers["Content-Length"] = self.length
        return response

    @override
    def close(self) -> None:
        pass


def prepared(*, ranged: bool) -> PreparedRequest:
    headers = {"Range": "bytes=0-9"} if ranged else {}
    return Request("GET", "https://example.org/blob", headers=headers).prepare()


def test_count_sums_content_length_of_206s():
    adapter = CountingAdapter(Canned(206, "10"))
    adapter.send(prepared(ranged=True))
    adapter.send(prepared(ranged=True))
    assert adapter.bytes_read == 20
    assert [r.range_header for r in adapter.records] == ["bytes=0-9"] * 2


def test_count_rejects_unranged_200():
    adapter = CountingAdapter(Canned(200, "99"))
    with pytest.raises(PeekError, match="unranged response 200"):
        adapter.send(prepared(ranged=True))
    assert adapter.records == []


def test_count_rejects_request_without_range():
    adapter = CountingAdapter(Canned(206, "10"))
    with pytest.raises(PeekError, match="unranged response 206"):
        adapter.send(prepared(ranged=False))


def test_count_rejects_missing_content_length():
    adapter = CountingAdapter(Canned(206, None))
    with pytest.raises(PeekError, match="Content-Length"):
        adapter.send(prepared(ranged=True))
