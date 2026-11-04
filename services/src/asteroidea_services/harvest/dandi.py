"""DANDI source: published dandisets only, one ``info/`` call per dandiset."""

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import TYPE_CHECKING

from asteroidea_services.harvest.model import (
    Clock,
    Counts,
    Dataset,
    Failure,
    License,
    ShapeError,
    SourceListingError,
    SourceResult,
    Species,
    as_obj,
    as_stale,
    dedupe,
    names,
    opt_int,
    opt_list,
    opt_obj,
    opt_str,
    stamp,
    str_list,
    taxon_from_url,
    utc_second,
)
from asteroidea_services.http import HTTPError, Request, Transport

if TYPE_CHECKING:
    from collections.abc import Sequence

    from asteroidea_services.harvest.progress import Progress

API = "https://api.dandiarchive.org/api"
LISTING_URL = f"{API}/dandisets/?page_size=1000&draft=false&empty=false"
WORKERS = 8


def _modality(approach: str) -> str:
    """Return the modality for a DANDI approach name; unknown names pass through."""
    lowered = approach.lower()
    if "electrophysiolog" in lowered:
        return "ecephys"
    if "microscopy" in lowered or "optical" in lowered:
        return "ophys"
    if "behavior" in lowered:
        return "behavior"
    return approach


def _species(summary: dict[str, object]) -> list[Species]:
    """Return the species of a DANDI summary; entries without a name or id skip."""
    out: list[Species] = []
    for item in opt_list(summary, "species"):
        entry = as_obj(item, "species")
        label = opt_str(entry, "name")
        identifier = opt_str(entry, "identifier")
        if label is None and identifier is None:
            continue
        out.append(
            {"taxon": taxon_from_url(identifier), "label": label or identifier or ""}
        )
    return out


def _license(metadata: dict[str, object]) -> License:
    """Return the licence of a DANDI version: its SPDX id, if any, and raw text."""
    entries = str_list(metadata, "license")
    raw = entries[0] if entries else None
    spdx = (
        raw.removeprefix("spdx:")
        if raw is not None and raw.startswith("spdx:")
        else None
    )
    return {"spdx": spdx, "raw": raw}


def _standards(summary: dict[str, object]) -> list[str]:
    """Return which of NWB and BIDS the data standards of a summary name."""
    out: list[str] = []
    for name in names(summary, "dataStandard"):
        out.extend(tag for tag in ("NWB", "BIDS") if tag in name.upper())
    return dedupe(out)


def map_dandiset(
    identifier: str,
    version: str,
    info: object,
    harvested_at: str,
    version_modified: str | None = None,
) -> Dataset:
    """Map one ``info/`` response to an index record."""
    root = as_obj(info, "info")
    metadata = opt_obj(root, "metadata")
    summary = opt_obj(metadata, "assetsSummary")
    variables = str_list(summary, "variableMeasured")
    species = _species(summary)
    approach = names(summary, "approach")
    landing = (
        opt_str(metadata, "url")
        or f"https://dandiarchive.org/dandiset/{identifier}/{version}"
    )
    return {
        "source": "dandi",
        "id": identifier,
        "version": version,
        "version_ref": opt_str(metadata, "doi"),
        "version_modified": version_modified,
        "title": opt_str(root, "name") or opt_str(metadata, "name") or identifier,
        "species": species,
        "species_status": "stated" if species else "not_stated",
        "modalities": dedupe([_modality(a) for a in approach]),
        "variables": variables,
        "has_units": "Units" in variables,
        "has_lfp": "LFP" in variables,
        "has_ephys_series": "ElectricalSeries" in variables,
        "approach": approach,
        "techniques": names(summary, "measurementTechnique"),
        "subjects": opt_int(summary, "numberOfSubjects"),
        "bytes": opt_int(summary, "numberOfBytes"),
        "files": opt_int(summary, "numberOfFiles"),
        "license": _license(metadata),
        "standards": _standards(summary),
        "urls": {
            "landing": landing,
            "api": f"{API}/dandisets/{identifier}/versions/{version}/",
        },
        "published_at": utc_second(
            opt_str(metadata, "datePublished") or opt_str(root, "created")
        ),
        "deid": {"affirmed_defaced": None},
        "harvested_at": harvested_at,
    }


@dataclass(frozen=True)
class _Listed:
    """One dandiset in the listing, with its latest published version."""

    identifier: str
    version: str | None
    modified: str | None = None


type _Outcome = tuple[Dataset | None, Failure | None]


def _get_json(http: Transport, url: str) -> object:
    """GET `url` and return the parsed JSON body."""
    return json.loads(http(Request("GET", url)).body)


