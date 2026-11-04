"""Live tests: the compute route, precision and latency.

Run with `make live T=tests/live/test_rf_compute.py` after the deploy
(`make deploy SANDBOX_TEST=1`) with `make reextract DATASET=dandi-000623` done.
Collected only under `make live` (`ASTEROIDEA_LIVE=1`); run by hand only.

Stand-in: sign-in is the headless `sandbox-test` client (the conftest
`auth` fixture). Nothing is written to the sandbox except the latency file
`~/asteroidea-live/rf-compute-<sha>.json`, where `<sha>` is
`git rev-parse HEAD` of this checkout: deploy that head before the run, because
`make rf-budget` accepts only the file whose sha equals the current head.

The `align=intervals:trials` call runs the `split`/`UNNEST` units parameter on live
Athena for the first time; a failure there is a STOP for the query text, not a retry.
"""

import json
import statistics
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from asteroidea import _nwb, _sql
from asteroidea._http import send
from asteroidea_infra.runner import execute
from asteroidea_ingest import compute_api
from asteroidea_ingest.athena_query import AthenaRunner

if TYPE_CHECKING:
    import boto3

    from asteroidea._auth import Auth

DANDI: Final = "dandi-000623"
CS62_R2: Final = "1d50b507-a8a1-58d5-83a6-199f232a1582"
TRIALS: Final = 41
SCENECUT_PATH: Final = "stimuli/scenecut_info.csv"
FRAME_COLUMN: Final = "shot_start_fr"
SCENECUT_ROWS: Final = 93  # analysis.py: scenecut_info.csv has 93 rows
WARM_CALLS: Final = 10
OK: Final = 200
SPIKES_CHECKED: Final = 1000
# Largest spike time is about 760 s; the f64 ulp there is about 1.1e-13 s, so 1e-9 s
# is well over 10 ulp (Athena truncates timestamp columns at milliseconds,
# so spike times are list<double>).
TIME_TOLERANCE_S: Final = 1e-9
POLL_BUDGET_S: Final = 60.0
WORKGROUP: Final = "asteroidea"
DATABASE: Final = "asteroidea"
RATES: Final = "pre_s=1&post_s=2&bin_s=0.05"
OUT_DIR: Final = Path.home() / "asteroidea-live"


def _get(auth: Auth, path: str) -> tuple[int, dict[str, Any], float]:
    """GET ``path``; return the status, the JSON body and the wall seconds."""
    headers = {"Authorization": f"Bearer {auth.tokens().access_token}"}
    started = time.perf_counter()
    reply = send("GET", f"{auth.config.api_url.rstrip('/')}{path}", headers)
    seconds = time.perf_counter() - started
    return reply.status, json.loads(reply.body) if reply.body else {}, seconds


def _head_sha() -> str:
    repo = Path(__file__).resolve().parents[2]
    done = execute(["git", "-C", str(repo), "rev-parse", "HEAD"], cwd=None)
    assert done.code == 0, done.err
    return done.out.strip()


def _unit_indexes(auth: Auth, count: int) -> list[int]:
    rows = _sql.sql(
        auth,
        "SELECT unit_index FROM units WHERE asset_id = ? ORDER BY unit_index",
        [CS62_R2],
    ).to_pylist()
    return [int(r["unit_index"]) for r in rows[:count]]


