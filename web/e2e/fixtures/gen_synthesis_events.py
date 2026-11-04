"""Write synthesis-events.json from the real event resolver.

Runs `analysis.resolve_events` over the SDK's dandi-000623 fixtures and writes the
`GET /compute/events` response shape (`compute_api._event_json`).

Run from the repo root:
`uv run --frozen python web/e2e/fixtures/gen_synthesis_events.py`, then
`(cd web && npx biome format --write e2e/fixtures/synthesis-events.json)`.
`missing_frames` on scene_cut is the two frames the series lacks; the asset id is the
fixture build's `asset-0201`.
"""

import csv
import json
from pathlib import Path
from typing import Any

from asteroidea import analysis

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "sdk" / "tests" / "fixtures" / "dandi-000623"
OUT = Path(__file__).with_name("synthesis-events.json")
CSV_PATH = "stimuli/scenecut_info.csv"
MOVIE = "stimulus/presentation/movieframe_time"
MISSING_SCENE_CUT_FRAMES = [11800, 11801]


def main() -> None:
    """Resolve the fixture file's events and write the fixture."""
    rows = json.loads((SRC / "intervals.json").read_text("utf-8"))
    with (SRC / "scenecut_info.csv").open(encoding="utf-8") as f:
        cuts = list(csv.DictReader(f))
    frames = json.loads((SRC / "movieframe_timestamps.json").read_text("utf-8"))
    sources = analysis.EventSources(
        asset_id=str(rows[0]["asset_id"]),
        intervals=rows,
        csv={CSV_PATH: cuts},
        frames={MOVIE: frames["timestamps"]},
    )
    recipe = analysis.load_recipe("dandi-000623")
    types: dict[str, dict[str, Any]] = {}
    for e in analysis.resolve_events(recipe, sources):
        entry = types.setdefault(
            e.event_type,
            {"name": e.event_type, "averaging": e.averaging, "events": []},
        )
        entry["events"].append(
            {
                "onset_s": e.onset_s,
                "stop_s": e.stop_s,
                "attributes": dict(e.attributes),
                "source": {
                    "kind": e.source.kind,
                    "ref": e.source.ref,
                    "row": e.source.row,
                },
            }
        )
    types["scene_cut"]["missing_frames"] = MISSING_SCENE_CUT_FRAMES
    for entry in types.values():
        entry["n_events"] = len(entry["events"])
    body = {
        "asset_id": "asset-0201",
        "dataset": "dandi-000623",
        "recipe": {
            "version": recipe.version,
            "default": recipe.default,
            "lfp_clock_offset_s": recipe.lfp_clock_offset_s,
        },
        "event_types": [
            types[n] for n in ("scene_cut", "recognition_onset", "movie_onset")
        ],
    }
    OUT.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
