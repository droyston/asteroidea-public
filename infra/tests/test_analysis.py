"""`make analysis`: the worker against a fake SDK, the judges, the runner.

No AWS and no network: a fake `asteroidea` module stands in for the SDK, and the
runner's `execute` is replaced by an in-process call of the worker.
"""

import ast
import contextlib
import importlib.util
import io
import json
import sys
import types
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

import numpy as np
import pandas as pd
import pytest

from asteroidea_infra import analysis_check as analysis
from asteroidea_infra import notebook
from asteroidea_infra.runner import Proc
from asteroidea_infra.steps import Console, StepResult, suggest_email

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

WORKER_PATH: Final = analysis.WORKER
ALLOWED: Final = frozenset({"asteroidea", "numpy", "pandas"})
# pynapple: `Session.to_pynapple()` in section 9
NOTEBOOK_EXTRA: Final = frozenset({"IPython", "matplotlib", "polars", "pynapple"})
NOTEBOOK_PATH: Final = notebook.NOTEBOOK
NBFORMAT: Final = 4
CS62_R2: Final = "nwb-cs62-r2"
CSV_ID: Final = "csv-cuts"
BOLD_ID: Final = "bold-run-1"
FPS: Final = 25.0
N_FRAMES: Final = 12000


def imported_roots(source: str) -> list[str]:
    """Return every module name the source imports (dotted names kept whole)."""
    names: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.append("." * node.level + (node.module or ""))
    return names


def forbidden_imports(
    source: str, extra_roots: frozenset[str] = frozenset()
) -> list[str]:
    """Return the imports the gate forbids: anything but the allowed roots and stdlib.

    Allowed: `asteroidea` as the package itself (its public calls), never a private
    submodule; numpy; pandas; `extra_roots` (the notebook adds matplotlib and polars);
    the standard library. Dynamic imports are forbidden outright.
    """
    bad = []
    for name in imported_roots(source):
        root = name.split(".", 1)[0]
        if name in ALLOWED:
            continue
        if root in {"numpy", "pandas"} | extra_roots:
            continue
        if root in sys.stdlib_module_names and root != "importlib":
            continue
        bad.append(name)
    if "__import__" in source:
        bad.append("__import__")
    return bad


def notebook_problems(
    notebook: dict[str, Any], extra_roots: frozenset[str] = NOTEBOOK_EXTRA
) -> list[str]:
    """Return what breaks the notebook gate: format, outputs, imports.

    `extra_roots` is the notebook's allowlist beyond the SDK, numpy, pandas and
    the stdlib (the exemplar's by default).
    """
    problems = []
    if notebook.get("nbformat") != NBFORMAT:
        problems.append(f"nbformat {notebook.get('nbformat')!r}, want {NBFORMAT}")
    for number, cell in enumerate(notebook.get("cells", [])):
        if cell.get("cell_type") != "code":
            continue
        if cell.get("outputs") != []:
            problems.append(f"cell {number}: outputs are not empty")
        if cell.get("execution_count") is not None:
            problems.append(f"cell {number}: execution_count is not null")
        source = cell["source"]
        text = source if isinstance(source, str) else "".join(source)
        problems.extend(
            f"cell {number}: forbidden import {name}"
            for name in forbidden_imports(text, extra_roots)
        )
        if print_calls(text):
            problems.append(f"cell {number}: print, show the result instead")
    return problems


def print_calls(source: str) -> int:
    """Return how many `print(...)` calls the source makes."""
    return sum(
        1
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "print"
    )


def test_the_worker_imports_only_the_sdk_numpy_pandas_and_the_stdlib() -> None:
    assert forbidden_imports(WORKER_PATH.read_text()) == []


@pytest.mark.parametrize(
    "line",
    [
        "import boto3",
        "from asteroidea._sql import sql",
        "import asteroidea._auth",
        "from duckdb import connect",
        "import importlib",
        "x = __import__('boto3')",
        "from . import admin",
    ],
)
def test_the_import_check_catches_each_kind_of_extra_import(line: str) -> None:
    source = WORKER_PATH.read_text() + "\n" + line + "\n"
    assert forbidden_imports(source) != []


