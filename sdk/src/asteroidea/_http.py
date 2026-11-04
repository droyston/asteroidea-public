"""Small HTTPS client over `http.client`: one request, status and body back."""

import http.client
import urllib.parse
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Final, NamedTuple

from asteroidea.errors import PlatformUnreachableError

TIMEOUT: Final = 60
CHUNK: Final = 1024 * 1024
_OK: Final = 200


class Reply(NamedTuple):
    """A response: the status code and the body."""

    status: int
    body: bytes


Transport = Callable[[str, str, Mapping[str, str], bytes | Path | None], Reply]
Downloader = Callable[[str, Path], int]


def connect(
    url: str, *, timeout: float = TIMEOUT, refusal: str | None = None
) -> tuple[http.client.HTTPSConnection, str]:
    """Return a connection to the host of `url` and the request target on it.

    Raises `PlatformUnreachableError` (with message `refusal`, when given) when the
    address is not https.
    """
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or parts.hostname is None:
        raise PlatformUnreachableError(refusal)
    target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    conn = http.client.HTTPSConnection(parts.hostname, parts.port, timeout=timeout)
    return conn, target


def send(
    method: str, url: str, headers: Mapping[str, str], body: bytes | Path | None = None
) -> Reply:
    """Send one request. A `Path` body streams from the file.

    Raises `PlatformUnreachableError` when the connection fails or the address is
    not https.
    """
    conn, target = connect(url)
    try:
        size = body.stat().st_size if isinstance(body, Path) else len(body or b"")
        conn.putrequest(method, target)
        for name, value in headers.items():
            conn.putheader(name, value)
        if body is not None:
            conn.putheader("Content-Length", str(size))
        conn.endheaders()
        if isinstance(body, Path):
            with body.open("rb") as handle:
                while chunk := handle.read(CHUNK):
                    conn.send(chunk)
        elif body is not None:
            conn.send(body)
        response = conn.getresponse()
        return Reply(response.status, response.read())
    except (OSError, http.client.HTTPException) as err:
        raise PlatformUnreachableError from err
    finally:
        conn.close()


def download(url: str, dest: Path) -> int:
    """GET `url` and stream the body into `dest`; return the status code.

    `dest` is written only on a 200. Raises `PlatformUnreachableError` when the
    connection fails or the address is not https.
    """
    conn, target = connect(url)
    try:
        conn.request("GET", target)
        response = conn.getresponse()
        if response.status == _OK:
            with dest.open("wb") as handle:
                while chunk := response.read(CHUNK):
                    handle.write(chunk)
    except (OSError, http.client.HTTPException) as err:
        raise PlatformUnreachableError from err
    else:
        return response.status
    finally:
        conn.close()
