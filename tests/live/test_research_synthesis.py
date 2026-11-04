"""Live test: rows A2e, A3e, A7 and A8.

Run with `make live T=tests/live/test_research_synthesis.py` after
`make deploy SANDBOX_TEST=1` and `make reextract DATASET=dandi-000623`
done. Collected only under `make live` (`ASTEROIDEA_LIVE=1`, `tests/live/conftest.py`
`collect_ignore_glob`); run by hand only.

Two researchers, both made by the conftest fixtures (the `test_rf_labels.py`
pattern): R1 is `auth`, the author, and R2 is `researcher_b`, the reader, created
for this module and deleted at its end. Stand-in: both sign in through
the headless `sandbox-test` client. The rows run in file order in one process and
each starts from the previous row's end; a row whose start state
is missing fails naming the row it needs.

- A2e: as R1, `/compute/rates?units=all` per event type of the recipe, then
  `load_session(...).rates(...)`; the route's rates equal the SDK's within rtol 1e-9
  after the route's 6-significant-digit rounding.
- A3e: as R1, `/compute/tuning`, then `Session.tuning`; SI, p and q equal.
- A7: R1 saves result X with `run()`; R2 lists All, opens and downloads X, then
  PATCHes and DELETEs X (403 `not_author` each).
- A8: R1 saves Y whose only input is X; R1 DELETEs X (409 `has_dependents` naming
  Y); R1 withdraws X; X shows WITHDRAWN. The test writes `{"x": X, "y": Y}` to the
  result id file that `make web-smoke RS_RESULT_FILE=<file>` reads for A9:
  `$RS_RESULT_FILE` when set, else `~/asteroidea-live/rs-results.json`.

Left behind: X, Y and R1 (A9 reads X and Y); R2 is deleted.

Timings: no row asserts a time. Each row stores its STATE entries and records phase
walls in `PHASES` (A2e and A3e: `load_session`, `Session.rates` or `Session.tuning`
and the route, per event type; A7 and A8: each HTTP call). `test_zz_budgets`, last,
writes `timings.json` beside the result id file, prints one line per phase and asserts
each row's total against its budget (all estimates), so an over-run fails only that test
and never breaks the chain.

Every expected value below that is not fixed elsewhere is marked "estimate": it holds
until a run confirms it.
"""

import base64
import contextlib
import json
import os
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import urlencode

import pytest

import asteroidea
from asteroidea import _artifacts, _image, _nwb, _run, _session, _sql
from asteroidea._client import PlatformClient
from asteroidea._http import send
from asteroidea.errors import HasDependentsError, NotTheAuthorError

if TYPE_CHECKING:
    from collections.abc import Generator

    from asteroidea._auth import Auth

DANDI: Final = "dandi-000623"
SUBJECT: Final = "sub-CS62"
SESSION: Final = "ses-P62CSR2"
CS62_R2: Final = "1d50b507-a8a1-58d5-83a6-199f232a1582"  # test_rf_compute.py
# Expected values. Each is an estimate until a run confirms it; the source is named.
UNITS: Final = 44  # estimate
TUNING: Final = (  # estimate (familiarity: the table or the 400)
    ("scene_cut", "cut_kind"),
    ("recognition_onset", "familiarity"),
)
SIG_DIGITS: Final = 6  # estimate: compute_api.SIG_DIGITS, the route's rounding
RTOL: Final = 1e-9  # estimate
OK: Final = 200
BAD_REQUEST: Final = 400
TOO_FEW: Final = "too_few_events_per_level"  # estimate: compute_api `_too_few_events`
A2E_BUDGET_S: Final = 30.0  # estimate; a run took 91.4 s
A3E_BUDGET_S: Final = 30.0  # estimate
OP_BUDGET_S: Final = 10.0  # estimate, per op
SAVE_BUDGET_S: Final = 40.0  # estimate: A7's save includes load_session and rates
# Rows: (label, phase-name prefix, budget in s, "sum" of the row or "each" phase).
BUDGETS: Final = (
    ("A2e", "A2e.", A2E_BUDGET_S, "sum"),
    ("A3e", "A3e.", A3E_BUDGET_S, "sum"),
    ("A7 save", "A7.save", SAVE_BUDGET_S, "each"),
    ("A7 op", "A7.", OP_BUDGET_S, "each"),
    ("A8 op", "A8.", OP_BUDGET_S, "each"),
)
LIST_WITHIN_S: Final = 30.0  # estimate: by-time is written at finalize, read at once
OUT_DIR: Final = Path.home() / "asteroidea-live"
DEFAULT_RESULT_FILE: Final = OUT_DIR / "rs-results.json"

