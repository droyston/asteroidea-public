"""Worker for `make live-analysis`: one researcher analysis step inside the fresh venv.

A researcher browses and queries real data solely through the SDK. This file
therefore imports only `asteroidea` (its public calls), numpy, pandas and the
standard library; `infra/tests/test_analysis.py` fails on any other import.

Run as `python analysis_check_worker.py <step>` with `ASTEROIDEA_CONFIG_DIR` in the
environment. It prints one JSON line of measurements on stdout. An SDK error becomes
`{"error": "<Class>: <the SDK's own message>"}` and exit status 1; so does a lookup
that does not find exactly the rows a step needs. The steps are the six analysis steps
of the live analysis check; `make live-analysis` judges the numbers.
"""

import io
import json
import sys
import time
from typing import Any, Final, cast

import numpy as np
import pandas as pd

import asteroidea

DANDI: Final = "dandi-000623"
OPENNEURO: Final = "openneuro-ds004798"
# curation/subject-links/bmovie.csv: the curated keys of patients 41 and 62 (the
# step 2 query names the same two keys).
PATIENT_KEYS: Final = ("bmovie-patient-41", "bmovie-patient-62")
# `make import` brought in the CS62 R2 recording with INCLUDE='sub-CS62/*P62CSR2*'.
CS62_SUBJECT: Final = "sub-CS62"
CS62_R2_MARK: Final = "P62CSR2"
SCENECUT_PATH: Final = "stimuli/scenecut_info.csv"
FMRI_SUBJECT: Final = "sub-p62cs"
FMRI_RUN_MARK: Final = "_run-1_"
WINDOW_S: Final = 1.0
LFP_ROWS: Final = 1000
LFP_COLS: Final = 8


class NotFoundError(Exception):
    """The catalog or the file does not hold exactly the rows a step needs."""


def _rows(table: object) -> list[dict[str, Any]]:
    return list(cast("Any", table).to_pylist())


def _cs62_r2() -> dict[str, Any]:
    """Return the assets row of the CS62 R2 NWB file; exactly one must match."""
    found = [
        row
        for row in _rows(
            asteroidea.assets(dataset=DANDI, subject=CS62_SUBJECT, file_format="nwb")
        )
        if CS62_R2_MARK in str(row["s3_key"])
    ]
    if len(found) != 1:
        keys = [row["s3_key"] for row in found]
        msg = f"expected one {CS62_R2_MARK} NWB asset, found {len(found)}: {keys}"
        raise NotFoundError(msg)
    return found[0]


def _resolve(nwb: object, path: str) -> object:
    """Return the object at a `series.path` such as `/processing/ecephys/LFP/X`."""
    parts = [part for part in path.split("/") if part]
    node: Any = getattr(nwb, parts[0])
    for part in parts[1:]:
        node = node[part]
    return node


def step_datasets() -> dict[str, Any]:
    """Step 1: `datasets()` lists both datasets with their titles and licences."""
    started = time.monotonic()
    table = asteroidea.datasets()
    seconds = time.monotonic() - started
    found = {
        str(row["dataset"]): {"title": row["title"], "license": row["license"]}
        for row in _rows(table)
        if row["dataset"] in {DANDI, OPENNEURO}
    }
    return {"seconds": seconds, "n_datasets": table.num_rows, "found": found}


def step_subjects() -> dict[str, Any]:
    """Step 2: patients 41 and 62 each appear in both datasets."""
    started = time.monotonic()
    table = asteroidea.sql(
        "SELECT subject_key, dataset, subject FROM subject_index "
        "WHERE subject_key IN ('bmovie-patient-41', 'bmovie-patient-62')"
    )
    seconds = time.monotonic() - started
    by_key: dict[str, dict[str, list[str]]] = {key: {} for key in PATIENT_KEYS}
    for row in _rows(table):
        by_key[str(row["subject_key"])].setdefault(str(row["dataset"]), []).append(
            str(row["subject"])
        )
    return {"seconds": seconds, "by_key": by_key}