def test_the_worker_stays_pandas_only() -> None:
    """`make analysis` keeps its allowed set; polars is for the notebook."""
    source = WORKER_PATH.read_text() + "\nimport polars\n"
    assert forbidden_imports(source) == ["polars"]
    source = WORKER_PATH.read_text() + "\nimport matplotlib\n"
    assert forbidden_imports(source) == ["matplotlib"]
    source = WORKER_PATH.read_text() + "\nfrom IPython.display import display\n"
    assert forbidden_imports(source) == ["IPython.display"]


def load_notebook() -> dict[str, Any]:
    return json.loads(NOTEBOOK_PATH.read_text())


def test_the_notebook_passes_the_gate() -> None:
    assert notebook_problems(load_notebook()) == []


def test_the_gate_fails_on_an_output() -> None:
    nb = load_notebook()
    first = next(c for c in nb["cells"] if c["cell_type"] == "code")
    first["outputs"] = [{"output_type": "stream", "name": "stdout", "text": ["x"]}]
    assert notebook_problems(nb) != []


def test_the_gate_fails_on_an_execution_count() -> None:
    nb = load_notebook()
    first = next(c for c in nb["cells"] if c["cell_type"] == "code")
    first["execution_count"] = 1
    assert notebook_problems(nb) != []


def test_the_gate_fails_on_a_forbidden_import() -> None:
    nb = load_notebook()
    first = next(c for c in nb["cells"] if c["cell_type"] == "code")
    first["source"] = ["import boto3\n", *first["source"]]
    assert notebook_problems(nb) == [
        "cell " + str(nb["cells"].index(first)) + ": forbidden import boto3"
    ]


@pytest.mark.parametrize(
    ("line", "refused"),
    [
        ("import pynapple as nap", []),
        ("from pynapple import TsGroup", []),
        ("import scipy.signal", ["scipy.signal"]),
        ("from sklearn import svm", ["sklearn"]),
    ],
)
def test_the_notebook_allowlist_takes_pynapple_and_still_refuses_others(
    line: str, refused: list[str]
) -> None:
    # Only pynapple is added; any other third-party module stays refused
    assert forbidden_imports(line + "\n", NOTEBOOK_EXTRA) == refused


def test_the_worker_does_not_gain_pynapple() -> None:
    source = WORKER_PATH.read_text() + "\nimport pynapple\n"
    assert forbidden_imports(source) == ["pynapple"]


def test_the_gate_fails_on_a_print() -> None:
    nb = load_notebook()
    first = next(c for c in nb["cells"] if c["cell_type"] == "code")
    first["source"] = [*first["source"], "\nprint(datasets)\n"]
    assert notebook_problems(nb) == [
        "cell " + str(nb["cells"].index(first)) + ": print, show the result instead"
    ]


def test_the_gate_passes_a_near_miss_in_markdown() -> None:
    nb = load_notebook()
    nb["cells"].append(
        {"cell_type": "markdown", "metadata": {}, "source": ["import boto3\n"]}
    )
    assert notebook_problems(nb) == []


def test_the_gate_fails_on_the_wrong_nbformat() -> None:
    nb = load_notebook()
    nb["nbformat"] = 3
    assert notebook_problems(nb) != []


DEEPER_HEADING: Final = "## Going deeper (slow)"
MAIN_PATH_CALLS: Final = (
    "asteroidea.sign_in(",
    "asteroidea.studies(",
    "asteroidea.participants(has=",
    "asteroidea.links(",
    "asteroidea.session_summary(",
    ".imshow(",
    "1 to 0 s, z",
)
SLOW_CALLS: Final = (
    "open_nwb(",
    "open_file(",
    "open_image(",
    "load_session(",
    "analysis.",
    ".rates(",
    ".tuning(",
    ".band_power(",
)