# Each row's end state, read by the next row.
STATE: dict[str, Any] = {}
PHASES: dict[str, float] = {}  # phase name -> wall seconds, filled by `_phase`


def _claim(auth: Auth, name: str) -> str:
    """Return one claim of the signed-in ID token."""
    payload = auth.tokens().id_token.split(".")[1]
    padded = payload + "=" * (-len(payload) % 4)
    return str(json.loads(base64.urlsafe_b64decode(padded))[name])


def _get(auth: Auth, route: str, query: dict[str, str]) -> tuple[int, dict[str, Any]]:
    """GET a compute route with the access token; return the status and JSON."""
    headers = {"Authorization": f"Bearer {auth.tokens().access_token}"}
    url = f"{auth.config.api_url.rstrip('/')}{route}?{urlencode(query)}"
    reply = send("GET", url, headers)
    return reply.status, json.loads(reply.body) if reply.body else {}


def _api(auth: Auth) -> PlatformClient:
    return PlatformClient(auth.config.api_url, auth.tokens().access_token, send)


def _round(value: float) -> float:
    return float(f"{value:.{SIG_DIGITS}g}")


@contextlib.contextmanager
def _as(auth: Auth) -> Generator[None]:
    """Make `auth` the saved sign-in of the public SDK functions for a block."""
    with pytest.MonkeyPatch.context() as patch:
        for module in (asteroidea, _run, _artifacts, _session):
            patch.setattr(module, "default_auth", lambda _path=None: auth)
        yield


def _backend(auth: Auth) -> _session.PlatformBackend:
    return _session.PlatformBackend(
        sql_run=lambda query, params: _sql.sql(auth, query, list(params)),
        file_open=lambda asset_id: _image.open_asset(auth, asset_id),
        nwb_open=lambda asset_id: _nwb.open_nwb(auth, asset_id),
    )


@contextlib.contextmanager
def _phase(name: str) -> Generator[None]:
    """Record the wall of a block under `name` in `PHASES`; never asserts."""
    started = time.perf_counter()
    try:
        yield
    finally:
        PHASES[name] = PHASES.get(name, 0.0) + time.perf_counter() - started


def _result_file() -> Path:
    return Path(os.environ.get("RS_RESULT_FILE") or DEFAULT_RESULT_FILE)


def _over_budget() -> list[str]:
    """Return one message per row or phase over its estimated budget."""
    over: list[str] = []
    for label, prefix, budget, mode in BUDGETS:
        rows = {n: s for n, s in PHASES.items() if n.startswith(prefix)}
        if label == "A7 op":
            rows.pop("A7.save", None)
        if mode == "sum":
            total = sum(rows.values())
            if rows and total > budget:
                over.append(f"{label} took {total:.1f} s, over {budget} s")
        else:
            over.extend(
                f"{name} took {seconds:.1f} s, over {budget} s"
                for name, seconds in rows.items()
                if seconds > budget
            )
    return over


def _need(key: str, row: str) -> Any:
    assert key in STATE, (
        f"start state missing: run row {row} first (the rows run in order)"
    )
    return STATE[key]