def test_athena_spike_times_equal_the_files_within_1e9_seconds(
    auth: Auth, admin_session: boto3.Session
) -> None:
    """The rates route's Athena path keeps spike-time precision."""
    units = _unit_indexes(auth, 12)
    assert units, "no units for CS62 R2: run `make reextract DATASET=dandi-000623`"
    runner = AthenaRunner(
        athena=admin_session.client("athena"), workgroup=WORKGROUP, database=DATABASE
    )
    rows = runner.run(
        compute_api.SPIKES_SQL,
        [CS62_R2, ",".join(str(u) for u in units)],
        time.monotonic() + POLL_BUDGET_S,
    )
    assert rows, "no unit_spikes rows: run `make reextract DATASET=dandi-000623`"
    nwbfile: Any = _nwb.open_nwb(auth, CS62_R2)
    checked = 0
    worst = 0.0
    for row in sorted(rows, key=lambda r: int(str(r["unit_index"]))):
        unit = int(str(row["unit_index"]))
        from_catalog = compute_api.parse_spikes(row["spike_times"])
        from_file = sorted(float(t) for t in nwbfile.units.get_unit_spike_times(unit))
        assert len(from_file) == len(from_catalog), (unit, len(from_file))
        for a, b in zip(from_file, from_catalog.tolist(), strict=True):
            worst = max(worst, abs(a - b))
        checked += len(from_file)
        if checked >= SPIKES_CHECKED:
            break
    assert checked >= SPIKES_CHECKED, f"only {checked} spike times compared"
    assert worst <= TIME_TOLERANCE_S, (
        f"STOP, Athena path loses precision; plan fallback is integer nanoseconds "
        f"(list<long>): max abs difference {worst!r} s over {checked} times"
    )


def test_rates_aligned_to_the_41_trials(auth: Auth) -> None:
    """Runs the `split`/`UNNEST` units parameter on live Athena (untested)."""
    units = ",".join(str(u) for u in _unit_indexes(auth, 3))
    status, body, _ = _get(
        auth,
        f"/compute/rates?asset_id={CS62_R2}&units={units}&align=intervals:trials&{RATES}",
    )
    assert status == OK, body
    assert body["n_trials"] == TRIALS, body["n_trials"]
    assert body["source"] in {"catalog", "file"}
    assert len(body["units"]) == len(units.split(","))
    assert isinstance(body["compute_ms"], int | float)


def test_rates_aligned_to_trials_with_a_single_unit(auth: Auth) -> None:
    """One unit makes the list `0`, which Athena would type as an integer."""
    status, body, _ = _get(
        auth,
        f"/compute/rates?asset_id={CS62_R2}&units=0&align=intervals:trials&{RATES}",
    )
    assert status == OK, body
    assert body["n_trials"] == TRIALS, body["n_trials"]
    assert [u["unit_index"] for u in body["units"]] == [0]


def _scenecut_call(auth: Auth) -> str:
    """Return the `/compute/rates` path for the scene-cut alignment of CS62 R2."""
    csv = _sql.sql(
        auth,
        "SELECT asset_id FROM assets WHERE dataset = ? AND s3_key LIKE ?",
        [DANDI, f"%/{SCENECUT_PATH}"],
    ).to_pylist()
    assert len(csv) == 1, f"expected one {SCENECUT_PATH} asset, found {len(csv)}"
    series = _sql.sql(
        auth,
        "SELECT path FROM series WHERE asset_id = ? AND has_timestamps "
        "AND lower(path) LIKE '%movie%'",
        [CS62_R2],
    ).to_pylist()
    assert len(series) == 1, f"expected one movie-frame timestamps series: {series}"
    unit = _unit_indexes(auth, 1)[0]
    align = f"frames:{csv[0]['asset_id']}:{FRAME_COLUMN}:{series[0]['path']}"
    return f"/compute/rates?asset_id={CS62_R2}&units={unit}&align={align}&{RATES}"


def test_scene_cut_rates_and_latency(auth: Auth) -> None:
    """One `frames:` call, then 10 warm calls; write the latency file."""
    path = _scenecut_call(auth)
    status, body, first_s = _get(auth, path)
    assert status == OK, body
    missing = body.get("missing_frames") or []
    assert body["n_trials"] + len(missing) == SCENECUT_ROWS, (body["n_trials"], missing)
    warm: list[float] = []
    for _ in range(WARM_CALLS):
        status, body, seconds = _get(auth, path)
        assert status == OK, body
        warm.append(seconds)
    sha = _head_sha()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    record = {
        "sha": sha,
        "route": "rates",
        "align": "frames",
        "first_call_s": first_s,
        "warm_s": warm,
        "warm_p50_s": statistics.median(warm),
        "warm_max_s": max(warm),
    }
    (OUT_DIR / f"rf-compute-{sha}.json").write_text(json.dumps(record, indent=2))
