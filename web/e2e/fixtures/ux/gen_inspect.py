"""Generate the flexible-inspection fixtures from the CS62 display body.

Run once from the repository root, format, then commit both outputs:

    uv run --frozen python web/e2e/fixtures/ux/gen_inspect.py
    (cd web && npx biome format --write e2e/fixtures/ux)

Writes, next to this script:

- ``rates-cs62-scene_cut-raster.json``: the ``/compute/rates?...&raster=1`` body for
  CS62 R2 at the display settings. Each unit's raster is synthesised from the display
  body's per-bin counts (rate x trials x bin), with spike times on a 1/128 s grid
  strictly inside their bin and trials assigned by a fixed formula, so the 50 ms
  rates equal the display body's. ``rate_hz`` and ``z`` are recomputed with the SDK.
- ``inspect-parity.golden.json``: per case (bin, baseline window, response window)
  and per unit in ``UNITS``: the SDK's PSTH (``analysis.rates``), its z
  (``analysis.zscore``: sample SD, ddof 1, baseline bins wholly inside the window),
  the change from the baseline mean in Hz and in percent, and the per-unit means.

Every onset is 8 s x trial and every relative spike time a multiple of 1/128 s, so
``onset + t`` and ``analysis.align``'s ``spike - onset`` are exact in float64 and the
SDK bins the same ``t`` the browser re-bins.

The change in Hz is ``rate - mean(baseline bins)``; the percent change is
``100 * that / mean(baseline bins)``, null when the baseline mean is 0. The SDK has
no function for either; these are the definitions the browser implements.
"""

import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np

from asteroidea import analysis

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "rates-cs62-scene_cut-display.json"
RASTER_OUT = HERE / "rates-cs62-scene_cut-raster.json"
GOLDEN_OUT = HERE / "inspect-parity.golden.json"
GRID = 128
ONSET_STEP_S = 8.0
PRE_S, POST_S, BIN_S = 1.0, 2.0, 0.05
UNITS = (0, 1, 2, 3, 4, 5, 43)
CASES = (
    {"bin_s": 0.05, "baseline": [-1.0, 0.0], "response": [0.0, 1.0]},
    {"bin_s": 0.1, "baseline": [-0.5, 0.0], "response": [0.0, 0.5]},
    {"bin_s": 0.2, "baseline": [-1.0, -0.2], "response": [0.2, 1.0]},
)
EDGE_TOL = 1e-9


def _grid_points(b: int) -> list[float]:
    """Return the grid points the SDK bins into 50 ms bin `b`, off its edges."""
    lo = -PRE_S + b * BIN_S
    first = math.floor(lo * GRID) - 1
    last = math.ceil((lo + BIN_S) * GRID) + 1
    points = [k / GRID for k in range(first, last + 1)]
    # Same expression as analysis.rates; one grid step away from either edge.
    return [
        p
        for p in points
        if math.floor((p + PRE_S) / BIN_S) == b
        and math.floor((p - 1 / GRID + PRE_S) / BIN_S) == b
        and math.floor((p + 1 / GRID + PRE_S) / BIN_S) == b
    ]


def _counts(entry: dict[str, Any], n_trials: int) -> list[int]:
    """Return a display unit's 50 ms spike counts.

    A unit the display body marks with no baseline variance (`z` null) gets a flat
    baseline (every bin before 0 at the mean count), so the SDK's z is null for it too.
    """
    counts: list[int] = [round(float(r) * n_trials * BIN_S) for r in entry["rate_hz"]]
    if entry["z"] is None:
        n_base = round(PRE_S / BIN_S)
        flat = round(sum(counts[:n_base]) / n_base)
        counts[:n_base] = [flat] * n_base
    return counts


def _raster(
    unit: int, counts: list[int], n_trials: int
) -> tuple[list[int], list[float]]:
    """Return one unit's raster (trial, t) with `counts` spikes in each 50 ms bin."""
    pairs: list[tuple[int, float]] = []
    for b, count in enumerate(counts):
        points = _grid_points(b)
        for j in range(count):
            trial = (j * 7 + b * 13 + unit * 3) % n_trials
            pairs.append((trial, points[(j * 5 + unit) % len(points)]))
    pairs.sort()
    return [p[0] for p in pairs], [p[1] for p in pairs]