def test_a2e_route_rates_equal_sdk_rates(auth: Auth) -> None:
    """A2e: per event type, `/compute/rates?units=all` equals `Session.rates`."""
    with _phase("A2e.load_session"):
        session = _session.load_session(DANDI, SUBJECT, SESSION, _backend(auth))
    assert session.units.height == UNITS  # estimate
    for spec in session.recipe.event_types:
        with _phase(f"A2e.rates.{spec.name}"):
            frame = session.rates(spec.name)
        pre, post, step = (float(frame[c][0]) for c in ("pre_s", "post_s", "bin_s"))
        with _phase(f"A2e.route.{spec.name}"):
            status, body = _get(
                auth,
                "/compute/rates",
                {
                    "asset_id": CS62_R2,
                    "units": "all",
                    "align": f"event:{spec.name}",
                    "pre_s": str(pre),
                    "post_s": str(post),
                    "bin_s": str(step),
                    "raster": "0",
                },
            )
        assert status == OK, (spec.name, body)
        route = {int(u["unit_index"]): u["rate_hz"] for u in body["units"]}
        sdk = {
            int(unit): [_round(v) for v in group["rate_hz"].to_list()]
            for (unit,), group in frame.sort("bin_start_s").group_by(
                "unit_index",
                maintain_order=True,
            )
        }
        assert set(route) == set(sdk), spec.name
        for unit, rates in sdk.items():
            assert route[unit] == pytest.approx(rates, rel=RTOL), (spec.name, unit)
    STATE["session"] = session


def test_a3e_route_tuning_equals_sdk_tuning(auth: Auth) -> None:
    """A3e: `/compute/tuning` SI, p and q equal `Session.tuning` (same seed)."""
    session = _need("session", "A2e")
    assert isinstance(session, _session.Session)
    for event, attribute in TUNING:
        with _phase(f"A3e.route.{event}.{attribute}"):
            status, body = _get(
                auth,
                "/compute/tuning",
                {
                    "asset_id": CS62_R2,
                    "event": event,
                    "attribute": attribute,
                    "units": "all",
                },
            )
        if status == BAD_REQUEST:
            # Familiarity gives the table or the 400, whichever the level
            # counts predict (estimate). The SDK must skip the same screen.
            assert body.get("error") == TOO_FEW, (event, attribute, body)  # estimate
            with (
                _phase(f"A3e.tuning.{event}.{attribute}"),
                pytest.raises(ValueError, match="skipped"),
            ):
                session.tuning(event, attribute)
            continue
        assert status == OK, (event, attribute, body)
        with _phase(f"A3e.tuning.{event}.{attribute}"):
            frame = session.tuning(event, attribute)
        sdk = {int(r["unit_index"]): r for r in frame.iter_rows(named=True)}
        route = {int(u["unit_index"]): u for u in body["units"]}
        assert set(route) == set(sdk), (event, attribute)
        for unit, row in sdk.items():
            got = route[unit]
            expected_si = _round(row["selectivity_index"])
            assert got["selectivity_index"] == pytest.approx(expected_si, rel=RTOL)
            # estimate: p and q are not rounded by the route, so they compare raw.
            assert got["p"] == pytest.approx(row["p"], rel=RTOL), (event, unit)
            assert got["q"] == pytest.approx(row["q"], rel=RTOL), (event, unit)
    STATE["a3e_done"] = True


