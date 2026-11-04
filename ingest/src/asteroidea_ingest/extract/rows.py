"""Derived row bundles of a re-extraction: format, manifest, checks.

A derive task runs an asset's extractor and records each catalog write it would make,
in order, as one *operation*: ``replace`` or ``append_if_absent``, a table, a key and
the rows. It stores them in the derived store under a prefix of its own,
distinct from the LFP builder's ``lfp/``:

    rows/<asset_id>/<run_id>/rows.jsonl.gz   one JSON line of rows per operation
    rows/<asset_id>/<run_id>/manifest.json   written last

The manifest names each operation's table, key, row count and the sha256 of its line,
plus the sha256 and size of the whole bundle. The extract handler reads the manifest,
checks everything, then commits the operations in order (it stays the one
catalog writer). Stdlib only (json, gzip, hashlib): no new dependency in the extract
image. A row value is None, bool, int, float, str, a naive or aware ``datetime``, or a
list of those; anything else is refused at write time, so a decoded row equals the row
the extractor produced.
"""

import gzip
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final, Protocol, cast

from botocore.exceptions import ClientError

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

PREFIX: Final = "rows"
BUNDLE_NAME: Final = "rows.jsonl.gz"
MANIFEST_NAME: Final = "manifest.json"
MANIFEST_VERSION: Final = 1
REPLACE: Final = "replace"
APPEND: Final = "append_if_absent"
# Bound on the compressed bundle, checked on write and on read. Measured offline on a
# synthetic file in the shape of the largest catalog asset modelled (CS62 R2: 96
# electrodes, 44 units, 76,279 spikes, 41 trials) at 677,839 B gzip, 1,447,585 B raw:
# `test_extract_handler.py::test_a_cs62_shaped_bundle_is_far_under_the_bound`.
MAX_BUNDLE_BYTES: Final = 64 * 1024 * 1024
MANIFEST_FIELDS: Final = frozenset(
    {"asset_id", "run_id", "dataset", "s3_key", "s3_version_id", "bundle", "operations"}
)
_DATETIME_TAG: Final = "$datetime"
_NO_SUCH_KEY: Final = frozenset({"NoSuchKey", "404"})


class RowsError(ValueError):
    """A bundle or manifest is missing, malformed, or fails a check."""


@dataclass(frozen=True, slots=True)
class Operation:
    """One catalog write a derive recorded."""

    op: str
    table: str
    key: dict[str, Any]
    rows: list[dict[str, Any]]


class RowStore(Protocol):
    """The derived-store calls: one put per object, one whole-object get."""

    def put_object(self, **kwargs: str | bytes) -> object:
        """Write one object."""
        ...

    def get_object(self, **kwargs: str) -> Mapping[str, Any]:
        """Read one object (``Body``)."""
        ...


def prefix(asset_id: str, run_id: str) -> str:
    """Return ``rows/<asset_id>/<run_id>``, the prefix of one asset's derive."""
    return f"{PREFIX}/{asset_id}/{run_id}"


def manifest_key(asset_id: str, run_id: str) -> str:
    """Return the manifest key of one asset's derive."""
    return f"{prefix(asset_id, run_id)}/{MANIFEST_NAME}"


def bundle_key(asset_id: str, run_id: str) -> str:
    """Return the bundle key of one asset's derive."""
    return f"{prefix(asset_id, run_id)}/{BUNDLE_NAME}"


def _encode_value(value: object) -> object:
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, datetime):
        return {_DATETIME_TAG: value.isoformat()}
    if isinstance(value, list):
        return [_encode_value(item) for item in value]
    msg = f"row value of type {type(value).__name__} cannot be stored"
    raise RowsError(msg)


def _decode_value(value: object) -> object:
    if isinstance(value, dict):
        tagged = cast("dict[str, object]", value)
        if set(tagged) != {_DATETIME_TAG}:
            msg = f"row value {tagged!r} is not a tagged datetime"
            raise RowsError(msg)
        return datetime.fromisoformat(str(tagged[_DATETIME_TAG]))
    if isinstance(value, list):
        return [_decode_value(item) for item in value]
    return value


def encode_rows(rows: Sequence[Mapping[str, Any]]) -> bytes:
    """Return one JSON line (no newline) holding ``rows``, keys sorted."""
    encoded = [{str(k): _encode_value(v) for k, v in row.items()} for row in rows]
    return json.dumps(encoded, sort_keys=True, separators=(",", ":")).encode()