def main_path_code(notebook: dict[str, Any]) -> str:
    """Return the code cells before the 'Going deeper (slow)' heading, joined."""
    parts: list[str] = []
    for cell in notebook["cells"]:
        text = "".join(cell["source"])
        if cell["cell_type"] == "markdown" and text.startswith(DEEPER_HEADING):
            break
        if cell["cell_type"] == "code":
            parts.append(text)
    return "\n".join(parts)


def main_path_problems(source: str) -> list[str]:
    """Return what breaks the main path: a missing view or a slow read.

    The main path reads only the catalog and stored summaries: no raw file open,
    no live recompute of rates, tuning or band power.
    """
    problems = [f"missing {call}" for call in MAIN_PATH_CALLS if call not in source]
    problems.extend(f"slow call {call}" for call in SLOW_CALLS if call in source)
    return problems


def test_the_main_path_reads_each_view_from_stored_data() -> None:
    nb = load_notebook()
    headings = [
        "".join(c["source"]) for c in nb["cells"] if c["cell_type"] == "markdown"
    ]
    assert any(h.startswith(DEEPER_HEADING) for h in headings)
    assert main_path_problems(main_path_code(nb)) == []


def test_the_main_path_check_fails_on_a_raw_open_or_a_recompute() -> None:
    nb = load_notebook()
    code = [c for c in nb["cells"] if c["cell_type"] == "code"]
    code[1]["source"] = [
        "session = asteroidea.load_session('d', 's', '_')\n",
        "nwb = asteroidea.open_nwb('a')\n",
        "session.rates('scene_cut')\n",
    ]
    assert main_path_problems(main_path_code(nb)) == [
        "missing asteroidea.sign_in(",
        "slow call open_nwb(",
        "slow call load_session(",
        "slow call .rates(",
    ]


def test_the_raw_file_example_stays_after_the_deeper_heading() -> None:
    nb = load_notebook()
    assert "open_nwb(" not in main_path_code(nb)
    everything = "\n".join(
        "".join(c["source"]) for c in nb["cells"] if c["cell_type"] == "code"
    )
    assert "asteroidea.open_nwb(" in everything


# --- every notebook: one gate table --------------------------------------------

# The showcase adds scipy (signal work) and nilearn (MNI152 plots)
SHOWCASE_EXTRA: Final = NOTEBOOK_EXTRA | {"scipy", "nilearn"}
SHOWCASE_CALLS: Final = (
    "sign_in(",
    "load_session(",
    "sql(",
    "open_image(",
    "pl.",
)
SHOWCASE_ONE_OF: Final = ("plot_markers(", "plot_glass_brain(")
SHOWCASE_FORBIDDEN: Final = ("open_nwb(", "open_file(")


@dataclass(frozen=True)
class NotebookGate:
    """One notebook's gate: its allowlist and what its main path must (not) call.

    The main path is every code cell before the 'Going deeper (slow)' heading,
    or every code cell when the notebook has none.
    """

    name: str
    path: Path
    extra_roots: frozenset[str]
    must_contain: tuple[str, ...]
    must_not_contain: tuple[str, ...]
    forbidden_label: str
    one_of: tuple[str, ...] = ()


NOTEBOOK_GATES: Final = (
    NotebookGate(
        "exemplar",
        notebook.NOTEBOOKS["exemplar"],
        NOTEBOOK_EXTRA,
        MAIN_PATH_CALLS,
        SLOW_CALLS,
        "slow call",
    ),
    NotebookGate(
        "showcase",
        notebook.NOTEBOOKS["showcase"],
        SHOWCASE_EXTRA,
        SHOWCASE_CALLS,
        SHOWCASE_FORBIDDEN,
        "forbidden call",
        SHOWCASE_ONE_OF,
    ),
)
GATE_IDS: Final = [gate.name for gate in NOTEBOOK_GATES]


