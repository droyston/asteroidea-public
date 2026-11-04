"""Workspace test: every uv workspace member has tests and an installed distribution."""

import tomllib
from importlib.metadata import distribution
from pathlib import Path

from asteroidea import _sql
from asteroidea_infra import schema

ROOT = Path(__file__).resolve().parents[1]


def _members() -> list[str]:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    return [str(m) for m in config["tool"]["uv"]["workspace"]["members"]]


def test_members_listed() -> None:
    assert _members()


def test_each_member_has_tests() -> None:
    for member in _members():
        assert list((ROOT / member / "tests").glob("test_*.py")), member


def test_each_member_is_installed() -> None:
    for member in _members():
        project = tomllib.loads((ROOT / member / "pyproject.toml").read_text())
        assert distribution(project["project"]["name"]).version, member


def test_sdk_table_list_equals_schema() -> None:
    assert _sql.TABLE_NAMES == schema.TABLE_NAMES