def _list(http: Transport) -> list[_Listed]:
    """Return every dandiset of the listing, following the pages."""
    listed: list[_Listed] = []
    url: str | None = LISTING_URL
    try:
        while url is not None:
            page = as_obj(_get_json(http, url), "listing")
            for item in opt_list(page, "results"):
                entry = as_obj(item, "listing result")
                latest = opt_obj(entry, "most_recent_published_version")
                identifier = opt_str(entry, "identifier")
                if identifier is None:
                    msg = "listing result without identifier"
                    raise ShapeError(msg)
                listed.append(
                    _Listed(
                        identifier,
                        opt_str(latest, "version"),
                        opt_str(latest, "modified"),
                    )
                )
            url = opt_str(page, "next")
    except (HTTPError, ValueError) as exc:
        message = f"{type(exc).__name__}: {exc}"
        source = "dandi"
        raise SourceListingError(source, message) from exc
    return listed


def _failure(
    item: _Listed, url: str, error_type: str, message: str, attempts: int
) -> Failure:
    """Return the failure record of a dandiset whose fetch failed."""
    return {
        "id": item.identifier,
        "version": item.version,
        "url": url,
        "error_type": error_type,
        "message": message,
        "attempts": attempts,
        "stale": False,
    }


@dataclass(frozen=True)
class DandiSource:
    """Harvest published dandisets from the DANDI archive."""

    clock: Clock
    workers: int = WORKERS
    name: str = "dandi"

    def _fetch(self, http: Transport, item: _Listed) -> _Outcome:
        """Fetch and map one dandiset; return the record or the failure."""
        version = item.version or ""
        url = f"{API}/dandisets/{item.identifier}/versions/{version}/info/"
        if item.version is None:
            return None, _failure(item, url, "ShapeError", "no published version", 1)
        try:
            info = _get_json(http, url)
            return map_dandiset(
                item.identifier,
                item.version,
                info,
                stamp(self.clock.now()),
                item.modified,
            ), None
        except HTTPError as exc:
            return None, _failure(
                item, exc.url, exc.error_type, exc.message, exc.attempts
            )
        except ValueError as exc:
            return None, _failure(item, url, type(exc).__name__, str(exc), 1)

    def _one(
        self, http: Transport, progress: Progress, item: _Listed, total: int
    ) -> _Outcome:
        """Fetch one dandiset, report progress and return the outcome."""
        outcome = self._fetch(http, item)
        progress.tick("dandi", total)
        failure = outcome[1]
        if failure:
            progress.line(
                f"dandi: FAILED {failure['id']} {failure['error_type']}:"
                f" {failure['message']}"
                f" ({failure['url']}, {failure['attempts']} attempt(s))"
            )
        return outcome

    def harvest(
        self,
        http: Transport,
        progress: Progress,
        cached: Sequence[Dataset] | None = None,
    ) -> SourceResult:
        """List published dandisets, then read ``info/`` for each new or changed one.

        With ``cached`` a dandiset whose listed (version, modified) equals its
        cached record is carried over; one whose refresh fails keeps its cached
        record and is reported as stale.
        """
        started = self.clock.monotonic()
        listed = _list(http)
        if not listed:
            msg = "listing returned zero records"
            raise SourceListingError(self.name, msg)
        progress.line(f"dandi: listed {len(listed)} dandisets")
        old = {d["id"]: d for d in cached or []}
        todo = [i for i in listed if not _unchanged(i, old.get(i.identifier))]
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            fetched = list(
                pool.map(lambda i: self._one(http, progress, i, len(todo)), todo)
            )
        outcomes = dict(zip((i.identifier for i in todo), fetched, strict=True))
        datasets: list[Dataset] = []
        failed: list[Failure] = []
        for item in listed:
            outcome = outcomes.get(item.identifier)
            record = outcome[0] if outcome else old.get(item.identifier)
            failure = outcome[1] if outcome else None
            if failure is not None and item.identifier in old:
                record = old[item.identifier]
                failure = as_stale(failure)
            if record is not None:
                datasets.append(record)
            if failure is not None:
                failed.append(failure)
        counts = None
        if cached is not None:
            gone = old.keys() - {i.identifier for i in listed}
            fresh = [i for i in todo if i.identifier not in old]
            counts = Counts(
                changed=len(todo) - len(fresh),
                new=len(fresh),
                removed=len(gone),
                unchanged=len(listed) - len(todo),
            )
            progress.line(counts.line(self.name))
        seconds = round(self.clock.monotonic() - started, 1)
        progress.line(
            f"dandi: listed {len(listed)}, ok {len(datasets)}, "
            f"failed {len(failed)}, {seconds} s"
        )
        return SourceResult(datasets, len(listed), failed, seconds, counts=counts)


def _unchanged(item: _Listed, record: Dataset | None) -> bool:
    """Return whether the cached record matches the listing's version."""
    return (
        record is not None
        and item.version is not None
        and record["version"] == item.version
        and record["version_modified"] == item.modified
    )