def gate_problems(gate: NotebookGate, path: Path | None = None) -> list[str]:
    """Return everything that breaks `gate` for the notebook at `path` (its own)."""
    where = path or gate.path
    if not where.exists():
        return [f"missing notebook {where}"]
    nb = json.loads(where.read_text())
    problems = notebook_problems(nb, gate.extra_roots)
    code = main_path_code(nb)
    problems.extend(f"missing {call}" for call in gate.must_contain if call not in code)
    if gate.one_of and not any(call in code for call in gate.one_of):
        problems.append(f"missing one of {', '.join(gate.one_of)}")
    problems.extend(
        f"{gate.forbidden_label} {call}"
        for call in gate.must_not_contain
        if call in code
    )
    return problems


def test_the_registry_and_the_gate_table_name_the_same_notebooks() -> None:
    assert [(g.name, g.path) for g in NOTEBOOK_GATES] == list(
        notebook.NOTEBOOKS.items()
    )


@pytest.mark.parametrize("gate", NOTEBOOK_GATES, ids=GATE_IDS)
def test_each_notebook_passes_its_gate(gate: NotebookGate) -> None:
    assert gate_problems(gate) == []


def test_the_exemplar_gate_keeps_its_own_rules() -> None:
    exemplar = NOTEBOOK_GATES[0]
    assert exemplar.path == NOTEBOOK_PATH
    assert exemplar.extra_roots == NOTEBOOK_EXTRA
    assert exemplar.must_contain == MAIN_PATH_CALLS
    assert exemplar.must_not_contain == SLOW_CALLS
    assert not {"scipy", "nilearn"} & exemplar.extra_roots


def write_copy(gate: NotebookGate, tmp_path: Path, nb: dict[str, Any]) -> Path:
    out = tmp_path / gate.path.name
    out.write_text(json.dumps(nb))
    return out


def code_cell(nb: dict[str, Any], needle: str) -> dict[str, Any]:
    return next(
        c
        for c in nb["cells"]
        if c["cell_type"] == "code" and needle in "".join(c["source"])
    )


@pytest.mark.parametrize("gate", NOTEBOOK_GATES, ids=GATE_IDS)
def test_a_missing_notebook_fails_its_gate(gate: NotebookGate, tmp_path: Path) -> None:
    gone = tmp_path / gate.path.name
    assert gate_problems(gate, gone) == [f"missing notebook {gone}"]


@pytest.mark.parametrize("gate", NOTEBOOK_GATES, ids=GATE_IDS)
def test_a_planted_print_output_or_import_fails_each_gate(
    gate: NotebookGate, tmp_path: Path
) -> None:
    clean = json.loads(gate.path.read_text())
    first = next(c for c in clean["cells"] if c["cell_type"] == "code")
    number = clean["cells"].index(first)
    plants = {
        "print": ("source", [*first["source"], "\nprint(1)\n"]),
        "output": ("outputs", [{"output_type": "stream", "name": "stdout"}]),
        "count": ("execution_count", 3),
        "seaborn": ("source", ["import seaborn\n", *first["source"]]),
        "sklearn": ("source", ["from sklearn import svm\n", *first["source"]]),
    }
    want = {
        "print": f"cell {number}: print, show the result instead",
        "output": f"cell {number}: outputs are not empty",
        "count": f"cell {number}: execution_count is not null",
        "seaborn": f"cell {number}: forbidden import seaborn",
        "sklearn": f"cell {number}: forbidden import sklearn",
    }
    for plant, (key, value) in plants.items():
        nb = json.loads(gate.path.read_text())
        nb["cells"][number][key] = value
        assert gate_problems(gate, write_copy(gate, tmp_path, nb)) == [want[plant]]


@pytest.mark.parametrize("gate", NOTEBOOK_GATES, ids=GATE_IDS)
def test_dropping_a_required_call_fails_each_gate(
    gate: NotebookGate, tmp_path: Path
) -> None:
    for call in gate.must_contain:
        nb = json.loads(gate.path.read_text())
        for cell in nb["cells"]:
            if cell["cell_type"] == "code":
                # `sql(` becomes `sql_x(`: still valid Python, no longer the call
                text = "".join(cell["source"]).replace(
                    call, call[:-1] + "_x" + call[-1]
                )
                cell["source"] = [text]
        assert f"missing {call}" in gate_problems(gate, write_copy(gate, tmp_path, nb))


