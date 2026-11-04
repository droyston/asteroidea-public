"""The function registry and the route table are consistent, and every handler resolves.

The template gate cannot see an image's CMD when the function has no ImageConfig
(ComputeFunction), so the handler-import test reads the CMD from the Dockerfile and
imports it here.
"""

import importlib
import json
import re
from typing import Any, Final

import pytest

from asteroidea_infra.api import CORS_METHODS
from asteroidea_infra.functions import (
    API_FUNCTIONS,
    FUNCTIONS,
    INGEST_FUNCTIONS,
    REPO_ROOT,
    FunctionSpec,
)
from asteroidea_ingest.routes import CURATORS, RESEARCHERS, ROUTES

DOCKERFILES: Final = (
    "ingest/Dockerfile",
    "ingest/Dockerfile.extract",
    "ingest/Dockerfile.compute",
)
CMD_RE: Final = re.compile(r"^CMD\s+(\[.*\])\s*$", re.MULTILINE)


def dockerfile_cmd(dockerfile: str) -> str:
    """Return the one handler string of the last exec-form CMD in `dockerfile`."""
    found = CMD_RE.findall((REPO_ROOT / dockerfile).read_text())
    assert found, f"{dockerfile}: no exec-form CMD"
    (handler,) = json.loads(found[-1])
    assert isinstance(handler, str), found[-1]
    return handler


def entry_point(spec: FunctionSpec[Any]) -> str:
    """Return what the runtime loads: the zip handler, the cmd override or the CMD."""
    if spec.image is None:
        return spec.handler
    if spec.image.cmd is not None:
        return spec.image.cmd
    return dockerfile_cmd(spec.image.dockerfile)


def import_failure(entry: str) -> str | None:
    """Import `entry`'s module; return why it is not a callable, or None."""
    module_name, _, attribute = entry.rpartition(".")
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        return f"{module_name}: {exc}"
    if not callable(getattr(module, attribute, None)):
        return f"{module_name} has no callable {attribute}"
    return None


@pytest.mark.parametrize("spec", FUNCTIONS, ids=lambda s: s.construct_id)
def test_every_handler_resolves(spec: FunctionSpec[Any]) -> None:
    entry = entry_point(spec)
    failure = import_failure(entry)
    assert failure is None, f"{spec.construct_id}: {entry}: {failure}"


@pytest.mark.parametrize("dockerfile", DOCKERFILES)
def test_every_dockerfile_cmd_resolves(dockerfile: str) -> None:
    entry = dockerfile_cmd(dockerfile)
    failure = import_failure(entry)
    assert failure is None, f"{dockerfile}: {entry}: {failure}"


@pytest.mark.parametrize(
    "spec",
    [s for s in FUNCTIONS if s.image is not None and s.image.cmd is None],
    ids=lambda s: s.construct_id,
)
def test_an_image_without_a_cmd_override_names_its_dockerfile_cmd(
    spec: FunctionSpec[Any],
) -> None:
    assert spec.image is not None
    assert spec.handler == dockerfile_cmd(spec.image.dockerfile), spec.construct_id


def test_a_missing_module_is_reported_with_its_name() -> None:
    failure = import_failure("asteroidea_ingest.no_such_module.handler")
    assert failure is not None
    assert "asteroidea_ingest.no_such_module" in failure


def test_construct_ids_and_integration_ids_are_unique() -> None:
    ids = [s.construct_id for s in FUNCTIONS]
    assert len(ids) == len(set(ids)), ids
    integrations = [s.integration_id for s in API_FUNCTIONS]
    assert None not in integrations
    assert len(integrations) == len(set(integrations)), integrations


def test_every_route_names_an_api_function_and_every_api_function_has_a_route() -> None:
    api = {s.construct_id for s in API_FUNCTIONS}
    routed = {r.function for r in ROUTES}
    assert not routed - api, f"routes name no API function: {sorted(routed - api)}"
    assert not api - routed, f"API functions with no route: {sorted(api - routed)}"


def test_route_keys_are_unique() -> None:
    keys = [r.key for r in ROUTES]
    assert len(keys) == len(set(keys)), sorted(k for k in keys if keys.count(k) > 1)


def test_route_methods_are_in_the_cors_set_and_groups_are_known() -> None:
    for route in ROUTES:
        assert route.method in CORS_METHODS, route.key
        assert route.group in {RESEARCHERS, CURATORS}, route.key
        assert route.path.startswith("/"), route.key
        assert route.summary, route.key


@pytest.mark.parametrize(
    "spec",
    [s for s in FUNCTIONS if s.image is not None and s.image.cmd is not None],
    ids=lambda s: s.construct_id,
)
def test_an_image_cmd_override_is_the_handler(spec: FunctionSpec[Any]) -> None:
    assert spec.image is not None
    assert spec.handler == spec.image.cmd, spec.construct_id


def test_the_registry_holds_all_twelve_functions() -> None:
    assert len(FUNCTIONS) == len(API_FUNCTIONS) + len(INGEST_FUNCTIONS) == 12
    overrides = [
        s.construct_id
        for s in FUNCTIONS
        if s.image is not None and s.image.cmd is not None
    ]
    # The four ImageConfig.Command overrides of the Ingest template.
    assert sorted(overrides) == [
        "DeriveFunction",
        "ImportFunction",
        "ImportPlanFunction",
        "LfpBuildFunction",
    ]


# Every API function's handler module serves through
# `api_common` and exports `DISPATCH`.
DISPATCHED: Final = tuple(s.construct_id for s in API_FUNCTIONS)
# A handler that hands some routes to another module's Api class: get_started_api's
# `handler` sends `/artifacts` to `artifacts.ArtifactsApi` and `/notes` to
# `notes_api.NotesApi`.
EXTRA_DISPATCH: Final = {
    "PlatformFunction": ("asteroidea_ingest.artifacts", "asteroidea_ingest.notes_api")
}


def dispatch_keys(spec: FunctionSpec[Any]) -> set[str]:
    """Return the union of `DISPATCH` keys of the modules serving `spec`'s routes."""
    modules = (
        spec.handler.rpartition(".")[0],
        *EXTRA_DISPATCH.get(spec.construct_id, ()),
    )
    keys: set[str] = set()
    for name in modules:
        dispatch = getattr(importlib.import_module(name), "DISPATCH", None)
        assert isinstance(dispatch, dict), f"{name} has no DISPATCH"
        keys |= {str(k) for k in dispatch}
    return keys


@pytest.mark.parametrize("function", DISPATCHED)
def test_every_route_has_a_handler_and_every_handler_a_route(
    function: str,
) -> None:
    (spec,) = [s for s in API_FUNCTIONS if s.construct_id == function]
    keys = dispatch_keys(spec)
    routes = {r.key for r in ROUTES if r.function == function}
    missing = ", ".join(sorted(routes - keys))
    assert not missing, f"{function}: routes without a handler: {missing}"
    stray = ", ".join(sorted(keys - routes))
    assert not stray, f"{function}: handlers without a route: {stray}"


def test_covers_the_six_api_functions() -> None:
    assert sorted(DISPATCHED) == [
        "CatalogFunction",
        "ComputeFunction",
        "ImportsFunction",
        "LabelsFunction",
        "PlatformFunction",
        "SessionFunction",
    ]
