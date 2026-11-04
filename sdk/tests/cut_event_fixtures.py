"""Cut the alignment-event fixtures under ``sdk/tests/fixtures/`` from the real files.

Run from the repo root (network, public HTTPS range reads, no credentials):

    uv run --frozen python sdk/tests/cut_event_fixtures.py

It opens each NWB file with h5py over remfile (the reader stack of
``asteroidea._remote`` and the extract Lambda) and pynwb, and builds the ``intervals``
rows with the production extractor (``asteroidea_ingest.extract.nwb.extract_nwb``),
so each row has the columns, types and ``columns_json`` text the catalog stores and
``sql()`` returns. For dandi 000623 it also writes the movie-frame timestamps of
``stimulus/presentation/movieframe_time`` (after checking that the string ``data`` is
the 0-based position, so frame ``f`` is ``timestamps[f - 1]``) and the pinned
scene-cut CSV (sha256 checked against ``curation/imports/000623-stimuli.json``).

``asset_id`` in the fixtures is the DANDI asset id, a fixture value: the catalog's
own ``asset_id`` is a uuid5 of the raw key and version.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, cast

import h5py
import numpy as np
import remfile
import requests
from pynwb import NWBHDF5IO

from asteroidea_ingest import bids_path
from asteroidea_ingest.extract.nwb import AssetScope, extract_nwb

ROOT: Final = Path(__file__).resolve().parents[2]
OUT: Final = Path(__file__).resolve().parent / "fixtures"
DANDI: Final = "https://api.dandiarchive.org/api/assets/{}/download/"
FILES: Final = {
    "dandi-000623": (
        "7887e844-7100-4384-975e-5ee38dbf153d",
        "sub-CS62/sub-CS62_ses-P62CSR2_behavior+ecephys.nwb",
    ),
    "dandi-001775": (
        "b7763578-7a3f-4919-ba98-38d2705f8400",
        "sub-UT06/sub-UT06_ses-Day5_behavior+ecephys.nwb",
    ),
}
MOVIE_SERIES: Final = "stimulus/presentation/movieframe_time"
MANIFEST: Final = ROOT / "curation" / "imports" / "000623-stimuli.json"


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=1) + "\n", encoding="utf-8")
    sys.stdout.write(f"wrote {path.relative_to(ROOT)}\n")


def _interval_rows(
    dataset: str, asset_id: str, relpath: str
) -> list[dict[str, object]]:
    parts = bids_path.parse(relpath)
    who = AssetScope(asset_id, dataset, parts.subject, parts.bids_session)
    with (
        contextlib.closing(remfile.File(DANDI.format(asset_id))) as remote,
        h5py.File(remote, "r") as handle,
        NWBHDF5IO(file=handle, load_namespaces=True) as io,
    ):
        rows = extract_nwb(io.read(), who, datetime.now(UTC)).intervals
    return [dict(row) for row in rows]


def _movie_timestamps(asset_id: str) -> list[float]:
    with (
        contextlib.closing(remfile.File(DANDI.format(asset_id))) as remote,
        h5py.File(remote, "r") as handle,
    ):
        group = handle[MOVIE_SERIES]
        if not isinstance(group, h5py.Group):
            msg = f"{MOVIE_SERIES} is not a group"
            raise TypeError(msg)
        data = np.asarray(group["data"])
        stamps = np.asarray(group["timestamps"], dtype=np.float64)
    labels = [x.decode() if isinstance(x, bytes) else str(x) for x in data.tolist()]
    if labels != [str(i) for i in range(len(labels))]:
        msg = f"{MOVIE_SERIES} data is not the 0-based frame position"
        raise ValueError(msg)
    return [float(x) for x in stamps]


def _scene_cut_csv() -> bytes:
    manifest = cast("dict[str, object]", json.loads(MANIFEST.read_text("utf-8")))
    entry = cast("list[dict[str, object]]", manifest["files"])[0]
    url = str(entry["url"])
    if not url.startswith("https://raw.githubusercontent.com/"):
        msg = f"unexpected manifest url {url}"
        raise ValueError(msg)
    reply = requests.get(url, timeout=60)
    reply.raise_for_status()
    body = reply.content
    if hashlib.sha256(body).hexdigest() != entry["sha256"]:
        msg = "scenecut_info.csv sha256 differs from the manifest"
        raise ValueError(msg)
    return body


def main() -> None:
    """Write every fixture."""
    for dataset, (asset_id, relpath) in FILES.items():
        _write_json(
            OUT / dataset / "intervals.json", _interval_rows(dataset, asset_id, relpath)
        )
    asset_623 = FILES["dandi-000623"][0]
    _write_json(
        OUT / "dandi-000623" / "movieframe_timestamps.json",
        {"series": MOVIE_SERIES, "timestamps": _movie_timestamps(asset_623)},
    )
    csv_path = OUT / "dandi-000623" / "scenecut_info.csv"
    csv_path.write_bytes(_scene_cut_csv())
    sys.stdout.write(f"wrote {csv_path.relative_to(ROOT)}\n")


if __name__ == "__main__":
    main()
