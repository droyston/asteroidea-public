"""Index schema `asteroidea.dataset-index/2` as TypedDicts, plus mapping helpers."""

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol, TypedDict

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from asteroidea_services.harvest.progress import Progress
    from asteroidea_services.http import Transport

SCHEMA = "asteroidea.dataset-index/2"


class ShapeError(ValueError):
    """An archive record does not have the shape the harvester expects."""


class Species(TypedDict):
    """One species value."""

    taxon: str | None
    label: str


class License(TypedDict):
    """Licence as stated (``raw``) and mapped (``spdx``)."""

    spdx: str | None
    raw: str | None


class Urls(TypedDict):
    """Landing page and API URL."""

    landing: str
    api: str


class Deid(TypedDict):
    """De-identification flags."""

    affirmed_defaced: bool | None


class Dataset(TypedDict):
    """One dataset summary."""

    source: str
    id: str
    version: str
    version_ref: str | None
    version_modified: str | None
    title: str
    species: list[Species]
    species_status: str
    modalities: list[str]
    variables: list[str]
    has_units: bool
    has_lfp: bool
    has_ephys_series: bool
    approach: list[str]
    techniques: list[str]
    subjects: int | None
    bytes: int | None
    files: int | None
    license: License
    standards: list[str]
    urls: Urls
    published_at: str | None
    deid: Deid
    harvested_at: str


class Failure(TypedDict):
    """One record that failed to harvest."""

    id: str
    version: str | None
    url: str
    error_type: str
    message: str
    attempts: int
    stale: bool


class SourceSummary(TypedDict):
    """Per-source counts."""

    listed: int
    ok: int
    seconds: float
    failed: list[Failure]


class OpenNeuroSummary(SourceSummary):
    """Per-source counts plus the null nodes OpenNeuro returned."""

    null_nodes: int


class Harvester(TypedDict):
    """Producer identity."""

    package: str
    version: str


class Index(TypedDict):
    """The whole index document."""

    schema: str
    harvested_at: str
    harvester: Harvester
    sources: dict[str, SourceSummary | OpenNeuroSummary]
    datasets: list[Dataset]


@dataclass(frozen=True)
class Clock:
    """Wall clock and monotonic clock, injectable so tests freeze both."""

    now: Callable[[], datetime]
    monotonic: Callable[[], float]


def as_stale(failure: Failure) -> Failure:
    """Return ``failure`` marked stale: its dataset kept an older record."""
    marked = failure.copy()
    marked["stale"] = True
    return marked


@dataclass(frozen=True)
class Counts:
    """How an incremental run treated one source's datasets."""

    changed: int = 0
    new: int = 0
    removed: int = 0
    unchanged: int = 0

    def line(self, source: str) -> str:
        """Return the per-source progress line."""
        return (
            f"{source}: changed {self.changed}, new {self.new}, "
            f"removed {self.removed}, unchanged {self.unchanged}"
        )


@dataclass
class SourceResult:
    """What one source hands back."""

    datasets: list[Dataset]
    listed: int
    failed: list[Failure]
    seconds: float
    null_nodes: int | None = None
    counts: Counts | None = None


class SourceListingError(Exception):
    """A source's listing failed after retries or yielded nothing."""

    def __init__(self, source: str, message: str) -> None:
        """Name the source and the cause."""
        super().__init__(f"{source} listing failed: {message}")
        self.source = source
        self.message = message


class Source(Protocol):
    """An archive the harvester can read."""

    @property
    def name(self) -> str:
        """The key under ``sources`` in the index."""
        ...

    def harvest(
        self,
        http: Transport,
        progress: Progress,
        cached: Sequence[Dataset] | None = None,
    ) -> SourceResult:
        """Return every dataset summary; with ``cached``, re-fetch only changed ones."""
        ...


def as_obj(value: object, what: str) -> dict[str, object]:
    """Return ``value`` as a JSON object or raise ``ShapeError``."""
    if not isinstance(value, dict):
        msg = f"{what}: expected object, got {type(value).__name__}"
        raise ShapeError(msg)
    return {str(k): v for k, v in value.items()}


def as_list(value: object, what: str) -> list[object]:
    """Return ``value`` as a JSON array or raise ``ShapeError``."""
    if not isinstance(value, list):
        msg = f"{what}: expected array, got {type(value).__name__}"
        raise ShapeError(msg)
    return list(value)


def opt_obj(parent: dict[str, object], key: str) -> dict[str, object]:
    """Return the object at ``key``, or an empty one when absent or null."""
    value = parent.get(key)
    return {} if value is None else as_obj(value, key)


def opt_list(parent: dict[str, object], key: str) -> list[object]:
    """Return the array at ``key``, or an empty one when absent or null."""
    value = parent.get(key)
    return [] if value is None else as_list(value, key)


def opt_str(parent: dict[str, object], key: str) -> str | None:
    """Return the string at ``key``, ``None`` when absent or null."""
    value = parent.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        msg = f"{key}: expected string, got {type(value).__name__}"
        raise ShapeError(msg)
    return value


def opt_int(parent: dict[str, object], key: str) -> int | None:
    """Return the integer at ``key``, ``None`` when absent or null."""
    value = parent.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        msg = f"{key}: expected integer, got {type(value).__name__}"
        raise ShapeError(msg)
    return value


def str_list(parent: dict[str, object], key: str) -> list[str]:
    """Return the array of strings at ``key`` (empty when absent)."""
    items = opt_list(parent, key)
    if not all(isinstance(i, str) for i in items):
        msg = f"{key}: expected array of strings"
        raise ShapeError(msg)
    return [i for i in items if isinstance(i, str)]


def names(parent: dict[str, object], key: str) -> list[str]:
    """Return the ``name`` of each object in the array at ``key``."""
    out: list[str] = []
    for item in opt_list(parent, key):
        name = opt_str(as_obj(item, key), "name")
        if name is not None:
            out.append(name)
    return out


def dedupe(items: list[str]) -> list[str]:
    """Drop repeats, keep first-seen order."""
    return list(dict.fromkeys(items))


def utc_second(text: str | None) -> str | None:
    """Return an ISO timestamp as UTC, second resolution (``2023-10-10T18:11:47Z``)."""
    if text is None:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        msg = f"bad timestamp {text!r}"
        raise ShapeError(msg) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def stamp(moment: datetime) -> str:
    """Format a clock reading as UTC with second resolution."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


_TAXON_URL = re.compile(r"NCBITaxon_(\d+)$")


def taxon_from_url(identifier: str | None) -> str | None:
    """Map ``.../NCBITaxon_9606`` to ``NCBITaxon:9606``; ``None`` otherwise."""
    match = _TAXON_URL.search(identifier) if identifier is not None else None
    return f"NCBITaxon:{match.group(1)}" if match else None


# Free-text species (OpenNeuro) -> NCBITaxon, matched case-insensitively.
SPECIES_TABLE: dict[str, str] = {
    "human": "NCBITaxon:9606",
    "homo sapiens": "NCBITaxon:9606",
    "mouse": "NCBITaxon:10090",
    "mice": "NCBITaxon:10090",
    "rat": "NCBITaxon:10116",
    "macaque": "NCBITaxon:9544",
    "rhesus": "NCBITaxon:9544",
}