def test_the_showcase_allows_scipy_and_nilearn_only() -> None:
    showcase = NOTEBOOK_GATES[1]
    for line, refused in [
        ("import scipy.signal", []),
        ("from scipy import stats", []),
        ("from nilearn import plotting", []),
        ("import nibabel", ["nibabel"]),
        ("import seaborn as sns", ["seaborn"]),
        ("import importlib", ["importlib"]),
    ]:
        assert forbidden_imports(line + "\n", showcase.extra_roots) == refused


def test_the_showcase_gate_refuses_a_raw_open_and_needs_a_brain_plot(
    tmp_path: Path,
) -> None:
    showcase = NOTEBOOK_GATES[1]
    nb = json.loads(showcase.path.read_text())
    cell = code_cell(nb, "load_session(")
    cell["source"] = [*cell["source"], "\nraw = asteroidea.open_nwb('a')\n"]
    for other in nb["cells"]:
        if other["cell_type"] == "code":
            text = "".join(other["source"])
            for call in SHOWCASE_ONE_OF:
                text = text.replace(call, call[:-1] + "_x(")
            other["source"] = [text]
    assert gate_problems(showcase, write_copy(showcase, tmp_path, nb)) == [
        "missing one of plot_markers(, plot_glass_brain(",
        "forbidden call open_nwb(",
    ]


# --- a fake SDK ------------------------------------------------------------------


@dataclass
class Table:
    """The two Arrow-table members the worker uses."""

    rows: list[dict[str, Any]]

    def to_pylist(self) -> list[dict[str, Any]]:
        return list(self.rows)

    @property
    def num_rows(self) -> int:
        return len(self.rows)


@dataclass
class Counter:
    bytes_fetched: int = 0


@dataclass
class Series:
    timestamps: Any = None
    data: Any = None


@dataclass
class Units:
    frame: pd.DataFrame

    def to_dataframe(self) -> pd.DataFrame:
        return self.frame


@dataclass
class Nwb:
    stimulus: dict[str, Series]
    units: Units
    processing: dict[str, Any]
    remote_file: Counter = field(default_factory=lambda: Counter(30 * 1024 * 1024))


@dataclass
class Image:
    dataobj: Any
    extra: dict[str, Any]


@dataclass
class Data:
    """What the fake catalog and files hold; tests change it before a step."""

    licences: dict[str, str] = field(
        default_factory=lambda: {
            "dandi-000623": "CC-BY-4.0",
            "openneuro-ds004798": "CC0",
        }
    )
    # 93 cuts, 100 frames (4 s) apart, so the 1 s windows never overlap
    cut_frames: list[int] = field(default_factory=lambda: list(range(1, 9202, 100)))
    cs41_units: int = 2
    sql_error: str | None = None


class QueryError(Exception):
    """Stands in for the SDK's error base class."""


def cut_seconds(frames: Sequence[int]) -> np.ndarray:
    stamps = np.arange(N_FRAMES) / FPS
    return stamps[np.asarray(frames) - 1]


SUBJECT_ROWS: Final = [
    {"subject_key": f"bmovie-patient-{n}", "dataset": d, "subject": s}
    for n in ("41", "62")
    for d, s in (("dandi-000623", f"sub-CS{n}"), ("openneuro-ds004798", f"sub-p{n}cs"))
]
LFP_PATH: Final = "/processing/ecephys/LFP/ElectricalSeries"
SERIES_ROWS: Final = [
    {
        "asset_id": asset,
        "path": path,
        "neurodata_type": kind,
        "shape": shape,
        "has_timestamps": stamped,
    }
    for asset, path, kind, shape, stamped in (
        (CS62_R2, LFP_PATH, "ElectricalSeries", [2000, 16], False),
        (CS62_R2, "/stimulus/movieframe_time", "TimeSeries", [N_FRAMES], True),
        ("other", LFP_PATH, "ElectricalSeries", [9, 9], False),
    )
]
UNIT_ROWS: Final = [
    {
        "asset_id": CS62_R2,
        "unit_index": 0,
        "unit_id": "u0",
        "location": "Right amygdala",
    },
    {"asset_id": "other", "unit_index": 1, "unit_id": "x", "location": "Left amygdala"},
]
NWB_ROWS: Final = [
    {"asset_id": "r1", "s3_key": "raw/d/sub-CS62/x_ses-P62CSR1_e.nwb"},
    {"asset_id": CS62_R2, "s3_key": "raw/d/sub-CS62/x_ses-P62CSR2.nwb"},
]
BOLD_ROWS: Final = [
    {
        "asset_id": asset,
        "s3_key": f"raw/o/sub-p62cs/func/x_task-movie_run-{run}_bold.nii.gz",
        "suffix": "bold",
        "size_bytes": 406002058,
        "nifti_shape": [80, 80, 60, 750],
    }
    for asset, run in ((BOLD_ID, 1), ("run-2", 2))
]
CSV_ROWS: Final = [
    {"asset_id": CSV_ID, "s3_key": "raw/dandi-000623/stimuli/scenecut_info.csv"}
]


