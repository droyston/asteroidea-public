"""Shared labels on a file: read them as an Arrow table."""

import json
import urllib.parse
from typing import TYPE_CHECKING, Any, Final

import pyarrow as pa

from asteroidea._auth import default_auth
from asteroidea._client import PlatformClient, json_of
from asteroidea._http import Transport, send
from asteroidea.errors import PlatformUnreachableError

if TYPE_CHECKING:
    from asteroidea._auth import Auth

_OK: Final = 200
_SCHEMA: Final = pa.schema(
    [
        ("label_id", pa.string()),
        ("name", pa.string()),
        ("note", pa.string()),
        ("start_s", pa.float64()),
        ("stop_s", pa.float64()),
        ("series", pa.string()),
        ("mine", pa.bool_()),
        ("created_at", pa.int64()),
        ("updated_at", pa.int64()),
        ("version", pa.int64()),
        ("s3_version_id", pa.string()),
        ("snapshots", pa.string()),
    ]
)


def _row(label: dict[str, Any]) -> dict[str, Any]:
    """Return one API label as a flat row; `snapshots` becomes JSON text."""
    row = {field.name: label.get(field.name) for field in _SCHEMA}
    row["mine"] = bool(label.get("mine"))
    snapshots = label.get("snapshots")
    row["snapshots"] = None if snapshots is None else json.dumps(snapshots)
    return row


def labels(auth: Auth, asset_id: str, transport: Transport = send) -> pa.Table:
    """Return every label on the file `asset_id`, earliest start first."""
    api = PlatformClient(auth.config.api_url, auth.tokens().access_token, transport)
    reply = api.call("GET", f"/files/{urllib.parse.quote(asset_id, safe='')}/labels")
    if reply.status != _OK:
        raise PlatformUnreachableError
    found = json_of(reply).get("labels", [])
    return pa.Table.from_pylist([_row(x) for x in found], schema=_SCHEMA)


def labels_of(asset_id: str) -> pa.Table:
    """Return the labels on a file (see `labels`)."""
    return labels(default_auth(), asset_id)
