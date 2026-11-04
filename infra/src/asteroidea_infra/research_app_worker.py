"""Worker for `make rf-run PASS=2` row R13: the notebook calls, in the fresh venv.

R13: `asteroidea.labels(<id>)` returns the label the
browser made, and `analysis.rates` on `unit_spikes` and `intervals` read through
`sql()` gives the same rates the compute route gave for R4's inputs. This file imports
only `asteroidea`, numpy and the standard library, so it runs in the venv `make
analysis` builds (SDK wheel plus the `analysis` extra's pins).

Run as `python research_app_worker.py <asset_id> <unit_index> <label_name>
<expected.json>` with
`ASTEROIDEA_CONFIG_DIR` in the environment. `expected.json` holds what
`GET /compute/rates` answered for the same inputs: `{unit, n_trials, edges, rate_hz}`.
The route rounds every float to 6 significant digits (`compute.py` `_sig`), so the SDK
result is rounded the same way before the comparison; the comparison is then
`np.allclose(rtol=1e-9, atol=0)`. It prints one JSON line of facts and judges nothing
(`research_app_check.sdk_measured` does). An SDK error becomes
`{"error": "<Class>: <the SDK's own message>"}` and exit status 1.
"""

import json
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, cast

import numpy as np

import asteroidea
from asteroidea import analysis
from asteroidea._auth import default_auth
from asteroidea._http import Reply, send
from asteroidea._labels import labels as read_labels

if TYPE_CHECKING:
    from collections.abc import Mapping

PRE_S: Final = 2.0
POST_S: Final = 2.0
BIN_S: Final = 0.05
SIG_DIGITS: Final = 6
RTOL: Final = 1e-9
ASSET: Final = "1d50b507-a8a1-58d5-83a6-199f232a1582"  # research_app_check.CS62_R2
# Literal queries for that one file: the SDK's `sql()` takes no parameters, and a
# built string would be an injection vector. The first unit is the lowest unit_index.
TRIALS_SQL: Final = (
    "SELECT start_time FROM intervals "
    "WHERE asset_id = '1d50b507-a8a1-58d5-83a6-199f232a1582' "
    "AND table_name = 'trials' ORDER BY row_index"
)
SPIKES_SQL: Final = (
    "SELECT unit_index, spike_times FROM unit_spikes "
    "WHERE asset_id = '1d50b507-a8a1-58d5-83a6-199f232a1582' "
    "AND unit_index = (SELECT min(unit_index) FROM unit_spikes "
    "WHERE asset_id = '1d50b507-a8a1-58d5-83a6-199f232a1582')"
)
ARGC: Final = 5


class InputError(Exception):
    """An argument or a lookup does not hold what the run needs."""


def _sig(values: object) -> np.ndarray:
    """Round as the compute route does: 6 significant digits."""
    flat = np.asarray(values, dtype=np.float64).ravel().tolist()
    return np.asarray([float(f"{v:.{SIG_DIGITS}g}") for v in flat], dtype=np.float64)


def _column(table: object, name: str) -> list[Any]:
    return list(cast("Any", table).column(name).to_pylist())


def _timed_labels(asset_id: str) -> tuple[object, dict[str, float]]:
    """Run what `asteroidea.labels` runs, with the time split by phase.

    `labels_s` spans `default_auth()` to the finished table, the quantity R13 judges.
    `auth_s` is `default_auth()` plus one `tokens()` (config and cache reads, and a
    refresh POST when within 300 s of expiry). `http_s` is the time inside the
    transport (connect, TLS, request, response). `table_s` is the rest: a second
    cache read, the JSON decode and `Table.from_pylist`.
    """
    spent = {"http": 0.0}

    def transport(
        method: str, url: str, headers: Mapping[str, str], body: bytes | Path | None
    ) -> Reply:
        """Send the request, add its wall time to the HTTP total; return the reply."""
        began = time.monotonic()
        try:
            return send(method, url, headers, body)
        finally:
            spent["http"] += time.monotonic() - began

    started = time.monotonic()
    auth = default_auth()
    auth.tokens()
    auth_s = time.monotonic() - started
    table = read_labels(auth, asset_id, transport)
    labels_s = time.monotonic() - started
    return table, {
        "labels_s": labels_s,
        "auth_s": auth_s,
        "http_s": spent["http"],
        "table_s": labels_s - auth_s - spent["http"],
    }


