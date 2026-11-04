"""The web app's "Copy code" snippets are valid Python that binds against the SDK.

Each template in `web/src/snippets/templates.json` is rendered with fixture ids (as
Python string literals, the way `render.ts` does) and parsed with `ast`. Every
`asteroidea.<name>(...)` call is then bound to `inspect.signature` of the SDK's
function. A wrong keyword or a missing argument fails here, not in a researcher's
notebook.
"""

import ast
import inspect
import json
import re
from annotationlib import Format
from pathlib import Path
from typing import Any

import pytest

import asteroidea

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = sorted((ROOT / "web" / "src" / "snippets").glob("templates.json"))

FIXTURE = {
    "study": "movie-watching",
    "dataset": "dandi-000623",
    "subject": "sub-CS62",
    "session": "ses-P62CSR2",
    "subject_key": 'key"with\\odd\nchars',
}


def render(code: str, values: dict[str, str]) -> str:
    """Fill each `{name}` with a JSON string, a Python literal (as `render.ts` does)."""
    return re.sub(r"\{(\w+)\}", lambda m: json.dumps(values[m.group(1)]), code)


def sdk_calls(source: str) -> list[ast.Call]:
    """Every `asteroidea.<name>(...)` call in `source`, which must also parse."""
    tree = ast.parse(source)
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "asteroidea"
    ]


def bind(call: ast.Call) -> None:
    """Bind the call's arguments to the SDK function's signature; raise if no fit."""
    assert isinstance(call.func, ast.Attribute)
    function = getattr(asteroidea, call.func.attr)
    args: list[Any] = [ast.literal_eval(a) for a in call.args]
    kwargs = {
        k.arg: ast.literal_eval(k.value) for k in call.keywords if k.arg is not None
    }
    # The SDK imports pyarrow for annotations only; ask for text, so none is evaluated.
    inspect.signature(function, annotation_format=Format.STRING).bind(*args, **kwargs)


def check(templates: dict[str, Any]) -> None:
    for page, template in templates.items():
        source = render(template["code"], FIXTURE)
        calls = sdk_calls(source)
        assert calls, f"{page}: the snippet makes no asteroidea call"
        assert "import asteroidea" in source, f"{page}: runs alone only with the import"
        assert any(
            c.func.attr == "sign_in" for c in calls if isinstance(c.func, ast.Attribute)
        ), f"{page}: runs alone only with asteroidea.sign_in()"
        for call in calls:
            try:
                bind(call)
            except TypeError as err:
                msg = f"{page}: {ast.unparse(call)} does not fit the SDK: {err}"
                raise AssertionError(msg) from err


def test_templates_are_found() -> None:
    assert [p.name for p in TEMPLATES] == ["templates.json"]
    pages = json.loads(TEMPLATES[0].read_text())
    assert list(pages) == ["home", "study", "participant", "session"]


@pytest.mark.parametrize("path", TEMPLATES, ids=lambda p: p.name)
def test_every_snippet_parses_and_binds_against_the_sdk(path: Path) -> None:
    check(json.loads(path.read_text()))


def test_a_wrong_keyword_fails() -> None:
    bad = {
        "home": {
            "code": (
                "import asteroidea\n\nasteroidea.sign_in()\n"
                "asteroidea.studies(studyy={study})\n"
            )
        }
    }
    with pytest.raises(AssertionError, match="does not fit the SDK"):
        check(bad)


def test_every_placeholder_has_a_fixture_value() -> None:
    for path in TEMPLATES:
        for page, template in json.loads(path.read_text()).items():
            names = set(re.findall(r"\{(\w+)\}", template["code"]))
            assert names <= FIXTURE.keys(), f"{page}: {names - FIXTURE.keys()}"
