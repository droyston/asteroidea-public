"""Worker for `make live-first-hour`: one SDK data step inside the fresh venv.

Run as `python first_hour_worker.py <upload|query|slice>` with `ASTEROIDEA_CONFIG_DIR`
and `ASTEROIDEA_ACC_STATE` (a JSON file shared by the steps) in the environment.
It prints one JSON line of measurements on stdout. An SDK error becomes
`{"error": "<Class>: <the SDK's own message>"}` and exit status 1. It uses only the
public SDK calls plus one private helper to regenerate the expected voxels.
"""

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Final, cast

import asteroidea
from asteroidea._sample import sample_array

SLICE_INDEX: Final = 48


def _state_path() -> Path:
    return Path(os.environ["ASTEROIDEA_ACC_STATE"])


def _load() -> dict[str, Any]:
    return dict(json.loads(_state_path().read_text()))


def _save(update: dict[str, Any]) -> None:
    current = _load() if _state_path().exists() else {}
    _state_path().write_text(json.dumps({**current, **update}))


def step_upload() -> dict[str, Any]:
    """Write the sample, upload it, and time `upload()` until the row is visible."""
    dataset, path = asteroidea.write_sample("sample")
    started = time.monotonic()
    result = asteroidea.upload(path, dataset=dataset, sandbox=True)
    seconds = time.monotonic() - started
    _save(
        {
            "dataset": dataset,
            "session_id": result.session_id,
            "asset_id": result.asset_id,
            "snapshot_id": result.snapshot_id,
        }
    )
    return {"seconds": seconds}


def step_query() -> dict[str, Any]:
    """Query, upload a second sample, query again, then query the first snapshot."""
    state = _load()
    started = time.monotonic()
    q1 = asteroidea.query(datatype="anat", subject="01")
    q1_seconds = time.monotonic() - started
    first_seen = state["dataset"] in {row["dataset"] for row in q1.rows}
    dataset2, path2 = asteroidea.write_sample("sample")
    second = asteroidea.upload(path2, dataset=dataset2, sandbox=True)
    _save(
        {
            "dataset2": dataset2,
            "asset_id2": second.asset_id,
            "snapshot_id2": second.snapshot_id,
            "q1_snapshot_id": q1.snapshot_id,
        }
    )
    started = time.monotonic()
    q2 = asteroidea.query(datatype="anat", subject="01")
    q2_seconds = time.monotonic() - started
    q3 = asteroidea.query(snapshot_id=q1.snapshot_id, datatype="anat", subject="01")
    return {
        "q1_seconds": q1_seconds,
        "q2_seconds": q2_seconds,
        "first_seen": first_seen,
        "same_rows": q3.rows == q1.rows,
        "new_visible": dataset2 in {row["dataset"] for row in q2.rows},
        "new_hidden": dataset2 not in {row["dataset"] for row in q3.rows},
    }


def step_slice() -> dict[str, Any]:
    """Read one slice of the first upload lazily and compare it with the sample."""
    state = _load()
    q1 = asteroidea.query(datatype="anat", subject="01")
    ref = next(f for f in q1.files if f.dataset == state["dataset"])
    started = time.monotonic()
    image = cast("Any", asteroidea.open_image(ref.asset_id))
    voxels = image.dataobj[:, :, SLICE_INDEX]
    seconds = time.monotonic() - started
    remote = image.extra["remote_file"]
    expected = cast("Any", sample_array())[:, :, SLICE_INDEX]
    return {
        "seconds": seconds,
        "fetched": int(remote.bytes_fetched),
        "requests": int(remote.requests),
        "same": bool((voxels == expected).all()),
    }


STEPS: Final = {"upload": step_upload, "query": step_query, "slice": step_slice}


def main(argv: list[str]) -> int:
    """Run the named step and print its JSON line."""
    try:
        out = STEPS[argv[1]]()
    except asteroidea.AsteroideaError as err:
        sys.stdout.write(json.dumps({"error": f"{type(err).__name__}: {err}"}) + "\n")
        return 1
    sys.stdout.write(json.dumps(out) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
