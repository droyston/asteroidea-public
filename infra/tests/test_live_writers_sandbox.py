"""Every live-check dataset writer marks its dataset sandbox at create.

The data home folds sandbox datasets away, so a live check that writes a dataset
without the mark leaves a test dataset in the researcher's list. This scans
`tests/live/` and the runner modules for the calls that create a dataset and fails on
a call without the mark: `upload(..., sandbox=True)`,
`upload_dataset(..., sandbox=True)`, `open_session(..., batch=sandbox_batch())` and a
`POST /imports` body with `"sandbox": True`.
"""

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCANNED = [
    *sorted((ROOT / "tests" / "live").glob("*.py")),
    *sorted((ROOT / "infra" / "src" / "asteroidea_infra").glob("*.py")),
]
UPLOADS = {"upload", "upload_dataset"}


def _name(call: ast.Call) -> str:
    fn = call.func
    if isinstance(fn, ast.Attribute):
        return fn.attr
    return fn.id if isinstance(fn, ast.Name) else ""


def _kw(call: ast.Call, key: str) -> ast.expr | None:
    return next((k.value for k in call.keywords if k.arg == key), None)


def _is_true(node: ast.expr | None) -> bool:
    return isinstance(node, ast.Constant) and node.value is True


def _is_sandbox_batch(node: ast.expr | None) -> bool:
    return isinstance(node, ast.Call) and _name(node) == "sandbox_batch"


def _is_post_imports(call: ast.Call) -> bool:
    consts = [a.value for a in call.args if isinstance(a, ast.Constant)]
    return "POST" in consts and "/imports" in consts


def _body_marked(call: ast.Call) -> bool:
    for arg in call.args:
        if isinstance(arg, ast.Dict):
            pairs = {
                k.value: v
                for k, v in zip(arg.keys, arg.values, strict=True)
                if isinstance(k, ast.Constant)
            }
            if _is_true(pairs.get("sandbox")):
                return True
    return False


def unmarked(source: str) -> list[tuple[int, str]]:
    """Return (line, call name) of each dataset-creating call without the mark."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        name = _name(node)
        if name in UPLOADS:
            ok = _is_true(_kw(node, "sandbox"))
        elif name == "open_session":
            ok = _is_sandbox_batch(_kw(node, "batch"))
        elif _is_post_imports(node):
            ok = _body_marked(node)
            name = "POST /imports"
        else:
            continue
        if not ok:
            found.append((node.lineno, name))
    return found


@pytest.mark.parametrize("path", SCANNED, ids=lambda p: p.name)
def test_every_dataset_writer_marks_sandbox(path: Path) -> None:
    assert unmarked(path.read_text()) == []


def test_the_scan_sees_the_known_writers() -> None:
    seen = {
        p.name
        for p in SCANNED
        if any(
            tok in p.read_text()
            for tok in ("sandbox=True", "sandbox_batch()", '"sandbox": True')
        )
    }
    assert {
        "first_hour_worker.py",
        "test_artifacts.py",
        "test_first_hour.py",
        "test_ownership_lock.py",
        "test_ingest_core.py",
        "test_sdk_data.py",
        "test_rf_imports.py",
    } <= seen


@pytest.mark.parametrize(
    "source",
    [
        "u.upload(p, d)",
        "u.upload(p, d, sandbox=False)",
        "upload_dataset(u, r, d)",
        "u.open_session(p, d)",
        "u.open_session(p, d, batch={})",
        '_call(url, t, "POST", "/imports", {"source": S})',
    ],
)
def test_the_scan_catches_an_unmarked_call(source: str) -> None:
    assert unmarked(source) != []


@pytest.mark.parametrize(
    "source",
    [
        "u.upload(p, d, sandbox=True)",
        "upload_dataset(u, r, d, sandbox=True)",
        "u.open_session(p, d, batch=_upload.sandbox_batch())",
        "u.open_session(p, d, batch=sandbox_batch())",
        '_call(url, t, "POST", "/imports", {"source": S, "sandbox": True})',
        '_call(url, t, "GET", "/imports")',
    ],
)
def test_the_scan_accepts_a_marked_call(source: str) -> None:
    assert unmarked(source) == []
