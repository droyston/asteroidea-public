"""The upload request rules the session, artifact and import paths share.

Standard library only, so the zip Lambdas can import it. Each `*_problem` function
returns the sentence a refusal carries, or None when the value is valid.
"""

import base64
import binascii
import re
import uuid
from typing import Final

__all__ = [
    "CRC_BYTES",
    "DATASET_RE",
    "MAX_SIZE",
    "PUT_URL_SECONDS",
    "crc_problem",
    "path_problem",
    "upload_id_problem",
]

DATASET_RE: Final = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
MAX_SIZE: Final = 5 * 1024**3
CRC_BYTES: Final = 8
PUT_URL_SECONDS: Final = 3600
UUID4: Final = 4


def path_problem(path: object) -> str | None:
    """Return why `path` is not a relative POSIX path of real segments, or None."""
    if not isinstance(path, str) or not path:
        return "path must be a non-empty string"
    segments = path.split("/")
    if (
        path.startswith("/")
        or "\\" in path
        or any(s in {"", ".", ".."} for s in segments)
    ):
        return "path must be relative POSIX with no '..', '.' or empty segments"
    return None


def crc_problem(crc: object) -> str | None:
    """Return why `crc` is not base64 of an 8-byte CRC64NVME, or None."""
    message = "crc64nvme must be base64 of 8 bytes"
    if not isinstance(crc, str):
        return message
    try:
        decoded = base64.b64decode(crc, validate=True)
    except binascii.Error, ValueError:
        return message
    return message if len(decoded) != CRC_BYTES else None


def upload_id_problem(upload_id: object) -> str | None:
    """Return why `upload_id` is not a canonical uuid4 string, or None."""
    message = "upload_id must be a canonical uuid4 string"
    if not isinstance(upload_id, str):
        return message
    try:
        parsed = uuid.UUID(upload_id)
    except ValueError:
        return message
    if parsed.version != UUID4 or str(parsed) != upload_id:
        return message
    return None
