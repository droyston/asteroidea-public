"""HTTP-layer byte counter: a requests adapter that records every response."""

from dataclasses import dataclass
from http import HTTPStatus
from typing import TYPE_CHECKING, override

from requests.adapters import BaseAdapter

if TYPE_CHECKING:
    from requests import PreparedRequest, Response
    from requests import _types as _t


class PeekError(Exception):
    """A failure peek reports as its own message, without the class name."""


@dataclass(frozen=True)
class Counted:
    """One response seen by the adapter."""

    range_header: str | None
    status: int
    length: int


class CountingAdapter(BaseAdapter):
    """Wrap ``inner``; record each response's ``Content-Length`` and fail closed.

    Bytes read are the sum of ``Content-Length`` over every response. A response to a
    request without ``Range``, or with a status other than 206, raises ``PeekError``.
    """

    def __init__(self, inner: BaseAdapter) -> None:
        """Keep the adapter that does the network work."""
        super().__init__()
        self.inner = inner
        self.records: list[Counted] = []
        self.violations: list[str] = []

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
        """Send through ``inner``, then record and check the response."""
        response = self.inner.send(request, stream, timeout, verify, cert, proxies)
        range_header = request.headers.get("Range")
        if range_header is None or response.status_code != HTTPStatus.PARTIAL_CONTENT:
            message = f"unranged response {response.status_code} for {request.url}"
            self.violations.append(message)
            raise PeekError(message)
        length = response.headers.get("Content-Length")
        if length is None or not length.isdigit():
            message = f"response without a Content-Length for {request.url}"
            self.violations.append(message)
            raise PeekError(message)
        self.records.append(
            Counted(str(range_header), response.status_code, int(length))
        )
        return response

    @override
    def close(self) -> None:
        """Close ``inner``."""
        self.inner.close()

    @property
    def bytes_read(self) -> int:
        """Sum of ``Content-Length`` over the recorded responses."""
        return sum(record.length for record in self.records)
