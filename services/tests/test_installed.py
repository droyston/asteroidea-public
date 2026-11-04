"""Smoke test: the distribution is installed from this member's src/."""

from importlib.metadata import distribution
from pathlib import Path

import asteroidea_services

MEMBER = Path(__file__).resolve().parents[1]


def test_distribution_resolves() -> None:
    assert distribution("asteroidea-services").version == "0.0.0"


def test_imported_from_member_src() -> None:
    assert Path(asteroidea_services.__file__).resolve().is_relative_to(MEMBER / "src")
