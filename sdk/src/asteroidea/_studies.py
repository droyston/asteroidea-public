"""Study, participant and link reads as Arrow tables.

`studies` and `links` are each one fixed query; a filter left as `None` matches
every row, and a given value is bound as a parameter, never spliced into the text.

`participants` reads the catalog routes instead: `GET /catalog/studies` (each
dataset's subjects with their modalities, from the stored dataset summaries the
web app's home page reads) and `GET /catalog/links`. A query of its own would bind
`assets`, `asset_headers`, `series` and `subjects` whole, which reads every file of
those tables (one per commit) and took minutes on the live catalog.
"""

import urllib.parse
from typing import TYPE_CHECKING, Any, Final

import pyarrow as pa

from asteroidea import _sql
from asteroidea._client import PlatformClient, json_of, refusal
from asteroidea._http import send
from asteroidea.errors import QueryError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from asteroidea._auth import Auth
    from asteroidea._http import Transport

# Null `tier` reads as curated for an import and as upload otherwise, as the catalog
# route does.
_STUDIES = (
    "SELECT s.study, s.title, s.authors, s.lab, s.citation, s.doi, sd.dataset, "
    "sd.role, d.title AS dataset_title, "
    "CASE WHEN d.dataset IS NULL THEN NULL ELSE coalesce(d.tier, "
    "CASE WHEN d.ingest_method = 'import' THEN 'curated' ELSE 'upload' END) "
    "END AS tier "
    "FROM studies s LEFT JOIN study_datasets sd ON sd.study = s.study "
    "LEFT JOIN datasets d ON d.dataset = sd.dataset "
    "WHERE (CAST(? AS VARCHAR) IS NULL OR (d.dataset IS NOT NULL AND "
    "coalesce(d.tier, CASE WHEN d.ingest_method = 'import' THEN 'curated' "
    "ELSE 'upload' END) = ?)) "
    "ORDER BY s.study, sd.dataset"
)
_LINKS = (
    "SELECT dataset, subject, subject_key, coalesce(status, 'pending') AS status, "
    "linked_by, linked_at, basis, note FROM subject_links "
    "WHERE (CAST(? AS VARCHAR) IS NULL OR dataset IN "
    "(SELECT dataset FROM study_datasets WHERE study = ?)) "
    "AND (CAST(? AS VARCHAR) IS NULL OR subject_key = ?) "
    "ORDER BY dataset, subject, subject_key"
)
_OK: Final = 200
_STUDIES_ROUTE: Final = "/catalog/studies"
_LINKS_ROUTE: Final = "/catalog/links"
# The catalog route's modality names and order (`catalog_api.MODALITIES`).
MODALITIES: Final = ("units", "lfp", "ieeg", "anat", "func", "task")
_BIDS_MODALITIES: Final = frozenset({"ieeg", "anat", "func"})


def _twice(*values: str | None) -> list[str | None]:
    """Return each value twice, in order, as `[a, a, b, b]`."""
    return [item for value in values for item in (value, value)]


def file_kind(datatype: object, suffix: object, fmt: object) -> str:
    """Name the modality of one non-NWB file: a named one or `other:<name>`.

    Same rule as the catalog route's `file_kind`, which names the modalities
    `participants` returns.
    """
    if suffix == "events":
        return "task"
    if datatype in _BIDS_MODALITIES:
        return str(datatype)
    for name in (datatype, suffix, fmt):
        if name:
            return f"other:{name}"
    return "other:unknown"


def _ordered(kinds: set[str]) -> list[str]:
    named = [m for m in MODALITIES if m in kinds]
    return named + sorted(kinds - set(MODALITIES))


def studies(auth: Auth, *, tier: str | None = None) -> pa.Table:
    """Return a row per study and dataset; a study with none has one null row."""
    return _sql.sql(auth, _STUDIES, _twice(tier))


def links(
    auth: Auth, *, study: str | None = None, subject_key: str | None = None
) -> pa.Table:
    """Return the subject link rows."""
    return _sql.sql(auth, _LINKS, _twice(study, subject_key))


def _get(api: PlatformClient, route: str) -> dict[str, Any]:
    """Return a route's JSON body; any status but 200 raises `QueryError`."""
    reply = api.call("GET", route)
    body = json_of(reply)
    if reply.status != _OK:
        code = body.get("error") or "error"
        msg = f"{route} answered {reply.status} {code}: {refusal(reply)}"
        raise QueryError(msg)
    return body


def participants(
    auth: Auth,
    *,
    study: str | None = None,
    has: Sequence[str] | None = None,
    transport: Transport | None = None,
) -> pa.Table:
    """Return one row per participant `subject_key`, with datasets and modalities.

    Each dataset's subjects and their modalities come from `GET /catalog/studies`,
    and each subject's keys from `GET /catalog/links` (any status); a subject with
    no link is its own `<dataset>/<subject>`. A refusal of either route raises
    `QueryError` with its status, code and detail.
    """
    api = PlatformClient(
        auth.config.api_url, auth.tokens().access_token, transport or send
    )
    catalog = _get(api, _STUDIES_ROUTE)
    query = "" if study is None else "?" + urllib.parse.urlencode({"study": study})
    found = _get(api, _LINKS_ROUTE + query).get("links", [])
    keys: dict[tuple[str, str], set[str]] = {}
    for link in found:
        pair = (str(link["dataset"]), str(link["subject"]))
        keys.setdefault(pair, set()).add(str(link["subject_key"]))
    members = None
    if study is not None:
        members = {
            str(member["dataset"])
            for entry in catalog.get("studies", [])
            if entry.get("study") == study
            for member in entry.get("datasets", [])
        }
    people: dict[str, dict[str, set[str]]] = {}
    for dataset in catalog.get("datasets", []):
        name = str(dataset["dataset"])
        if members is not None and name not in members:
            continue
        for subject in dataset.get("subjects", []):
            label = str(subject["subject"])
            for key in keys.get((name, label)) or {f"{name}/{label}"}:
                entry = people.setdefault(
                    key, {"datasets": set(), "subjects": set(), "modalities": set()}
                )
                entry["datasets"].add(name)
                entry["subjects"].add(f"{name}/{label}")
                entry["modalities"].update(subject.get("modalities", []))
    wanted = {has} if isinstance(has, str) else set(has or ())
    ordered = [k for k in sorted(people) if wanted <= people[k]["modalities"]]
    return pa.table(
        {
            "subject_key": pa.array(ordered, pa.string()),
            "datasets": pa.array(
                [sorted(people[k]["datasets"]) for k in ordered],
                pa.list_(pa.string()),
            ),
            "subjects": pa.array(
                [sorted(people[k]["subjects"]) for k in ordered],
                pa.list_(pa.string()),
            ),
            "modalities": pa.array(
                [_ordered(people[k]["modalities"]) for k in ordered],
                pa.list_(pa.string()),
            ),
        }
    )