def _inside(edges: np.ndarray, window: list[float]) -> np.ndarray:
    """Return the bins lying wholly inside `window` (analysis.zscore's rule)."""
    return (edges[:-1] >= window[0] - EDGE_TOL) & (edges[1:] <= window[1] + EDGE_TOL)


def _floats(row: np.ndarray) -> list[float]:
    """Return a row as plain floats."""
    return [float(v) for v in row]


def _case(
    case: dict[str, Any], spikes: dict[int, np.ndarray], onsets: np.ndarray
) -> dict[str, Any]:
    """Return one parity case: the SDK's numbers for each unit at its settings."""
    result = analysis.rates(spikes, onsets, PRE_S, POST_S, case["bin_s"])
    base = tuple(case["baseline"])
    z = analysis.zscore(result.rate_hz, result.edges, base)
    inside = _inside(result.edges, case["baseline"])
    response = _inside(result.edges, case["response"])
    units = []
    for row, unit in enumerate(result.units):
        rate = result.rate_hz[row]
        mean = float(rate[inside].mean())
        delta = rate - mean
        pct = None if mean == 0 else 100 * delta / mean
        zrow = z[row]
        units.append(
            {
                "unit_index": int(unit),
                "rate_hz": _floats(rate),
                "z": None if zrow is None else _floats(zrow),
                "delta_hz": _floats(delta),
                "pct": None if pct is None else _floats(pct),
                "baseline_hz": mean,
                "response_hz": float(rate[response].mean()),
                "response_z": None if zrow is None else float(zrow[response].mean()),
                "response_delta_hz": float(delta[response].mean()),
                "response_pct": None if pct is None else float(pct[response].mean()),
            }
        )
    return {**case, "edges": _floats(result.edges), "units": units}


def main() -> None:
    """Write the raster body and the parity golden."""
    body = json.loads(SOURCE.read_text())
    n_trials = int(body["n_trials"])
    onsets = np.arange(n_trials, dtype=np.float64) * ONSET_STEP_S
    spikes: dict[int, np.ndarray] = {}
    out_units: list[dict[str, Any]] = []
    for u in body["units"]:
        unit = int(u["unit_index"])
        trial, t = _raster(unit, _counts(u, n_trials), n_trials)
        trials = np.asarray(trial, dtype=np.int64)
        spikes[unit] = np.sort(onsets[trials] + np.asarray(t))
        out_units.append({"unit_index": unit, "raster": {"trial": trial, "t": t}})
    display = analysis.rates(spikes, onsets, PRE_S, POST_S, BIN_S)
    z = analysis.zscore(display.rate_hz, display.edges)
    for row, entry in enumerate(out_units):
        # The display body's rates carry a few significant digits: compare spike counts.
        scale = n_trials * BIN_S
        expected = np.asarray(_counts(body["units"][row], n_trials))
        if not np.array_equal(np.rint(display.rate_hz[row] * scale), expected):
            msg = f"unit {entry['unit_index']}: the raster does not rebuild the rates"
            raise SystemExit(msg)
        entry["rate_hz"] = _floats(np.round(display.rate_hz[row], 6))
        zrow = z[row]
        entry["z"] = None if zrow is None else _floats(np.round(zrow, 6))
    raster_body = {**body, "units": out_units}
    RASTER_OUT.write_text(json.dumps(raster_body, separators=(",", ":")) + "\n")
    subset = {u: spikes[u] for u in UNITS}
    golden = {
        "source": RASTER_OUT.name,
        "pre_s": PRE_S,
        "post_s": POST_S,
        "n_trials": n_trials,
        "cases": [_case(c, subset, onsets) for c in CASES],
    }
    GOLDEN_OUT.write_text(json.dumps(golden, indent=1) + "\n")
    for out in (RASTER_OUT, GOLDEN_OUT):
        sys.stdout.write(f"{out.name}: {out.stat().st_size} bytes\n")


if __name__ == "__main__":
    main()
