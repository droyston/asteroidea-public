"""The live synthesis rows keep their chain when a budget is over (no network).

`tests/live/test_research_synthesis.py` imports without `ASTEROIDEA_LIVE`. Row A2e is
driven here with a fake session whose `rates` takes 100 s on a fake clock: the row
passes and stores its state, the next row finds it, and only `test_zz_budgets` fails,
naming the phase. A row that asserted its time before storing state fails this test.
"""

import importlib.util
import json
import types
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from asteroidea import _session

LIVE = Path(__file__).resolve().parent / "live" / "test_research_synthesis.py"
SLOW_S = 100.0


class _Clock:
    now = 0.0

    def perf_counter(self) -> float:
        return self.now


class _Spec:
    name = "scene_cut"


class _Session:
    units = types.SimpleNamespace(height=44)
    recipe = types.SimpleNamespace(event_types=[_Spec()])

    def __init__(self, clock: _Clock) -> None:
        self.clock = clock

    def rates(self, _name: str) -> pl.DataFrame:
        self.clock.now += SLOW_S
        return pl.DataFrame(
            {
                "unit_index": [0],
                "bin_start_s": [0.0],
                "rate_hz": [1.0],
                "pre_s": [1.0],
                "post_s": [1.0],
                "bin_s": [0.1],
            },
        )


def test_a_slow_row_keeps_its_state_and_only_the_budget_test_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("ASTEROIDEA_LIVE", raising=False)
    monkeypatch.setenv("RS_RESULT_FILE", str(tmp_path / "rs-results.json"))
    spec = importlib.util.spec_from_file_location("_rs_live_chain", LIVE)
    assert spec is not None
    assert spec.loader is not None
    live: Any = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(live)
    clock = _Clock()
    monkeypatch.setattr(live, "time", clock)
    monkeypatch.setattr(live, "_backend", lambda _auth: None)
    monkeypatch.setattr(_session, "load_session", lambda *_a, **_k: _Session(clock))
    monkeypatch.setattr(
        live,
        "_get",
        lambda *_a, **_k: (200, {"units": [{"unit_index": 0, "rate_hz": [1.0]}]}),
    )

    live.test_a2e_route_rates_equal_sdk_rates(object())

    assert live.STATE["session"] is not None  # the chained row's start
    with pytest.raises(AssertionError, match=r"A2e took 100\.0 s, over 30\.0 s"):
        live.test_zz_budgets()
    timings = json.loads((tmp_path / "timings.json").read_text())
    assert timings["A2e.rates.scene_cut"] == SLOW_S
