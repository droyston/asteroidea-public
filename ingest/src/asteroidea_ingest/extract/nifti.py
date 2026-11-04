"""NIfTI-1 header extraction, header bytes only.

`.nii` needs the first 352 bytes (348 header plus the 4 extension bytes); `.nii.gz`
needs the first 64 KiB of the gzip stream, from which the first 348 bytes inflate.
nibabel parses the header; no voxel data is read.
"""

import io
import zlib
from typing import Any, Final

from nibabel.nifti1 import Nifti1Header

HEADER_BYTES: Final = 348
NII_RANGE_BYTES: Final = 352
GZ_RANGE_BYTES: Final = 64 * 1024
GZIP_WBITS: Final = 31  # zlib: gzip wrapper
# NIfTI xyzt_units to millimetres / seconds (nibabel names them).
SPACE_TO_MM: Final = {"meter": 1000.0, "mm": 1.0, "micron": 0.001}
TIME_TO_S: Final = {"sec": 1.0, "msec": 0.001, "usec": 0.000001}
SPATIAL_DIMS: Final = 3


class NiftiError(ValueError):
    """The bytes are not a NIfTI-1 header we can read."""


def range_bytes(extension: str) -> int:
    """Return how many leading bytes of the object a header read needs."""
    return GZ_RANGE_BYTES if extension.lower().endswith(".gz") else NII_RANGE_BYTES


def header_bytes(head: bytes, extension: str) -> bytes:
    """Return the 348 header bytes from the leading ``head`` of a `.nii[.gz]` object."""
    if extension.lower().endswith(".gz"):
        # A truncated gzip stream is expected: only the first bytes are inflated.
        head = zlib.decompressobj(GZIP_WBITS).decompress(head, HEADER_BYTES)
    if len(head) < HEADER_BYTES:
        msg = f"NIfTI header is {len(head)} bytes, need {HEADER_BYTES}"
        raise NiftiError(msg)
    return head[:HEADER_BYTES]


def parse(head: bytes, extension: str) -> dict[str, Any]:
    """Return the `asset_headers` NIfTI columns from the leading bytes of the object."""
    raw = header_bytes(head, extension)
    header: Any = Nifti1Header.from_fileobj(io.BytesIO(raw), check=False)
    if header["sizeof_hdr"] != HEADER_BYTES:
        msg = f"not a NIfTI-1 header (sizeof_hdr {int(header['sizeof_hdr'])})"
        raise NiftiError(msg)
    magic = bytes(header["magic"]).rstrip(b"\0")
    if magic not in {b"n+1", b"ni1"}:
        msg = f"not a NIfTI-1 header (magic {magic!r})"
        raise NiftiError(msg)
    shape = [int(n) for n in header.get_data_shape()]
    zooms = [float(z) for z in header.get_zooms()]
    space, time = header.get_xyzt_units()
    mm = SPACE_TO_MM.get(space or "mm", 1.0)
    voxel = [z * mm for z in zooms[:SPATIAL_DIMS]]
    # BIDS: pixdim[4] of a 4-D image is the repetition time in seconds.
    repetition = None
    if len(shape) > SPATIAL_DIMS and len(zooms) > SPATIAL_DIMS:
        repetition = zooms[SPATIAL_DIMS] * TIME_TO_S.get(time or "sec", 1.0)
    return {
        "nifti_shape": shape,
        "nifti_voxel_mm": voxel,
        "nifti_datatype": str(header.get_data_dtype()),
        "tr_seconds": repetition,
    }