def fake_sql(data: Data, query: str) -> Table:
    """Answer the worker's fixed queries from the rows above."""
    if data.sql_error is not None:
        raise QueryError(data.sql_error)
    if "GROUP BY ALL" in query:
        rows = [{"subject": "sub-CS62", "bids_session": "ses-P62CSR2", "n": 35}]
        if data.cs41_units:
            rows.append(
                {
                    "subject": "sub-CS41",
                    "bids_session": "ses-P41CSR1",
                    "n": data.cs41_units,
                }
            )
        return Table(rows)
    for marker, rows in (
        ("FROM subject_index", SUBJECT_ROWS),
        ("FROM series", SERIES_ROWS),
        ("FROM units", UNIT_ROWS),
    ):
        if marker in query:
            return Table(list(rows))
    raise AssertionError(query)


def fake_assets(**filters: str) -> Table:
    """`assets()`: the NWB, NIfTI or (unfiltered) CSV rows."""
    by_format = {"nwb": NWB_ROWS, "nifti": BOLD_ROWS}
    return Table(list(by_format.get(filters.get("file_format") or "", CSV_ROWS)))


def fake_sdk(data: Data) -> types.ModuleType:
    """Return a module with the public SDK calls the worker makes."""
    sdk = types.ModuleType("asteroidea")
    cuts = cut_seconds(data.cut_frames if max(data.cut_frames) <= N_FRAMES else [1])
    # unit 0: one spike 0.5 s before each cut and two after -> 1 Hz pre, 2 Hz post
    spikes = np.sort(np.concatenate([cuts - 0.5, cuts + 0.25, cuts + 0.5]))
    nwb = Nwb(
        stimulus={
            "movieframe_time": Series(timestamps=np.arange(N_FRAMES) / FPS),
            "movie_still": Series(timestamps=None),
        },
        units=Units(pd.DataFrame({"spike_times": [spikes, np.array([1.0])]})),
        processing={
            "ecephys": {"LFP": {"ElectricalSeries": Series(data=np.ones((2000, 16)))}}
        },
    )

    def sql(query: str) -> Table:
        return fake_sql(data, query)

    def datasets() -> Table:
        return Table(
            [
                {"dataset": name, "title": f"title of {name}", "license": licence}
                for name, licence in data.licences.items()
            ]
        )

    def open_file(asset_id: str) -> io.BytesIO:
        assert asset_id == CSV_ID
        frame = pd.DataFrame({"scene_id": 1.0, "shot_start_fr": data.cut_frames})
        return io.BytesIO(frame.to_csv(index=False).encode())

    def open_nwb(asset_id: str) -> Nwb:
        assert asset_id == CS62_R2
        return nwb

    def open_image(asset_id: str) -> Image:
        assert asset_id == BOLD_ID
        return Image(np.zeros((80, 80, 60, 3)), {"remote_file": Counter(4 * 1024**2)})

    for name, value in {
        "AsteroideaError": QueryError,
        "sql": sql,
        "datasets": datasets,
        "assets": fake_assets,
        "open_file": open_file,
        "open_nwb": open_nwb,
        "open_image": open_image,
    }.items():
        setattr(sdk, name, value)
    return sdk


