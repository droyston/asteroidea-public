"""Resolve a DANDI asset to its S3 blob URL and size."""

import json
from dataclasses import dataclass
from urllib.parse import urlsplit

from asteroidea_services.http import Request, Transport
from asteroidea_services.peek.count import PeekError

API = "https://api.dandiarchive.org/api"
BLOB_HOST = "dandiarchive.s3.amazonaws.com"
NWB_FORMAT = "application/x-nwb"


@dataclass(frozen=True)
class Resolved:
    """What peek needs to open one asset."""

    url: str
    size: int
    path: str


def asset_url(dandiset: str, version: str, asset: str) -> str:
    """Return the per-asset metadata endpoint."""
    return f"{API}/dandisets/{dandiset}/versions/{version}/assets/{asset}/"


def resolve(dandiset: str, version: str, asset: str, http: Transport) -> Resolved:
    """Read the asset metadata and return its blob URL, size and path."""
    url = asset_url(dandiset, version, asset)
    loaded: object = json.loads(http(Request("GET", url)).body)
    if not isinstance(loaded, dict):
        msg = f"asset metadata is not a JSON object ({url})"
        raise PeekError(msg)
    meta = {str(k): v for k, v in loaded.items()}
    encoding = meta.get("encodingFormat")
    if encoding != NWB_FORMAT:
        msg = f"asset encodingFormat is {encoding!r}, not {NWB_FORMAT} ({url})"
        raise PeekError(msg)
    size = meta.get("contentSize")
    if not isinstance(size, int) or isinstance(size, bool):
        msg = f"asset contentSize is {size!r}, not an integer ({url})"
        raise PeekError(msg)
    content_urls = meta.get("contentUrl")
    blobs = [
        entry
        for entry in (content_urls if isinstance(content_urls, list) else [])
        if isinstance(entry, str) and urlsplit(entry).hostname == BLOB_HOST
    ]
    if not blobs:
        msg = f"asset contentUrl has no {BLOB_HOST} entry ({url})"
        raise PeekError(msg)
    return Resolved(blobs[0], size, str(meta.get("path", "")))
