"""Harvest one summary per published dataset from DANDI and OpenNeuro."""

import argparse
import gzip
import json
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import TYPE_CHECKING, cast

from asteroidea_services.harvest.dandi import DandiSource
from asteroidea_services.harvest.model import (
    SCHEMA,
    Clock,
    Index,
    OpenNeuroSummary,
    Source,
    SourceListingError,
    SourceResult,
    SourceSummary,
    stamp,
)
from asteroidea_services.harvest.openneuro import OpenNeuroSource
from asteroidea_services.harvest.progress import Progress
from asteroidea_services.http import (
    Transport,
    live_transport,
    recording,
    replaying,
    retrying,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

DEFAULT_OUT = "web/public/index.json"


def _summary(result: SourceResult) -> SourceSummary | OpenNeuroSummary:
    """Return the counts and timing of one source for the index summary."""
    base: SourceSummary = {
        "listed": result.listed,
        "ok": len(result.datasets),
        "seconds": result.seconds,
        "failed": result.failed,
    }
    if result.null_nodes is None:
        return base
    return {**base, "null_nodes": result.null_nodes}


def build_index(
    sources: Sequence[Source],
    http: Transport,
    progress: Progress,
    clock: Clock,
    cached: Index | None = None,
) -> tuple[Index, list[SourceListingError]]:
    """Harvest every source. Returns the index and the sources whose listing failed.

    With ``cached`` each source re-fetches only what changed since that index.
    """
    index: Index = {
        "schema": SCHEMA,
        "harvested_at": stamp(clock.now()),
        "harvester": {
            "package": "asteroidea-services",
            "version": version("asteroidea-services"),
        },
        "sources": {},
        "datasets": [],
    }
    errors: list[SourceListingError] = []
    for source in sources:
        try:
            old = (
                None
                if cached is None
                else [d for d in cached["datasets"] if d["source"] == source.name]
            )
            result = source.harvest(http, progress, old)
        except SourceListingError as exc:
            errors.append(exc)
            progress.line(f"harvest: {exc.source} listing failed: {exc.message}")
            continue
        index["sources"][source.name] = _summary(result)
        index["datasets"].extend(result.datasets)
    return index, errors


def load_cache(out: Path) -> tuple[Index | None, str]:
    """Return the cached index at ``out``, or ``None`` and the reason for a rebuild."""
    try:
        doc = json.loads(out.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, f"no cached index at {out}"
    except (OSError, ValueError) as exc:
        return None, f"cached index unreadable: {type(exc).__name__}: {exc}"
    found = doc.get("schema") if isinstance(doc, dict) else None
    if found != SCHEMA:
        return None, f"cached schema {found!r} is not {SCHEMA!r}"
    return cast("Index", doc), "incremental"


def _content(index: Index) -> object:
    """Return the index as JSON minus the stamps and timings that change every run."""
    doc = json.loads(json.dumps(index))
    doc.pop("harvested_at", None)
    for summary in doc.get("sources", {}).values():
        summary.pop("seconds", None)
    return doc


def write_index(index: Index, out: Path) -> str:
    """Write ``index`` to ``<out>.tmp`` then rename; return a size summary."""
    out.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(index, indent=2, ensure_ascii=False) + "\n"
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(out)
    raw = len(text.encode())
    packed = len(gzip.compress(text.encode()))
    return f"{out}: {raw / 1e6:.2f} MB raw, {packed / 1e6:.2f} MB gzipped"


def _of(index: Index, source: str) -> list[object]:
    """Return the index records of one source."""
    return [d for d in index["datasets"] if d["source"] == source]


def _parser() -> argparse.ArgumentParser:
    """Return the command-line parser of the harvester."""
    parser = argparse.ArgumentParser(
        prog="python -m asteroidea_services.harvest", description=__doc__
    )
    parser.add_argument(
        "--out", type=Path, default=Path(DEFAULT_OUT), help="index file to write"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="ignore the cached index and rebuild from scratch",
    )
    parser.add_argument(
        "--record",
        type=Path,
        help="save each request/response pair under this directory",
    )
    parser.add_argument(
        "--replay", type=Path, help="serve responses from this directory; no network"
    )
    parser.add_argument(
        "--now", help="freeze the clock at this ISO time (fixture generation, tests)"
    )
    return parser


def _no_sleep(_seconds: float) -> None:
    """Skip the retry back-off when replaying recorded responses."""


def _clock(frozen: str | None) -> Clock:
    """Return the clock: the real one, or one frozen at `frozen`."""
    if frozen is None:
        return Clock(lambda: datetime.now(UTC), time.monotonic)
    fixed = datetime.fromisoformat(frozen)
    return Clock(lambda: fixed, lambda: 0.0)


def main(argv: Sequence[str] | None = None, progress: Progress | None = None) -> int:
    """Run the harvest; exit 0 when every listing worked, else 1 and no write."""
    args = _parser().parse_args(argv)
    progress = progress or Progress()
    clock = _clock(args.now)
    sleep: Callable[[float], None] = time.sleep
    if args.replay:
        base = replaying(args.replay)
        sleep = _no_sleep
    else:
        base = live_transport()
    if args.record:
        base = recording(base, args.record)
    http = retrying(base, sleep=sleep)
    sources: list[Source] = [DandiSource(clock), OpenNeuroSource(clock)]
    return run(sources, http, progress, clock, Target(args.out, args.force))


@dataclass(frozen=True)
class Target:
    """Where the index goes, and whether to ignore the one already there."""

    out: Path
    force: bool = False


def run(
    sources: Sequence[Source],
    http: Transport,
    progress: Progress,
    clock: Clock,
    target: Target,
) -> int:
    """Harvest into ``target.out``: incremental against its cache unless forced."""
    out = target.out
    cached, reason = (None, "--force") if target.force else load_cache(out)
    if cached is None:
        progress.line(f"harvest: full rebuild ({reason})")
    index, errors = build_index(sources, http, progress, clock, cached)
    if errors:
        last = errors[-1]
        progress.line(
            f"harvest: {last.source} listing failed: {last.message}; index not written"
        )
        return 1
    if cached is not None and _content(cached) == _content(index):
        counts = ", ".join(f"{k} {len(_of(index, k))}" for k in index["sources"])
        progress.line(f"harvest: up to date ({counts})")
        return 0
    progress.line(f"harvest: wrote {write_index(index, out)}")
    return 0


__all__ = [
    "DEFAULT_OUT",
    "Target",
    "build_index",
    "load_cache",
    "main",
    "run",
    "write_index",
]
_ = os  # keep import used below
