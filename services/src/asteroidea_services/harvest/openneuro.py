"""OpenNeuro source: GraphQL pagination, count-based stop, null nodes counted."""

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import TYPE_CHECKING

from asteroidea_services.harvest.model import (
    SPECIES_TABLE,
    Clock,
    Counts,
    Dataset,
    Failure,
    License,
    ShapeError,
    SourceListingError,
    SourceResult,
    Species,
    as_list,
    as_obj,
    as_stale,
    dedupe,
    opt_int,
    opt_list,
    opt_obj,
    opt_str,
    stamp,
    str_list,
    utc_second,
)
from asteroidea_services.http import HTTPError, Request, Transport

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from asteroidea_services.harvest.progress import Progress

ENDPOINT = "https://openneuro.org/crn/graphql"
PAGE_SIZE = 50
NODE = (
    "id metadata{species modalities affirmedDefaced} "
    "latestSnapshot{tag hexsha created description{Name License DatasetDOI} "
    "summary{modalities secondaryModalities subjects size totalFiles}}"
)
QUERY = (
    "query($after:String){datasets(first:50, after:$after)"
    "{pageInfo{count hasNextPage endCursor} "
    f"edges{{cursor node{{{NODE}}}}}}}}}"
)
# Change signal: the same listing without the heavy summary, 100 per page (the
# server's cap). A record is unchanged when tag, hexsha, species and defaced match.
LIGHT_QUERY = (
    "query($after:String){datasets(first:100, after:$after)"
    "{pageInfo{count hasNextPage endCursor} "
    "edges{cursor node{id metadata{species affirmedDefaced} "
    "latestSnapshot{tag hexsha}}}}}"
)
ONE_QUERY = f"query($id:ID!){{dataset(id:$id){{{NODE}}}}}"
WORKERS = 8

# Licence text (lowercased, trimmed) -> SPDX id; anything else: ``spdx: null``.
LICENSE_TABLE: dict[str, str] = {
    "cc0": "CC0-1.0",
    "cc0 1.0": "CC0-1.0",
    "cc0-1.0": "CC0-1.0",
    "pddl": "PDDL-1.0",
    "pddl-1.0": "PDDL-1.0",
    "cc-by-4.0": "CC-BY-4.0",
    "cc by 4.0": "CC-BY-4.0",
    "cc-by 4.0": "CC-BY-4.0",
    "creative commons attribution 4.0 international": "CC-BY-4.0",
}
_PDDL_MARKER = "opendatacommons.org/licenses/pddl"


def map_species(text: str | None) -> tuple[list[Species], str]:
    """Map ``metadata.species`` to ``(species, status)``; null and "" are not_stated."""
    label = (text or "").strip()
    if not label:
        return [], "not_stated"
    return [{"taxon": SPECIES_TABLE.get(label.lower()), "label": label}], "stated"


def map_license(raw: str | None) -> License:
    """Map a licence string through the table."""
    if raw is None:
        return {"spdx": None, "raw": None}
    key = raw.strip().lower()
    spdx = LICENSE_TABLE.get(key)
    if spdx is None and _PDDL_MARKER in key:
        spdx = "PDDL-1.0"
    return {"spdx": spdx, "raw": raw}


class NoSnapshotError(Exception):
    """A dataset without a published snapshot."""

    def __init__(self, identifier: str) -> None:
        """Keep the dataset id."""
        super().__init__("latestSnapshot is null")
        self.identifier = identifier


def map_dataset(node: object, harvested_at: str) -> Dataset:
    """Map one GraphQL node; raise ``NoSnapshotError`` for a node without a snapshot."""
    root = as_obj(node, "node")
    identifier = opt_str(root, "id")
    if identifier is None:
        msg = "node without id"
        raise ShapeError(msg)
    snapshot = root.get("latestSnapshot")
    if snapshot is None:
        raise NoSnapshotError(identifier)
    snap = as_obj(snapshot, "latestSnapshot")
    metadata = opt_obj(root, "metadata")
    description = opt_obj(snap, "description")
    summary = opt_obj(snap, "summary")
    tag = opt_str(snap, "tag") or ""
    species, status = map_species(opt_str(metadata, "species"))
    defaced = metadata.get("affirmedDefaced")
    subjects = opt_list(summary, "subjects") if "subjects" in summary else None
    return {
        "source": "openneuro",
        "id": identifier,
        "version": tag,
        "version_ref": opt_str(snap, "hexsha"),
        "version_modified": None,
        "title": opt_str(description, "Name") or identifier,
        "species": species,
        "species_status": status,
        "modalities": dedupe(
            [
                m.lower()
                for m in str_list(summary, "modalities")
                + str_list(summary, "secondaryModalities")
            ],
        ),
        "variables": [],
        "has_units": False,
        "has_lfp": False,
        "has_ephys_series": False,
        "approach": [],
        "techniques": [],
        "subjects": None if subjects is None else len(subjects),
        "bytes": opt_int(summary, "size"),
        "files": opt_int(summary, "totalFiles"),
        "license": map_license(opt_str(description, "License")),
        "standards": ["BIDS"],
        "urls": {
            "landing": f"https://openneuro.org/datasets/{identifier}/versions/{tag}",
            "api": ENDPOINT,
        },
        "published_at": utc_second(opt_str(snap, "created")),
        "deid": {"affirmed_defaced": defaced if isinstance(defaced, bool) else None},
        "harvested_at": harvested_at,
    }


