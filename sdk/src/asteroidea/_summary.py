"""One session's stored summary, read from `GET /catalog/session-summary`.

The route answers `missing`, or the stored parts inline, or presigned addresses of
them. The presigned parts are fetched without the sign-in header (the address
carries its own signature). Each part is the body the live route returns, so the
tables here have the live routes' fields.
"""

import json
import urllib.parse
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

import pyarrow as pa

from asteroidea._client import PlatformClient, json_of, refusal
from asteroidea._http import Transport, send
from asteroidea.errors import QueryError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from asteroidea._auth import Auth

_OK: Final = 200
_ROUTE: Final = "/catalog/session-summary"
RATES_PREFIX: Final = "rates-"
_RATES_SCHEMA: Final = pa.schema(
    [
        ("unit_index", pa.int64()),
        ("rate_hz", pa.list_(pa.float64())),
        ("z", pa.list_(pa.float64())),
    ]
)
_EVENTS_SCHEMA: Final = pa.schema(
    [
        ("event_type", pa.string()),
        ("averaging", pa.string()),
        ("onset_s", pa.float64()),
        ("stop_s", pa.float64()),
        ("attributes", pa.string()),
    ]
)
_RESPONSIVE_SCHEMA: Final = pa.schema(
    [
        ("unit_index", pa.int64()),
        ("location", pa.string()),
        ("p", pa.float64()),
        ("q", pa.float64()),
        ("screened", pa.bool_()),
    ]
)


@dataclass(frozen=True)
class EventRates:
    """One event type's stored rates: every unit's rate and z per bin.

    `units` has one row per unit: `unit_index`, `rate_hz` and `z` (each a list with
    one value per bin; `z` is null for a unit whose baseline never varies). `edges`
    holds the bin edges in seconds from the event (one more than the bins). The z
    baseline is the bins before 0 s.
    """

    event_type: str
    edges: tuple[float, ...]
    n_trials: int
    units: pa.Table
    responsive: pa.Table
    label: str | None
    defaults: dict[str, Any]


@dataclass(frozen=True)
class SessionSummary:
    """A session's stored summary: its units, events and per-type rates.

    `units` is the file's units table (first page; `manifest["units_table"]` says
    whether more rows exist), `events` one row per event, `rates` maps each averaged
    event type to its `EventRates`, and `parts` keeps every stored body as the
    route returned it (the tuning screens among them).
    """

    dataset: str
    subject: str
    session: str
    asset_id: str
    manifest: dict[str, Any]
    units: pa.Table
    events: pa.Table
    rates: dict[str, EventRates]
    parts: dict[str, dict[str, Any]]


def _fetch_parts(
    urls: Mapping[str, Any], transport: Transport
) -> dict[str, dict[str, Any]]:
    """GET each presigned part, with no sign-in header; refuse a non-200."""
    parts: dict[str, dict[str, Any]] = {}
    for name, url in urls.items():
        reply = transport("GET", str(url), {}, None)
        if reply.status != _OK:
            msg = f"stored summary part {name} answered {reply.status}"
            raise QueryError(msg)
        body = json_of(reply)
        if not body:
            msg = f"stored summary part {name} is not a JSON object"
            raise QueryError(msg)
        parts[name] = body
    return parts


def _floats(values: list[float] | None) -> list[float] | None:
    """Return a list of numbers as floats; null stays `None`."""
    if values is None:
        return None
    return [float(v) for v in values]


def _rates(event_type: str, body: dict[str, Any]) -> EventRates:
    rows = [
        {
            "unit_index": int(u["unit_index"]),
            "rate_hz": _floats(u.get("rate_hz")),
            "z": _floats(u.get("z")),
        }
        for u in body.get("units", [])
    ]
    responsive = [
        {field.name: r.get(field.name) for field in _RESPONSIVE_SCHEMA}
        for r in body.get("responsive", [])
    ]
    label = body.get("label")
    return EventRates(
        event_type=event_type,
        edges=tuple(float(e) for e in body.get("edges", [])),
        n_trials=int(body.get("n_trials", 0)),
        units=pa.Table.from_pylist(rows, schema=_RATES_SCHEMA),
        responsive=pa.Table.from_pylist(responsive, schema=_RESPONSIVE_SCHEMA),
        label=label if isinstance(label, str) else None,
        defaults=dict(body.get("defaults") or {}),
    )


def _events(body: dict[str, Any]) -> pa.Table:
    rows = [
        {
            "event_type": kind.get("name"),
            "averaging": kind.get("averaging"),
            "onset_s": event.get("onset_s"),
            "stop_s": event.get("stop_s"),
            "attributes": json.dumps(event.get("attributes") or {}, sort_keys=True),
        }
        for kind in body.get("event_types", [])
        for event in kind.get("events", [])
    ]
    return pa.Table.from_pylist(rows, schema=_EVENTS_SCHEMA)


def _units(body: dict[str, Any]) -> pa.Table:
    rows = body.get("rows", [])
    return pa.Table.from_pylist(rows) if rows else pa.table({})


def session_summary(
    auth: Auth,
    dataset: str,
    subject: str,
    session: str,
    transport: Transport = send,
) -> SessionSummary | None:
    """Return the session's stored summary, or `None` when none is built yet.

    A refusal (a malformed name, a summary that cannot be read whole, a timeout)
    raises `QueryError` with the route's status, code and detail.
    """
    api = PlatformClient(auth.config.api_url, auth.tokens().access_token, transport)
    query = urllib.parse.urlencode(
        {"dataset": dataset, "subject": subject, "session": session}
    )
    reply = api.call("GET", f"{_ROUTE}?{query}")
    body = json_of(reply)
    if reply.status != _OK:
        code = body.get("error") or "error"
        msg = f"session summary answered {reply.status} {code}: {refusal(reply)}"
        raise QueryError(msg)
    if body.get("status") == "missing":
        return None
    if body.get("status") != "built":
        msg = f"session summary answered an unknown status {body.get('status')!r}"
        raise QueryError(msg)
    if body.get("delivery") == "presigned":
        parts = _fetch_parts(body.get("part_urls") or {}, transport)
    else:
        parts = {str(k): dict(v) for k, v in (body.get("parts") or {}).items()}
    manifest = dict(body.get("manifest") or {})
    rates = {
        name.removeprefix(RATES_PREFIX): _rates(name.removeprefix(RATES_PREFIX), part)
        for name, part in parts.items()
        if name.startswith(RATES_PREFIX)
    }
    return SessionSummary(
        dataset=dataset,
        subject=subject,
        session=session,
        asset_id=str(manifest.get("asset_id", "")),
        manifest=manifest,
        units=_units(parts.get("units", {})),
        events=_events(parts.get("events", {})),
        rates=rates,
        parts=parts,
    )