@pytest.fixture
def data() -> Data:
    return Data()


@pytest.fixture
def worker(monkeypatch: pytest.MonkeyPatch, data: Data) -> types.ModuleType:
    """Load analysis_worker.py with the fake SDK in place of `asteroidea`."""
    monkeypatch.setitem(sys.modules, "asteroidea", fake_sdk(data))
    spec = importlib.util.spec_from_file_location("analysis_worker_fake", WORKER_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_worker(worker: types.ModuleType, name: str) -> tuple[int, dict[str, Any]]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = worker.main(["analysis_worker.py", name])
    return code, json.loads(out.getvalue().strip().splitlines()[-1])


# --- the worker and the judges ---------------------------------------------------


JUDGES: Final = {
    "datasets": analysis.judge_datasets,
    "subjects": analysis.judge_subjects,
    "units": analysis.judge_units,
    "scenecuts": analysis.judge_scenecuts,
    "lfp": analysis.judge_lfp,
    "fmri": analysis.judge_fmri,
}


@pytest.mark.parametrize("name", list(JUDGES))
def test_each_step_passes_on_good_data(worker: types.ModuleType, name: str) -> None:
    code, out = run_worker(worker, name)
    assert code == 0
    result = JUDGES[name](out)
    assert result.passed, result.detail


def test_scene_cut_rates_use_timestamps_at_shot_start_minus_one(
    worker: types.ModuleType,
) -> None:
    _, out = run_worker(worker, "scenecuts")
    assert out["stimulus_path"] == "/stimulus/movieframe_time"
    assert out["stimulus_paths_in_catalog"] == ["/stimulus/movieframe_time"]
    assert out["first_cut_s"] == 0.0
    assert out["n_cuts"] == 93
    assert out["last_cut_s"] == 9200 / FPS
    assert [u["unit_id"] for u in out["units"]] == ["u0"]
    assert out["units"][0]["pre_hz"] == 1.0
    assert out["units"][0]["post_hz"] == 2.0


def test_a_cut_frame_outside_the_timestamps_fails_with_the_numbers(
    worker: types.ModuleType, data: Data
) -> None:
    data.cut_frames = [1, 500, N_FRAMES + 7]
    code, out = run_worker(worker, "scenecuts")
    assert code == 0
    assert out["frames_outside"] == 1
    assert out["units"] == []
    result = analysis.judge_scenecuts(out)
    assert not result.passed
    assert f"frames 1..{N_FRAMES + 7} of {N_FRAMES} timestamps" in result.detail
    assert f"1 cut frames outside the {N_FRAMES} timestamps" in result.detail


def test_a_cut_count_other_than_93_fails(worker: types.ModuleType) -> None:
    _, out = run_worker(worker, "scenecuts")
    out["n_cuts"] = 92
    assert "92 cuts, expected 93" in analysis.judge_scenecuts(out).detail
    assert not analysis.judge_scenecuts(out).passed


def test_the_sdk_error_is_printed_verbatim(
    worker: types.ModuleType, data: Data
) -> None:
    data.sql_error = "Catalog Error: Table with name units does not exist"
    code, out = run_worker(worker, "units")
    assert code == 1
    assert out == {
        "error": "QueryError: Catalog Error: Table with name units does not exist"
    }


def test_a_wrong_licence_or_a_missing_subject_fails(
    worker: types.ModuleType, data: Data
) -> None:
    data.licences["openneuro-ds004798"] = "CC-BY-4.0"
    data.cs41_units = 0
    assert not analysis.judge_datasets(run_worker(worker, "datasets")[1]).passed
    units = analysis.judge_units(run_worker(worker, "units")[1])
    assert not units.passed
    assert "sub-CS41 0" in units.detail


def test_limits_come_with_their_basis() -> None:
    out = {"shape": [1000, 8], "finite": True, "seconds": 60.1, "fetched": 1}
    slow = analysis.judge_lfp(out)
    assert not slow.passed
    assert "(limit 60 s, NWB open, estimated; a run took 28.1 s)" in slow.detail
    big = analysis.judge_fmri(
        {
            "shape": [2, 2, 2],
            "nifti_shape": [2, 2, 2, 9],
            "seconds": 1.0,
            "fetched": 32 * 1024 * 1024 + 1,
        }
    )
    assert not big.passed
    assert "(limit 32 MiB, gzipped volume, estimated; a run took 4.0 MiB)" in big.detail


# --- the runner ------------------------------------------------------------------


def in_process(worker: types.ModuleType) -> Callable[..., Proc]:
    """Return an `execute` stand-in that runs the worker step in this process."""

    def execute(cmd: Sequence[str], **_kwargs: object) -> Proc:
        if cmd[0] == "git":
            return Proc(0, "abc1234\n", "")
        assert cmd[1] == str(WORKER_PATH)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = worker.main(["analysis_worker.py", cmd[2]])
        return Proc(code, out.getvalue(), "")

    return execute


def data_steps() -> list[analysis.Step]:
    return [step for step in analysis.STEPS if step[0][0].isdigit()]


def new_run(tmp_path: Path) -> tuple[analysis.AnalysisRun, list[str]]:
    said: list[str] = []
    console = Console(headless=True, read=lambda _p: "", write=said.append)
    run = analysis.AnalysisRun(console, "r1", tmp_path, "profile", "000000000000")
    return run, said


def test_the_runner_prints_pass_lines_and_writes_the_report(
    worker: types.ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(analysis, "execute", in_process(worker))
    run, said = new_run(tmp_path)
    assert analysis.run_all(run, data_steps())
    assert [r.name for r in run.results] == [
        "1 Datasets",
        "2 Subjects",
        "3 Amygdala units",
        "4 Scene cuts",
        "5 LFP slice",
        "6 fMRI volume",
    ]
    assert sum(line.startswith("PASS  ") for line in said) == 6
    report = analysis.write_report(run).read_text()
    assert report.startswith("# Researcher analysis run r1")
    assert "| 0 | u0 | Right amygdala | 279 | 1.000 | 2.000 |" in report
    assert "- scenecuts.nwb_asset_id: nwb-cs62-r2" in report


def test_the_runner_stops_at_the_first_fail_with_the_sdk_error(
    worker: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    data: Data,
) -> None:
    monkeypatch.setattr(analysis, "execute", in_process(worker))
    data.sql_error = "IO Error: HTTP 403"
    run, said = new_run(tmp_path)
    assert not analysis.run_all(run, data_steps())
    assert [(r.name, r.passed) for r in run.results] == [
        ("1 Datasets", True),
        ("2. Finding patients 41 and 62", False),
    ]
    assert run.results[1] == StepResult(
        "2. Finding patients 41 and 62",
        passed=False,
        detail="QueryError: IO Error: HTTP 403",
        seconds=run.results[1].seconds,
    )
    assert "FAIL  QueryError: IO Error: HTTP 403" in said
    assert not any("3. Counting" in line for line in said)


def test_a_worker_crash_is_named_as_a_worker_defect(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def crash(_cmd: Sequence[str], **_kwargs: object) -> Proc:
        return Proc(1, "", "Traceback ...\nKeyError: 'title'\n")

    monkeypatch.setattr(analysis, "execute", crash)
    run, said = new_run(tmp_path)
    assert not analysis.run_all(run, data_steps()[:1])
    assert "FAIL  the datasets step crashed: KeyError: 'title'" in said
    assert "Next step: Report this as a defect of the worker." in said


def test_the_analysis_researcher_gets_its_own_email_tag() -> None:
    now = datetime(2026, 10, 24, 20, 0, tzinfo=UTC)
    assert suggest_email(None, now, headless=True, tag="ana") == (
        "ana-202610242000@example.invalid"
    )
    assert suggest_email("owner+x@example.com", now, headless=False, tag="ana") == (
        "owner+ana-202610242000@example.com"
    )
