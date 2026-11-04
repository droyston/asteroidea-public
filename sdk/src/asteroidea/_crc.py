"""CRC64NVME of a file, in the form the upload API and S3 use: base64 of 8 bytes."""

import base64
from typing import TYPE_CHECKING, Final

from awscrt import checksums

if TYPE_CHECKING:
    from pathlib import Path

CHUNK: Final = 8 * 1024 * 1024
HEADER: Final = "x-amz-checksum-crc64nvme"
_WIDTH: Final = 8


def crc64nvme(path: Path) -> str:
    """Return the file's CRC64NVME as base64 of its 8 big-endian bytes."""
    value = 0
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK):
            value = checksums.crc64nvme(chunk, value)
    return base64.b64encode(value.to_bytes(_WIDTH, "big")).decode()
