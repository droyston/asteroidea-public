"""Every request on every API route writes exactly one timing line.

The route list is the literal oracle `fixtures/routes.txt`, not `routes.ROUTES`, so a
route the table loses still fails here. Each call goes through the function's Lambda
entry point (the registry's `handler`) with `boto3.client` replaced by a client that
refuses every AWS call: a route answers its own refusal, or raises at its first AWS
call, and either way the line must be there, once, with the route's timing name.
"""

import importlib
import json
from pathlib import Path
from typing import Any, Final

import boto3
import pytest

from asteroidea_infra.functions import API_FUNCTIONS
from asteroidea_ingest import get_started_api, timing_log
from asteroidea_ingest.routes import ROUTES

ROUTES_ORACLE: Final = Path(__file__).parent / "fixtures" / "routes.txt"
KEYS: Final = [k for k in ROUTES_ORACLE.read_text().splitlines() if k.strip()]
ENV: Final = {
    "SESSIONS_TABLE": "sessions",
    "OWNERS_TABLE": "owners",
    "RAW_BUCKET": "raw",
    "COMMIT_QUEUE_URL": "https://sqs.example/commit",
    "ARTIFACTS_BUCKET": "artifacts",
    "SITE_BUCKET": "site",
    "WEB_URL": "https://app.example",
    "ATHENA_WORKGROUP": "wg",
    "GLUE_DATABASE": "db",
    "LABELS_TABLE": "labels",
    "DERIVED_BUCKET": "derived",
    "RUNS_TABLE": "runs",
    "NOTES_TABLE": "notes",
    "IMPORT_PLAN_FUNCTION_NAME": "planner",
    **{name: f"v-{key}" for key, name in get_started_api.PLATFORM_ENV.items()},
}
NO_ROUTE: Final = '{"error": "no such route"}'


class AwsCallError(RuntimeError):
    """A handler reached an AWS call; the test's client refuses it."""


class NoAws:
    """A boto3 client whose every call raises: this test makes no AWS call."""

    def __getattr__(self, name: str) -> Any:
        def refuse(*_args: object, **_kwargs: object) -> object:
            msg = f"no AWS call in this test: {name}"
            raise AwsCallError(msg)

        return refuse


class Context:
    """The two Lambda context members the handlers read."""

    aws_request_id = "req-per-route"

    def get_remaining_time_in_millis(self) -> int:
        return 29_000


def _event(key: str, group: str) -> dict[str, Any]:
    return {
        "routeKey": key,
        "pathParameters": None,
        "queryStringParameters": None,
        "body": None,
        "requestContext": {
            "domainName": "api.example",
            "authorizer": {
                "jwt": {"claims": {"sub": "user-1", "cognito:groups": f"[{group}]"}}
            },
        },
    }


def _lines(out: str) -> list[dict[str, Any]]:
    found = [json.loads(x) for x in out.splitlines() if x.startswith("{")]
    return [x for x in found if "timing" in x]


def test_the_oracle_holds_the_36_routes() -> None:
    assert len(KEYS) == len(set(KEYS)) == 36, KEYS


@pytest.mark.parametrize("key", KEYS)
def test_one_timing_line_per_call(
    key: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (route,) = [r for r in ROUTES if r.key == key]
    (spec,) = [s for s in API_FUNCTIONS if s.construct_id == route.function]
    module_name, _, attribute = spec.handler.rpartition(".")
    entry = getattr(importlib.import_module(module_name), attribute)
    monkeypatch.setattr(boto3, "client", lambda *_a, **_k: NoAws())
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)
    timing_log.reset()
    capsys.readouterr()
    try:
        reply = entry(_event(key, route.group), Context())
    except AwsCallError:
        status = 500
    else:
        assert reply["body"] != NO_ROUTE, f"{key}: no handler answered"
        status = int(reply["statusCode"])
    lines = _lines(capsys.readouterr().out)
    assert len(lines) == 1, f"{key}: {len(lines)} timing lines, expected 1"
    (line,) = lines
    assert line["timing"] == (route.timing or key), key
    assert line["status"] == status, key
    assert line["request_id"] == "req-per-route", key
    assert line["cold"] is True, key
    assert "user-1" not in json.dumps(line), key


def test_the_two_existing_timing_names_stay() -> None:
    named = {r.key: r.timing for r in ROUTES if r.timing}
    assert named == {
        "GET /catalog/assets/{asset_id}/download": "catalog_download",
        "POST /files/{asset_id}/labels": "labels_create",
    }