def measure(
    asset_id: str, unit: int, label_name: str, expected: Path
) -> dict[str, Any]:
    """Run the three SDK reads and compare with `expected`; return the facts."""
    if asset_id != ASSET:
        msg = f"this worker reads {ASSET} only, not {asset_id}"
        raise InputError(msg)
    want = json.loads(expected.read_text())
    table, split = _timed_labels(asset_id)
    labels_s = split["labels_s"]
    _, warm = _timed_labels(asset_id)
    rows = cast("Any", table).to_pylist()
    named = [row for row in rows if row["name"] == label_name]

    started = time.monotonic()
    intervals = asteroidea.sql(TRIALS_SQL)
    spikes = asteroidea.sql(SPIKES_SQL)
    sql_s = time.monotonic() - started
    onsets = np.asarray(_column(intervals, "start_time"), dtype=np.float64)
    held = _column(spikes, "spike_times")
    first = _column(spikes, "unit_index")
    if len(held) != 1 or held[0] is None or first != [unit]:
        msg = f"unit_spikes gave unit_index {first} for the first unit {unit}"
        raise InputError(msg)
    times = np.sort(np.asarray(held[0], dtype=np.float64))

    started = time.monotonic()
    fit = analysis.rates({int(unit): times}, onsets, PRE_S, POST_S, BIN_S)
    rates_s = time.monotonic() - started

    mine = _sig(fit.rate_hz[0])
    theirs = np.asarray(want["rate_hz"], dtype=np.float64)
    edges_equal = bool(
        _sig(fit.edges).shape == (len(want["edges"]),)
        and np.allclose(_sig(fit.edges), np.asarray(want["edges"]), rtol=RTOL, atol=0.0)
    )
    same_shape = mine.shape == theirs.shape
    equal = bool(same_shape and np.allclose(mine, theirs, rtol=RTOL, atol=0.0))
    with np.errstate(divide="ignore", invalid="ignore"):
        rel = np.abs(mine - theirs) / np.where(theirs == 0.0, 1.0, np.abs(theirs))
    return {
        "labels_split": {**split, "warm_labels_s": warm["labels_s"]},
        "parts": {"labels": labels_s, "sql": sql_s, "rates": rates_s},
        "n_labels": len(named),
        "label": None
        if not named
        else {
            k: named[0][k] for k in ("label_id", "name", "mine", "start_s", "stop_s")
        },
        "n_onsets": int(onsets.size),
        "n_trials": int(want["n_trials"]),
        "n_spikes": int(times.size),
        "unit": int(unit),
        "bins": int(mine.size),
        "edges_equal": edges_equal,
        "equal": equal and edges_equal,
        "max_rel_diff": float(rel.max()) if same_shape and rel.size else None,
        "rtol": RTOL,
    }


def main(argv: list[str]) -> int:
    """Print the facts as one JSON line."""
    if len(argv) != ARGC:
        sys.stderr.write(
            "usage: research_app_worker.py <asset_id> <unit_index> <label_name> "
            "<expected.json>\n"
        )
        return 2
    try:
        out = measure(argv[1], int(argv[2]), argv[3], Path(argv[4]))
    except (asteroidea.AsteroideaError, InputError) as err:
        sys.stdout.write(json.dumps({"error": f"{type(err).__name__}: {err}"}) + "\n")
        return 1
    sys.stdout.write(json.dumps(out, default=str) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