def step_units() -> dict[str, Any]:
    """Step 3: amygdala units per subject and session, through `sql()`."""
    started = time.monotonic()
    table = asteroidea.sql(
        "SELECT subject, bids_session, count(*) AS n FROM units "
        "WHERE lower(location) LIKE '%amygdala%' GROUP BY ALL "
        "ORDER BY subject, bids_session"
    )
    seconds = time.monotonic() - started
    groups = [
        {"subject": r["subject"], "bids_session": r["bids_session"], "n": int(r["n"])}
        for r in _rows(table)
    ]
    per_subject: dict[str, int] = {}
    for group in groups:
        name = str(group["subject"])
        per_subject[name] = per_subject.get(name, 0) + int(group["n"])
    return {"seconds": seconds, "groups": groups, "per_subject": per_subject}


def _scenecuts() -> tuple[pd.DataFrame, str]:
    found = [
        row
        for row in _rows(asteroidea.assets(dataset=DANDI))
        if str(row["s3_key"]).endswith("/" + SCENECUT_PATH)
    ]
    if len(found) != 1:
        msg = f"expected one {SCENECUT_PATH} asset, found {len(found)}"
        raise NotFoundError(msg)
    asset_id = str(found[0]["asset_id"])
    handle = asteroidea.open_file(asset_id)
    return pd.read_csv(io.BytesIO(handle.read())), asset_id


def _series(asset_id: str) -> list[dict[str, Any]]:
    """Return the catalog's `series` rows of one asset (the table is small)."""
    return [
        row
        for row in _rows(
            asteroidea.sql(
                "SELECT asset_id, path, neurodata_type, shape, has_timestamps "
                "FROM series ORDER BY path"
            )
        )
        if row["asset_id"] == asset_id
    ]


def _movie_frames(
    nwb: object, asset_id: str
) -> tuple[str, object, list[str], list[str]]:
    """Find the stimulus series with frame timestamps through `open_nwb`.

    Returns the chosen path, its object, every stimulus path that has timestamps in
    the file, and the same list from the catalog's `series` rows.
    """
    in_file = sorted(
        f"/stimulus/{name}"
        for name, obj in cast("Any", nwb).stimulus.items()
        if getattr(obj, "timestamps", None) is not None
    )
    catalog = sorted(
        str(row["path"])
        for row in _series(asset_id)
        if str(row["path"]).startswith("/stimulus/") and row["has_timestamps"]
    )
    if len(in_file) != 1:
        msg = f"expected one stimulus series with timestamps, found {in_file}"
        raise NotFoundError(msg)
    return in_file[0], _resolve(nwb, in_file[0]), in_file, catalog


def _rates(spikes: object, cuts: np.ndarray) -> tuple[float, float]:
    """Mean rate (Hz) in [-1, 0) s and [0, 1) s around every cut."""
    times = np.sort(np.asarray(spikes, dtype=float))
    lo = np.searchsorted(times, cuts - WINDOW_S, side="left")
    mid = np.searchsorted(times, cuts, side="left")
    hi = np.searchsorted(times, cuts + WINDOW_S, side="left")
    span = len(cuts) * WINDOW_S
    return float((mid - lo).sum() / span), float((hi - mid).sum() / span)


def step_scenecuts() -> dict[str, Any]:
    """Step 4: firing rates of the amygdala units around the movie scene cuts.

    `shot_start_fr` (1-based) maps to seconds through the movie-frame timestamps,
    `timestamps[shot_start_fr - 1]`, as the authors' QC code does
    (`examine_neurons_scenecuts.py`). A frame outside the timestamps is reported,
    not clamped.
    """
    cuts_df, csv_id = _scenecuts()
    row = _cs62_r2()
    asset_id = str(row["asset_id"])
    started = time.monotonic()
    nwb = cast("Any", asteroidea.open_nwb(asset_id))
    path, frames, in_file, catalog = _movie_frames(nwb, asset_id)
    stamps = np.asarray(cast("Any", frames).timestamps[:], dtype=float)
    shot = cuts_df["shot_start_fr"].to_numpy(dtype=int)
    outside = int(((shot < 1) | (shot > len(stamps))).sum())
    out: dict[str, Any] = {
        "csv_asset_id": csv_id,
        "nwb_asset_id": asset_id,
        "nwb_key": row["s3_key"],
        "stimulus_path": path,
        "stimulus_paths_in_file": in_file,
        "stimulus_paths_in_catalog": catalog,
        "n_cuts": len(cuts_df),
        "n_timestamps": len(stamps),
        "frame_min": int(shot.min()),
        "frame_max": int(shot.max()),
        "frames_outside": outside,
        "units": [],
    }
    if outside == 0:
        cut_s = stamps[shot - 1]
        picked = [
            unit
            for unit in _rows(
                asteroidea.sql(
                    "SELECT asset_id, unit_index, unit_id, location FROM units "
                    "WHERE lower(location) LIKE '%amygdala%' ORDER BY unit_index"
                )
            )
            if unit["asset_id"] == asset_id
        ]
        units_df = nwb.units.to_dataframe()
        for unit in picked:
            spikes = units_df.iloc[int(unit["unit_index"])]["spike_times"]
            pre, post = _rates(spikes, cut_s)
            out["units"].append(
                {
                    "unit_index": int(unit["unit_index"]),
                    "unit_id": unit["unit_id"],
                    "location": unit["location"],
                    "n_spikes": len(spikes),
                    "pre_hz": pre,
                    "post_hz": post,
                }
            )
        out["n_units_in_file"] = len(units_df)
        out["first_cut_s"] = float(cut_s[0])
        out["last_cut_s"] = float(cut_s[-1])
    out["seconds"] = time.monotonic() - started
    out["fetched"] = int(nwb.remote_file.bytes_fetched)
    return out


