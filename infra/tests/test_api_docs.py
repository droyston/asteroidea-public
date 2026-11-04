"""`docs/src/api.md` matches the route table.

The table between the markers must equal what `api_docs` renders from `ROUTES`, every
route must have a hand-written section, and no section may name a route that is not in
the table. The first three tests read the real page; the rest prove each check can fail.
"""

import dataclasses
from typing import TYPE_CHECKING, Final

from asteroidea_infra import api_docs
from asteroidea_ingest import routes

if TYPE_CHECKING:
    import pytest

SCRATCH: Final = dataclasses.replace(
    routes.ROUTES[0], method="GET", path="/scratch", summary="scratch"
)


def _page() -> str:
    return api_docs.PAGE.read_text(encoding="utf-8")


def test_the_table_is_current() -> None:
    assert api_docs.render_page(_page()) == _page(), api_docs.STALE


def test_every_route_has_a_section() -> None:
    sections = api_docs.section_keys(_page())
    missing = [
        f"missing section for {r.key}" for r in routes.ROUTES if r.key not in sections
    ]
    assert missing == []


def test_every_section_names_a_route() -> None:
    keys = {r.key for r in routes.ROUTES}
    extra = [k for k in api_docs.section_keys(_page()) if k not in keys]
    assert extra == [], "section for a route not in the table"


def test_has_no_problems() -> None:
    assert api_docs.problems(_page()) == []


def test_a_new_route_makes_the_page_stale(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(routes, "ROUTES", (*routes.ROUTES, SCRATCH))
    found = api_docs.problems(_page())
    assert api_docs.STALE in found
    assert "missing section for GET /scratch" in found


def test_a_deleted_section_is_reported() -> None:
    text = _page()
    heading = "### `GET /uploads`"
    assert heading in text
    found = api_docs.problems(text.replace(heading, "### GET /uploads"))
    assert found == ["missing section for GET /uploads"]


def test_a_section_for_an_unknown_route_is_reported() -> None:
    found = api_docs.problems(f"{_page()}\n### `GET /scratch`\n")
    assert found == ["section for a route not in the table: GET /scratch"]


def test_a_page_without_markers_is_reported() -> None:
    found = api_docs.problems("# no markers\n")
    assert found[0].startswith("docs/src/api.md needs the markers")


def test_render_is_idempotent() -> None:
    once = api_docs.render_page(_page())
    assert api_docs.render_page(once) == once