def decode_rows(line: bytes) -> list[dict[str, Any]]:
    """Return the rows of one line written by ``encode_rows``."""
    loaded: object = json.loads(line)
    if not isinstance(loaded, list):
        msg = "a bundle line is not a list of rows"
        raise RowsError(msg)
    rows: list[dict[str, Any]] = []
    for row in loaded:
        if not isinstance(row, dict):
            msg = "a bundle row is not an object"
            raise RowsError(msg)
        rows.append({str(k): _decode_value(v) for k, v in row.items()})
    return rows


def encode_json(value: Mapping[str, Any]) -> bytes:
    """Return ``value`` as sorted-key JSON bytes (the manifest)."""
    return json.dumps(value, sort_keys=True, indent=1).encode()


def sha256(data: bytes) -> str:
    """Return the hex sha256 of ``data``."""
    return hashlib.sha256(data).hexdigest()


def build(
    operations: Sequence[Operation], header: Mapping[str, Any]
) -> tuple[bytes, dict[str, Any]]:
    """Return the gzip bundle and the manifest (without the bundle key) of a derive.

    ``header`` holds asset_id, run_id and the other manifest fields. Raises
    ``RowsError`` when the bundle is over ``MAX_BUNDLE_BYTES``.
    """
    lines = [encode_rows(op.rows) for op in operations]
    bundle = gzip.compress(b"\n".join(lines), mtime=0)
    if len(bundle) > MAX_BUNDLE_BYTES:
        msg = f"row bundle is {len(bundle)} bytes, over {MAX_BUNDLE_BYTES}"
        raise RowsError(msg)
    manifest = {
        "version": MANIFEST_VERSION,
        **header,
        "bundle": {"sha256": sha256(bundle), "bytes": len(bundle)},
        "operations": [
            {
                "op": op.op,
                "table": op.table,
                "key": dict(op.key),
                "rows": len(op.rows),
                "sha256": sha256(line),
            }
            for op, line in zip(operations, lines, strict=True)
        ],
    }
    return bundle, manifest


def _get(store: RowStore, bucket: str, key: str, what: str) -> bytes:
    try:
        reply = store.get_object(Bucket=bucket, Key=key)
    except ClientError as err:
        if err.response.get("Error", {}).get("Code") in _NO_SUCH_KEY:
            msg = f"{what} {key} is missing"
            raise RowsError(msg) from err
        raise
    return bytes(reply["Body"].read())


def read_manifest(store: RowStore, bucket: str, key: str) -> dict[str, Any]:
    """Return the manifest at ``key``; ``RowsError`` when missing or not an object."""
    raw = _get(store, bucket, key, "manifest")
    try:
        loaded: Any = json.loads(raw)
    except ValueError as err:
        msg = f"manifest {key} is not JSON"
        raise RowsError(msg) from err
    if not isinstance(loaded, dict) or loaded.get("version") != MANIFEST_VERSION:
        msg = f"manifest {key} is not a version {MANIFEST_VERSION} manifest"
        raise RowsError(msg)
    missing = sorted(MANIFEST_FIELDS - loaded.keys())
    if missing:
        msg = f"manifest {key} lacks {', '.join(missing)}"
        raise RowsError(msg)
    return loaded


def read_operations(
    store: RowStore, bucket: str, manifest: Mapping[str, Any]
) -> list[Operation]:
    """Read the bundle the manifest names and return its checked operations.

    Checks, before anything is returned: bundle size and sha256, one line per listed
    operation, and each line's sha256 and row count.
    """
    entry = manifest["bundle"]
    key = str(entry["key"])
    if int(entry["bytes"]) > MAX_BUNDLE_BYTES:
        msg = f"row bundle is {entry['bytes']} bytes, over {MAX_BUNDLE_BYTES}"
        raise RowsError(msg)
    bundle = _get(store, bucket, key, "row bundle")
    if sha256(bundle) != entry["sha256"]:
        msg = f"row bundle {key} sha256 differs from the manifest"
        raise RowsError(msg)
    listed: list[dict[str, Any]] = list(manifest["operations"])
    lines = gzip.decompress(bundle).split(b"\n") if listed else []
    if len(lines) != len(listed):
        msg = f"row bundle holds {len(lines)} operations, manifest lists {len(listed)}"
        raise RowsError(msg)
    operations: list[Operation] = []
    for item, line in zip(listed, lines, strict=True):
        table = str(item["table"])
        if sha256(line) != item["sha256"]:
            msg = f"rows of {table} sha256 differs from the manifest"
            raise RowsError(msg)
        rows = decode_rows(line)
        if len(rows) != int(item["rows"]):
            msg = f"rows of {table}: {len(rows)} in the bundle, {item['rows']} listed"
            raise RowsError(msg)
        operations.append(Operation(str(item["op"]), table, dict(item["key"]), rows))
    return operations
