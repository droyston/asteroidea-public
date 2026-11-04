"""Read one stored per-session summary: `GET /catalog/session-summary`.

The LFP builder writes the summary (`dataset_summary.summarize_session`): parts under
`summary/<dataset>/<subject>/<session>/<digest>/`, the manifest last, then the pointer
`latest.json`. This module reads the pointer, the manifest it names, then each part
the manifest lists, and answers one of three bodies:

- `{"status": "missing"}` when the session has no `latest.json` yet. The page then
  computes live.
- `{"status": "built", "delivery": "inline", "manifest", "parts"}` when the parts fit
  the 5 MiB body. `parts` maps each part name without `.json` (`events`, `units`,
  `rates-<event_type>`, `tuning-<event_type>-<attribute>`) to the stored body, which
  is the body the live route returns for the same inputs. A manifest of an earlier
  minor schema version lists fewer parts; it is read the same way.
- `{"status": "built", "delivery": "presigned", "manifest", "part_urls",
  "expires_in"}` when they do not: `part_urls` maps the same names to presigned GETs
  of the stored objects (not read first; the manifest's `bytes` decides, or the
  assembled body when it passes the cap anyway).

Only a missing `latest.json` reads as missing. A pointer to a manifest that is not
there, a part the manifest lists that is not there, a body that is not the stored
shape, and every S3 refusal other than NoSuchKey (AccessDenied above all, which S3
answers for a missing key when the caller may not list the prefix) are
`502 summary_unreadable`, with the S3 error code in `detail`.

This module runs in the compute image. It reads `dataset_summary` attributes only
inside functions, because `dataset_summary` imports `compute_api`, which imports this
module.
"""

import json
from typing import TYPE_CHECKING, Any, Final

from botocore.exceptions import ClientError

from asteroidea_ingest import dataset_summary
from asteroidea_ingest.api_common import ApiError
from asteroidea_ingest.aws_types import S3_NOT_FOUND
from asteroidea_ingest.catalog_api import SESSION_NONE, SESSION_RE
from asteroidea_ingest.upload_rules import DATASET_RE

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from asteroidea._remote import ObjectClient

MAX_BODY_BYTES: Final = 5 * 1024 * 1024  # compute_api's cap; a test pins the pair
PART_URL_TTL_S: Final = 900
UNREADABLE: Final = "summary_unreadable"
PART_SUFFIX: Final = ".json"
NAME_PATTERN: Final = "^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"


def parse_query(query: Mapping[str, str]) -> tuple[str, str, str]:
    """Return ``(dataset, subject, session)``; 400 on a missing or malformed one.

    The dataset and session patterns are the catalog routes' (`DATASET_RE`,
    `SESSION_RE` with `_` for no session); the subject takes the session's name
    pattern, so every value is one key-safe path segment.
    """
    dataset = query.get("dataset", "")
    if not DATASET_RE.fullmatch(dataset):
        raise ApiError(400, "dataset must match ^[a-z0-9][a-z0-9_-]{0,62}$")
    subject = query.get("subject", "")
    if not SESSION_RE.fullmatch(subject):
        raise ApiError(400, f"subject must match {NAME_PATTERN}")
    session = query.get("session", "")
    if session != SESSION_NONE and not SESSION_RE.fullmatch(session):
        raise ApiError(
            400, f"session must be _ or match {NAME_PATTERN}", code="invalid_session"
        )
    return dataset, subject, session


def _unreadable(detail: str) -> ApiError:
    return ApiError(502, detail, code=UNREADABLE)


