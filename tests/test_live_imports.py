"""Every module under `tests/live/` imports without `ASTEROIDEA_LIVE`.

The live suite is not collected in `make check`, so a renamed module it imports would
break it unseen. Loading each file by path here makes the broken import fail the gate,
with the file named in the test id and the message.
"""

import importlib.util
from pathlib import Path

import pytest

LIVE_DIR = Path(__file__).resolve().parent / "live"
FILES = sorted(LIVE_DIR.glob("*.py"))


def test_the_live_directory_has_modules() -> None:
    assert FILES


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_a_live_module_imports_without_the_live_switch(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ASTEROIDEA_LIVE", raising=False)
    spec = importlib.util.spec_from_file_location(f"_live_import_{path.stem}", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except ImportError as err:
        pytest.fail(f"tests/live/{path.name} does not import: {err}")