def test_a7_another_researcher_reads_x_but_cannot_change_it(
    auth: Auth,
    researcher_b: Auth,
    tmp_path: Path,
) -> None:
    """A7: R1 saves X with `run()`; R2 lists, opens, downloads; PATCH, DELETE 403."""
    _need("a3e_done", "A3e")
    r1_email = _claim(auth, "email")
    data_file = tmp_path / "rs-a7-rates.csv"
    with _as(auth), _phase("A7.save"):
        with asteroidea.run("rs-live-a7", {"event_type": "scene_cut"}) as r:
            session = asteroidea.load_session(DANDI, SUBJECT, SESSION)
            asteroidea.open_file(CS62_R2)  # A9: the file page lists X
            session.rates("scene_cut").write_csv(data_file)
        saved = r.save_artifact(data_file, note="RS live A7")
    x = saved.artifact_id
    kinds = {i["kind"] for i in r.record()["inputs"]}
    assert {"table_snapshot", "asset", "defaults", "recipe"} <= kinds  # estimate
    with _as(researcher_b):
        deadline = time.monotonic() + LIST_WITHIN_S
        with _phase("A7.list_all"):
            rows = asteroidea.artifacts("all")
            while (
                x not in [a.artifact_id for a in rows] and time.monotonic() < deadline
            ):
                time.sleep(1)
                rows = asteroidea.artifacts("all")
        (row,) = [a for a in rows if a.artifact_id == x]
        assert row.author_email == r1_email  # estimate
        assert row.state == "COMMITTED"  # estimate
        with _phase("A7.open"):
            opened = asteroidea.open_artifact(x)
        assert opened.author_email == r1_email  # estimate
        assert opened.record["schema"] == "asteroidea.provenance/2"
        with _phase("A7.download"):
            target = asteroidea.download_artifact(x, tmp_path / "downloaded.csv")
        assert target.read_bytes() == data_file.read_bytes()  # estimate
        with _phase("A7.patch"), pytest.raises(NotTheAuthorError):
            _api(researcher_b).call("PATCH", f"/artifacts/{x}", {"note": "R2 edit"})
        with _phase("A7.delete"), pytest.raises(NotTheAuthorError):
            _api(researcher_b).call("DELETE", f"/artifacts/{x}")
        with pytest.raises(NotTheAuthorError):
            asteroidea.withdraw_artifact(x)
    STATE["x"] = x


def test_a8_a_result_made_from_x_blocks_its_delete_and_x_is_withdrawn(
    auth: Auth,
    tmp_path: Path,
) -> None:
    """A8: Y's only input is X; DELETE X is 409 naming Y; withdraw X succeeds."""
    x = _need("x", "A7")
    note_file = tmp_path / "rs-a8-note.txt"
    note_file.write_text(f"made from {x}\n")
    with _as(auth):
        with asteroidea.run("rs-live-a8", {"from": x}) as r:
            asteroidea.open_artifact(x)
        assert r.record()["inputs"] == [{"kind": "artifact", "id": x, "role": "opened"}]
        assert r.record()["sources"] == []
        with _phase("A8.save_y"):
            y = r.save_artifact(note_file, note="RS live A8").artifact_id
        record = asteroidea.open_artifact(y).record
        assert record["schema"] == "asteroidea.provenance/2"  # estimate
        assert record["inputs"] == [{"kind": "artifact", "id": x, "role": "opened"}]
        with _phase("A8.delete_x"), pytest.raises(HasDependentsError, match=y):
            _api(auth).call("DELETE", f"/artifacts/{x}")
        with _phase("A8.withdraw_x"):
            withdrawn = asteroidea.withdraw_artifact(x)
        assert withdrawn == "WITHDRAWN"  # estimate
        assert asteroidea.open_artifact(x).state == "WITHDRAWN"  # estimate
    out = _result_file()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"x": x, "y": y}) + "\n")
    sys.stdout.write(f"result id file: {out} (make web-smoke RS_RESULT_FILE={out})\n")


def test_zz_budgets() -> None:
    """Print each phase, write `timings.json`, assert each row against its budget."""
    lines = [f"{name}: {seconds:.2f} s" for name, seconds in PHASES.items()]
    sys.stdout.write(
        "\n".join(["phase timings (budgets are estimates):", *lines]) + "\n"
    )
    out = _result_file().parent / "timings.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(PHASES, indent=2) + "\n")
    sys.stdout.write(f"timings file: {out}\n")
    over = _over_budget()
    assert not over, "over an estimated budget: " + "; ".join(over)
