"""Live tests: spike times, events and dataset depth.

Run with `make live T=tests/live/test_rf_catalog.py` after the deploy,
`make catalog-init` and `make reextract DATASET=dandi-000623`.
Collected only under `make live` (`ASTEROIDEA_LIVE=1`); run by hand only.

Stand-in: sign-in is the headless `sandbox-test` client (the conftest
`auth` fixture). Nothing is written to the sandbox.
"""

import json
from typing import TYPE_CHECKING, Any, Final

from asteroidea import _nwb, _sql
from asteroidea._http import send

if TYPE_CHECKING:
    from asteroidea._auth import Auth

DANDI: Final = "dandi-000623"
CS62_R2: Final = "1d50b507-a8a1-58d5-83a6-199f232a1582"
TRIALS: Final = 41
SPIKES_CHECKED: Final = 1000
# Largest spike time is about 760 s; the f64 ulp there is about 1.1e-13 s, so 1e-9 s
# is above 10 ulp (the catalog stores the file's doubles; Athena prints them in text).
TIME_TOLERANCE_S: Final = 1e-9


def _get(auth: Auth, path: str) -> tuple[int, dict[str, Any]]:
    headers = {"Authorization": f"Bearer {auth.tokens().access_token}"}
    reply = send("GET", f"{auth.config.api_url.rstrip('/')}{path}", headers)
    return reply.status, json.loads(reply.body) if reply.body else {}


def test_unit_spikes_counts_match_units(auth: Auth) -> None:
    spikes = _sql.sql(
        auth,
        "SELECT unit_index, spike_count FROM unit_spikes WHERE asset_id = ? "
        "ORDER BY unit_index",
        [CS62_R2],
    ).to_pylist()
    units = _sql.sql(
        auth,
        "SELECT unit_index, spike_count FROM units WHERE asset_id = ? "
        "ORDER BY unit_index",
        [CS62_R2],
    ).to_pylist()
    assert spikes, "no unit_spikes rows: run `make reextract DATASET=dandi-000623`"
    assert spikes == units


def test_catalog_spike_times_equal_the_files(auth: Auth) -> None:
    catalog = _sql.sql(
        auth,
        "SELECT unit_index, spike_times FROM unit_spikes WHERE asset_id = ? "
        "ORDER BY unit_index",
        [CS62_R2],
    ).to_pylist()
    nwbfile: Any = _nwb.open_nwb(auth, CS62_R2)
    checked = 0
    for row in catalog:
        if checked >= SPIKES_CHECKED:
            break
        from_file = [
            float(t) for t in nwbfile.units.get_unit_spike_times(row["unit_index"])
        ]
        from_catalog = [float(t) for t in row["spike_times"]]
        assert len(from_file) == len(from_catalog), row["unit_index"]
        for a, b in zip(from_file, from_catalog, strict=True):
            assert abs(a - b) <= TIME_TOLERANCE_S, (row["unit_index"], a, b)
        checked += len(from_file)
    assert checked >= SPIKES_CHECKED, f"only {checked} spike times in the catalog"


def test_events_route_returns_the_41_trials(auth: Auth) -> None:
    status, body = _get(auth, f"/catalog/assets/{CS62_R2}/events")
    assert status == 200, body
    trials = [r for r in body["intervals"] if r["table_name"] == "trials"]
    assert len(trials) == TRIALS, [r["table_name"] for r in body["intervals"]]
    assert isinstance(body["query_ms"], int)


def test_dataset_route_lists_both_subjects(auth: Auth) -> None:
    status, body = _get(auth, f"/catalog/datasets/{DANDI}")
    assert status == 200, body
    subjects = {row["subject"] for row in body["subjects"]}
    assert {"sub-CS41", "sub-CS62"} <= subjects, subjects
    assert any(f["format"] == "nwb" for f in body["formats"]), body["formats"]
    assert body["sessions"]
