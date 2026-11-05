"""Smoke test: the distribution is installed from this member's src/."""

from importlib.metadata import distribution
from pathlib import Path

import asteroidea_ingest

MEMBER = Path(__file__).resolve().parents[1]


def test_distribution_resolves() -> None:
    version = (MEMBER.parent / "VERSION").read_text(encoding="utf-8").strip()
    assert distribution("asteroidea-ingest").version == version


def test_imported_from_member_src() -> None:
    assert Path(asteroidea_ingest.__file__).resolve().is_relative_to(MEMBER / "src")
