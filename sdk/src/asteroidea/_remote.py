"""A read-only seekable remote file: each `read(n)` is exactly one ranged GET.

No read-ahead and no cache; pinned to one object version.
"""

import io
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Protocol, TypedDict, override

from botocore.exceptions import BotoCoreError, ClientError

from asteroidea.errors import ReadInterruptedError

if TYPE_CHECKING:
    from collections.abc import Buffer, Callable

EXPIRED_CODES: Final = frozenset(
    {"ExpiredToken", "ExpiredTokenException", "RequestExpired", "InvalidToken"}
)


class Body(Protocol):
    """The streaming body of a GET reply."""

    def read(self, amount: int | None = None, /) -> bytes:
        """Read the body."""
        ...


class GetReply(TypedDict):
    """The part of a `get_object` reply this module reads."""

    Body: Body


class ObjectClient(Protocol):
    """The one object-store call this module makes."""

    def get_object(self, **kwargs: str | int) -> GetReply:
        """Read a byte range of one object version."""
        ...


@dataclass(frozen=True)
class Source:
    """One object version."""

    bucket: str
    key: str
    version_id: str
    size: int


class RemoteFile(io.RawIOBase):
    """A seekable view of one object version; counts what it fetches.

    `client()` returns the client to use and `renewed()` one holding new
    credentials. An expired-credentials reply renews once and retries once;
    any other failure, or a second one, raises `ReadInterruptedError`.
    """

    def __init__(
        self,
        source: Source,
        client: Callable[[], ObjectClient],
        renewed: Callable[[], ObjectClient],
    ) -> None:
        """Open nothing yet; the first read makes the first request."""
        super().__init__()
        self.source = source
        self._make_client = client
        self._renew_client = renewed
        self._client: ObjectClient | None = None
        self._pos = 0
        self.bytes_fetched = 0
        self.requests = 0

    @override
    def readable(self) -> bool:
        """Return true."""
        return True

    @override
    def seekable(self) -> bool:
        """Return true."""
        return True

    @override
    def tell(self) -> int:
        """Return the position."""
        return self._pos

    @override
    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        """Move the position; no request is made."""
        base = {io.SEEK_SET: 0, io.SEEK_CUR: self._pos, io.SEEK_END: self.source.size}
        if whence not in base:
            msg = f"invalid whence {whence}"
            raise ValueError(msg)
        self._pos = max(0, base[whence] + offset)
        return self._pos

    def _get(self, client: ObjectClient, length: int) -> bytes:
        """Return `length` bytes from the current position with one ranged GET."""
        reply = client.get_object(
            Bucket=self.source.bucket,
            Key=self.source.key,
            VersionId=self.source.version_id,
            Range=f"bytes={self._pos}-{self._pos + length - 1}",
        )
        return reply["Body"].read()

    def _fetch(self, length: int) -> bytes:
        """Make one ranged request for `length` bytes at the position."""
        self.requests += 1
        if self._client is None:
            self._client = self._make_client()
        try:
            try:
                return self._get(self._client, length)
            except ClientError as err:
                if err.response.get("Error", {}).get("Code") not in EXPIRED_CODES:
                    raise
                self._client = self._renew_client()
                return self._get(self._client, length)
        except (ClientError, BotoCoreError) as err:
            raise ReadInterruptedError from err

    @override
    def readinto(self, buffer: Buffer) -> int:
        """Fill `buffer` from one ranged request; 0 at the end of the object."""
        view = memoryview(buffer).cast("B")
        length = min(len(view), self.source.size - self._pos)
        if length <= 0:
            return 0
        data = self._fetch(length)
        view[: len(data)] = data
        self._pos += len(data)
        self.bytes_fetched += len(data)
        return len(data)

    @override
    def readall(self) -> bytes:
        """Read to the end in one request."""
        length = self.source.size - self._pos
        if length <= 0:
            return b""
        data = self._fetch(length)
        self._pos += len(data)
        self.bytes_fetched += len(data)
        return data