def step_lfp() -> dict[str, Any]:
    """Step 5: one LFP slice `data[0:1000, 0:8]` at a `series` row's path."""
    row = _cs62_r2()
    asset_id = str(row["asset_id"])
    candidates = [
        row
        for row in _series(asset_id)
        if row["neurodata_type"] == "ElectricalSeries"
        and "lfp" in str(row["path"]).lower()
    ]
    if not candidates:
        msg = f"no ElectricalSeries row under an LFP path for asset {asset_id}"
        raise NotFoundError(msg)
    path = str(candidates[0]["path"])
    started = time.monotonic()
    nwb = cast("Any", asteroidea.open_nwb(asset_id))
    series: Any = _resolve(nwb, path)
    block = np.asarray(series.data[0:LFP_ROWS, 0:LFP_COLS])
    return {
        "nwb_asset_id": asset_id,
        "paths": [str(c["path"]) for c in candidates],
        "path": path,
        "series_shape": list(candidates[0]["shape"] or []),
        "shape": list(block.shape),
        "finite": bool(np.isfinite(block).all()),
        "seconds": time.monotonic() - started,
        "fetched": int(nwb.remote_file.bytes_fetched),
    }


def step_fmri() -> dict[str, Any]:
    """Step 6: volume 0 of the p62cs bold run 1, read lazily from the .nii.gz."""
    found = [
        row
        for row in _rows(
            asteroidea.assets(
                dataset=OPENNEURO, subject=FMRI_SUBJECT, file_format="nifti"
            )
        )
        if row["suffix"] == "bold" and FMRI_RUN_MARK in str(row["s3_key"])
    ]
    if len(found) != 1:
        keys = [row["s3_key"] for row in found]
        msg = f"expected one bold run-1 asset for {FMRI_SUBJECT}, found {keys}"
        raise NotFoundError(msg)
    row = found[0]
    started = time.monotonic()
    image = cast("Any", asteroidea.open_image(str(row["asset_id"])))
    volume = np.asarray(image.dataobj[..., 0])
    return {
        "asset_id": row["asset_id"],
        "s3_key": row["s3_key"],
        "size_bytes": int(row["size_bytes"]),
        "nifti_shape": list(row["nifti_shape"] or []),
        "shape": list(volume.shape),
        "seconds": time.monotonic() - started,
        "fetched": int(image.extra["remote_file"].bytes_fetched),
    }


STEPS: Final = {
    "datasets": step_datasets,
    "subjects": step_subjects,
    "units": step_units,
    "scenecuts": step_scenecuts,
    "lfp": step_lfp,
    "fmri": step_fmri,
}


def main(argv: list[str]) -> int:
    """Run the named step and print its JSON line."""
    try:
        out = STEPS[argv[1]]()
    except (asteroidea.AsteroideaError, NotFoundError) as err:
        sys.stdout.write(json.dumps({"error": f"{type(err).__name__}: {err}"}) + "\n")
        return 1
    sys.stdout.write(json.dumps(out, default=str) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
