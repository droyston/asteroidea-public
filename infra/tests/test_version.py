"""Every manifest, lockfile and the services User-Agent agree with the root `VERSION`.

`VERSION` is the oracle: nothing under test computes it. A hand edit of one manifest
fails here until `make version-sync` writes the others.
"""

import json
import shutil
import tomllib
from importlib.metadata import version as installed_version
from pathlib import Path

import pytest

from asteroidea_infra import version_sync
from asteroidea_services.http import _USER_AGENT

REPO = Path(__file__).resolve().parents[2]
VERSION = (REPO / "VERSION").read_text(encoding="utf-8").strip()
DISTRIBUTIONS = {
    "infra": "asteroidea-infra",
    "ingest": "asteroidea-ingest",
    "services": "asteroidea-services",
    "sdk": "asteroidea",
}


def test_version_file_is_one_semver_line() -> None:
    assert version_sync.read_version(REPO) == VERSION


@pytest.mark.parametrize("member", sorted(DISTRIBUTIONS))
def test_member_pyproject_holds_version(member: str) -> None:
    text = (REPO / member / "pyproject.toml").read_text(encoding="utf-8")
    assert tomllib.loads(text)["project"]["version"] == VERSION


@pytest.mark.parametrize("member", sorted(DISTRIBUTIONS))
def test_installed_distribution_holds_version(member: str) -> None:
    assert installed_version(DISTRIBUTIONS[member]) == VERSION


def test_uv_lock_holds_version_for_every_member() -> None:
    lock = tomllib.loads((REPO / "uv.lock").read_text(encoding="utf-8"))
    members = {
        p["name"]: p["version"]
        for p in lock["package"]
        if "editable" in p.get("source", {})
    }
    assert members == dict.fromkeys(sorted(DISTRIBUTIONS.values()), VERSION)


def test_web_package_and_lockfile_hold_version() -> None:
    package = json.loads((REPO / "web/package.json").read_text(encoding="utf-8"))
    lock = json.loads((REPO / "web/package-lock.json").read_text(encoding="utf-8"))
    assert package["version"] == VERSION
    assert lock["version"] == VERSION
    assert lock["packages"][""]["version"] == VERSION


def test_harvest_golden_index_holds_version() -> None:
    golden = REPO / version_sync.HARVEST_GOLDEN
    index = json.loads(golden.read_text(encoding="utf-8"))
    assert index["harvester"] == {"package": "asteroidea-services", "version": VERSION}


def test_services_user_agent_holds_version() -> None:
    assert _USER_AGENT.startswith(f"asteroidea-services/{VERSION} (+https://")


def _copy_tree(tmp_path: Path) -> Path:
    paths = [
        *(Path(m) / "pyproject.toml" for m in DISTRIBUTIONS),
        version_sync.PACKAGE_JSON,
        version_sync.HARVEST_GOLDEN,
    ]
    for rel in paths:
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO / rel, tmp_path / rel)
    return tmp_path


def test_sync_writes_version_into_every_manifest(tmp_path: Path) -> None:
    root = _copy_tree(tmp_path)
    (root / "VERSION").write_text("12.3.40\n", encoding="utf-8")
    changed = version_sync.sync(root)
    assert sorted(p.relative_to(root).as_posix() for p in changed) == sorted(
        [
            "infra/pyproject.toml",
            "ingest/pyproject.toml",
            "sdk/pyproject.toml",
            "services/pyproject.toml",
            "services/tests/fixtures/harvest/expected-index.json",
            "web/package.json",
        ]
    )
    for member in DISTRIBUTIONS:
        text = (root / member / "pyproject.toml").read_text(encoding="utf-8")
        assert tomllib.loads(text)["project"]["version"] == "12.3.40"
    package = json.loads((root / "web/package.json").read_text(encoding="utf-8"))
    assert package["version"] == "12.3.40"
    golden = root / version_sync.HARVEST_GOLDEN
    assert json.loads(golden.read_text())["harvester"]["version"] == "12.3.40"
    assert version_sync.sync(root) == []


def test_sync_changes_only_the_version_field(tmp_path: Path) -> None:
    root = _copy_tree(tmp_path)
    (root / "VERSION").write_text("12.3.40\n", encoding="utf-8")
    version_sync.sync(root)
    for member in DISTRIBUTIONS:
        before = (REPO / member / "pyproject.toml").read_text(encoding="utf-8")
        after = (root / member / "pyproject.toml").read_text(encoding="utf-8")
        old = f'version = "{VERSION}"'
        assert after == before.replace(old, 'version = "12.3.40"', 1)


@pytest.mark.parametrize(
    "text", ["1.2\n", "v1.2.3\n", "01.2.3\n", "1.2.3", "1.2.3\n\n"]
)
def test_sync_refuses_a_version_that_is_not_one_semver_line(
    tmp_path: Path, text: str
) -> None:
    root = _copy_tree(tmp_path)
    (root / "VERSION").write_text(text, encoding="utf-8")
    with pytest.raises(version_sync.VersionError, match=r"must hold one line X\.Y\.Z"):
        version_sync.sync(root)