def _get(s3: ObjectClient, bucket: str, key: str) -> dict[str, Any] | None:
    """Return the JSON object at ``key``; None only when S3 answers NoSuchKey."""
    try:
        reply = s3.get_object(Bucket=bucket, Key=key)
    except ClientError as err:
        error = err.response.get("Error", {})
        code = str(error.get("Code"))
        if code in S3_NOT_FOUND:
            return None
        message = error.get("Message") or ""
        raise _unreadable(f"{key}: S3 {code} {message}".rstrip()) from err
    try:
        body = json.loads(reply["Body"].read())
    except ValueError as err:
        msg = f"{key}: not JSON: {err}"
        raise _unreadable(msg) from err
    if not isinstance(body, dict):
        msg = f"{key}: not a JSON object"
        raise _unreadable(msg)
    return body


def _need(s3: ObjectClient, bucket: str, key: str, named_by: str) -> dict[str, Any]:
    found = _get(s3, bucket, key)
    if found is None:
        msg = f"{named_by} names {key}, which does not exist"
        raise _unreadable(msg)
    return found


def _check_manifest(
    manifest: dict[str, Any], expected: dict[str, str], key: str
) -> list[str]:
    """Return the manifest's part names; 502 when it is not the stored shape."""
    schema = manifest.get("schema")
    major = dataset_summary.SESSION_SCHEMA_MAJOR
    if not isinstance(schema, str) or (
        schema != major and not schema.startswith(f"{major}.")
    ):
        msg = f"{key}: schema is {schema!r}, not {major} or a minor version of it"
        raise _unreadable(msg)
    for field, value in expected.items():
        if manifest.get(field) != value:
            got = manifest.get(field)
            msg = f"{key}: {field} is {got!r}, not {value!r}"
            raise _unreadable(msg)
    names = manifest.get("parts")
    if not isinstance(names, list) or not all(
        isinstance(n, str) and n.endswith(PART_SUFFIX) and "/" not in n for n in names
    ):
        msg = f"{key}: parts is not a list of part file names"
        raise _unreadable(msg)
    return [str(n) for n in names]


def read_summary(
    s3: ObjectClient,
    bucket: str,
    names: tuple[str, str, str],
    *,
    presign: Callable[[str, int], str] | None,
    check: Callable[[], None],
) -> dict[str, Any]:
    """Return the route body for one session (see the module docstring).

    ``check`` raises the deadline's 503 and runs before each part read.
    """
    dataset, subject, session = names
    latest_key = dataset_summary.latest_key(dataset, subject, session)
    latest = _get(s3, bucket, latest_key)
    if latest is None:
        return {"status": "missing"}
    digest = latest.get("digest")
    prefix = f"{dataset_summary.session_prefix(dataset, subject, session)}/{digest}"
    manifest_key = f"{prefix}/{dataset_summary.MANIFEST_FILE}"
    if not isinstance(digest, str) or latest.get("manifest") != manifest_key:
        got = latest.get("manifest")
        msg = f"{latest_key} names {got!r}, not {manifest_key}"
        raise _unreadable(msg)
    manifest = _need(s3, bucket, manifest_key, latest_key)
    expected = {
        "dataset": dataset,
        "subject": subject,
        "session": session,
        "digest": digest,
    }
    parts = _check_manifest(manifest, expected, manifest_key)
    stored = manifest.get("bytes")
    if isinstance(stored, int) and stored <= MAX_BODY_BYTES:
        inline: dict[str, Any] = {}
        for name in parts:
            check()
            key = f"{prefix}/{name}"
            inline[name.removesuffix(PART_SUFFIX)] = _need(
                s3, bucket, key, manifest_key
            )
        body = {
            "status": "built",
            "delivery": "inline",
            "manifest": manifest,
            "parts": inline,
        }
        text = json.dumps(body, allow_nan=False, separators=(",", ":"))
        if len(text.encode()) <= MAX_BODY_BYTES:
            return body
    if presign is None:
        raise ApiError(500, "no presigner is configured")
    return {
        "status": "built",
        "delivery": "presigned",
        "manifest": manifest,
        "part_urls": {
            name.removesuffix(PART_SUFFIX): presign(f"{prefix}/{name}", PART_URL_TTL_S)
            for name in parts
        },
        "expires_in": PART_URL_TTL_S,
    }