def _failure(identifier: str, error_type: str, message: str) -> Failure:
    """Return the failure record of a dataset that could not be mapped."""
    return {
        "id": identifier,
        "version": None,
        "url": f"https://openneuro.org/datasets/{identifier}",
        "error_type": error_type,
        "message": message,
        # The record arrived in a listing page: that one fetch is its attempt.
        "attempts": 1,
        "stale": False,
    }


@dataclass(frozen=True)
class OpenNeuroSource:
    """Harvest the latest snapshot of every OpenNeuro dataset."""

    clock: Clock
    name: str = "openneuro"

    def _post(
        self, http: Transport, query: str, variables: dict[str, object], key: str
    ) -> object:
        """POST a GraphQL query and return the object under `key`.

        Raises when the response has no data for that key.
        """
        body = json.dumps({"query": query, "variables": variables}).encode()
        request = Request(
            "POST", ENDPOINT, (("Content-Type", "application/json"),), body
        )
        reply = as_obj(json.loads(http(request).body), "graphql response")
        data = opt_obj(reply, "data")
        if data.get(key) is None:
            errors = json.dumps(reply.get("errors"))[:300]
            msg = f"graphql response without data.{key}: {errors}"
            raise ShapeError(msg)
        return data[key]

    def _page(
        self, http: Transport, after: str | None, query: str = QUERY
    ) -> dict[str, object]:
        """Return one page of the dataset listing after the cursor `after`."""
        return as_obj(self._post(http, query, {"after": after}, "datasets"), "datasets")

    def _edges(
        self, http: Transport, query: str = QUERY
    ) -> Iterator[tuple[int, object]]:
        """Yield ``(total, edge)`` per page; stop at ``pageInfo.count`` or no edges."""
        seen = 0
        after: str | None = None
        total = 0
        first = True
        while first or seen < total:
            page = self._page(http, after, query)
            info = opt_obj(page, "pageInfo")
            count = opt_int(info, "count")
            if first and count is None:
                msg = "pageInfo.count missing"
                raise ShapeError(msg)
            total = count or total
            first = False
            edges = as_list(page.get("edges"), "edges")
            if not edges:
                return
            for edge in edges:
                seen += 1
                yield total, edge
            after = opt_str(info, "endCursor")
            if after is None and seen < total:
                msg = "pageInfo.endCursor missing"
                raise ShapeError(msg)

    def _record(self, edge: object) -> tuple[Dataset | None, Failure | None]:
        """Map one edge: ``(None, None)`` for a null edge or node."""
        node = None if edge is None else as_obj(edge, "edge").get("node")
        if node is None:
            return None, None
        try:
            return map_dataset(node, stamp(self.clock.now())), None
        except NoSnapshotError as exc:
            return None, _failure(exc.identifier, "NoSnapshot", str(exc))
        except ShapeError as exc:
            identifier = as_obj(node, "node").get("id")
            return None, _failure(str(identifier), "ShapeError", str(exc))

    def harvest(
        self,
        http: Transport,
        progress: Progress,
        cached: Sequence[Dataset] | None = None,
    ) -> SourceResult:
        """Page through ``datasets`` until ``pageInfo.count`` edges are read.

        With ``cached`` list only ``id``, ``tag``, ``hexsha`` and species, then
        fetch the full node of each new or changed dataset.
        """
        if cached is not None:
            return self._incremental(http, progress, cached)
        started = self.clock.monotonic()
        datasets: list[Dataset] = []
        failed: list[Failure] = []
        null_nodes = 0
        seen = 0
        try:
            for total, edge in self._edges(http):
                seen += 1
                progress.tick(self.name, total)
                dataset, failure = self._record(edge)
                if dataset:
                    datasets.append(dataset)
                elif failure:
                    failed.append(failure)
                    progress.line(_failed_line(failure))
                else:
                    null_nodes += 1
        except (HTTPError, ValueError) as exc:
            raise SourceListingError(self.name, f"{type(exc).__name__}: {exc}") from exc
        if seen == 0:
            msg = "listing returned zero records"
            raise SourceListingError(self.name, msg)
        seconds = round(self.clock.monotonic() - started, 1)
        progress.line(
            f"openneuro: listed {seen}, ok {len(datasets)}, null nodes {null_nodes}, "
            f"failed {len(failed)}, {seconds} s",
        )
        return SourceResult(datasets, seen, failed, seconds, null_nodes)

    def _light(self, http: Transport) -> tuple[list[dict[str, object]], int]:
        """Return the light node of every listed dataset and the null-node count."""
        nodes: list[dict[str, object]] = []
        null_nodes = 0
        try:
            for _total, edge in self._edges(http, LIGHT_QUERY):
                node = None if edge is None else as_obj(edge, "edge").get("node")
                if node is None:
                    null_nodes += 1
                else:
                    nodes.append(as_obj(node, "node"))
        except (HTTPError, ValueError) as exc:
            raise SourceListingError(self.name, f"{type(exc).__name__}: {exc}") from exc
        if not nodes and not null_nodes:
            msg = "listing returned zero records"
            raise SourceListingError(self.name, msg)
        return nodes, null_nodes

    def _fetch(
        self, http: Transport, identifier: str
    ) -> tuple[Dataset | None, Failure | None]:
        """Fetch and map one dataset; return the record or the failure."""
        try:
            node = self._post(http, ONE_QUERY, {"id": identifier}, "dataset")
            return map_dataset(node, stamp(self.clock.now())), None
        except NoSnapshotError as exc:
            return None, _failure(identifier, "NoSnapshot", str(exc))
        except HTTPError as exc:
            failure = _failure(identifier, exc.error_type, exc.message)
            return None, {**failure, "url": exc.url, "attempts": exc.attempts}
        except ValueError as exc:
            return None, _failure(identifier, type(exc).__name__, str(exc))

    def _incremental(
        self, http: Transport, progress: Progress, cached: Sequence[Dataset]
    ) -> SourceResult:
        """Refresh only the datasets that changed since the cached run."""
        started = self.clock.monotonic()
        nodes, null_nodes = self._light(http)
        progress.line(f"openneuro: listed {len(nodes) + null_nodes} datasets (light)")
        old = {d["id"]: d for d in cached}
        todo = [
            str(n.get("id"))
            for n in nodes
            if n.get("latestSnapshot") is not None
            and not _same(n, old.get(str(n.get("id"))))
        ]

        def one(identifier: str) -> tuple[Dataset | None, Failure | None]:
            """Fetch one dataset, report progress and return the outcome."""
            outcome = self._fetch(http, identifier)
            progress.tick(self.name, len(todo))
            if outcome[1]:
                progress.line(_failed_line(outcome[1]))
            return outcome

        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            fetched = dict(zip(todo, pool.map(one, todo), strict=True))
        datasets: list[Dataset] = []
        failed: list[Failure] = []
        for node in nodes:
            identifier = str(node.get("id"))
            record, failure = fetched.get(identifier, (old.get(identifier), None))
            if node.get("latestSnapshot") is None:
                record = None
                failure = _failure(identifier, "NoSnapshot", "latestSnapshot is null")
                progress.line(_failed_line(failure))
            if failure is not None and identifier in old:
                record, failure = old[identifier], as_stale(failure)
            if record is not None:
                datasets.append(record)
            if failure is not None:
                failed.append(failure)
        listed = {str(n.get("id")) for n in nodes}
        fresh = [i for i in todo if i not in old]
        counts = Counts(
            changed=len(todo) - len(fresh),
            new=len(fresh),
            removed=len(old.keys() - listed),
            unchanged=len(nodes) - len(todo),
        )
        progress.line(counts.line(self.name))
        seconds = round(self.clock.monotonic() - started, 1)
        progress.line(
            f"openneuro: listed {len(nodes) + null_nodes}, ok {len(datasets)}, "
            f"null nodes {null_nodes}, failed {len(failed)}, {seconds} s",
        )
        return SourceResult(
            datasets, len(nodes) + null_nodes, failed, seconds, null_nodes, counts
        )


def _same(node: dict[str, object], record: Dataset | None) -> bool:
    """Return whether a light node matches its record: tag, hexsha, species, defaced."""
    snapshot = node.get("latestSnapshot")
    if record is None or not isinstance(snapshot, dict):
        return False
    metadata = opt_obj(node, "metadata")
    species, status = map_species(opt_str(metadata, "species"))
    defaced = metadata.get("affirmedDefaced")
    return (
        snapshot.get("tag") == record["version"]
        and snapshot.get("hexsha") == record["version_ref"]
        and species == record["species"]
        and status == record["species_status"]
        and (defaced if isinstance(defaced, bool) else None)
        == record["deid"]["affirmed_defaced"]
    )


def _failed_line(failure: Failure) -> str:
    """Return the one-line progress text for a failed dataset."""
    return (
        f"openneuro: FAILED {failure['id']} {failure['error_type']}: "
        f"{failure['message']} ({failure['url']}, {failure['attempts']} attempt(s))"
    )
