"""The shared handler kit: caller, refusals, one timing line."""

import base64
import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from asteroidea_ingest import api_common, timing_log
from asteroidea_ingest.api_common import (
    ApiError,
    Invocation,
    Request,
    Response,
    Timing,
    claim_groups,
    error_response,
    json_response,
    serve,
)

UPLOADS = "GET /uploads"  # a researchers route
IMPORTS = "GET /imports"  # a curators route
DOWNLOAD = "GET /catalog/assets/{asset_id}/download"  # timing="catalog_download"


def _event(route: str, claims: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {
        "routeKey": route,
        "requestContext": {"authorizer": {"jwt": {"claims": claims}}},
        **extra,
    }


def _lines(out: str) -> list[dict[str, Any]]:
    found = [json.loads(x) for x in out.splitlines() if x.startswith("{")]
    return [x for x in found if "timing" in x]


@dataclass
class Probe:
    """An Api whose one method records the Request and answers with `answer`."""

    answer: Response = field(default_factory=lambda: json_response(200, {}))
    raises: Exception | None = None
    seen: list[Request] = field(default_factory=list)

    def probe(self, request: Request, timing: Timing) -> Response:
        self.seen.append(request)
        with timing.phase("work"):
            pass
        if self.raises is not None:
            raise self.raises
        return self.answer


def _serve(probe: Probe, event: dict[str, Any], **kwargs: Any) -> Response:
    route = str(event["routeKey"])
    return serve(event, api=probe, dispatch={route: "probe"}, **kwargs)


# -- the caller -------------------------------------------------------------------


def test_an_id_token_shaped_caller_passes_and_yields_the_email() -> None:
    claims = {
        "sub": "user-1",
        "aud": "sdk-client-id",
        "email": "researcher@example.org",
        "token_use": "id",
        "cognito:groups": "[researchers]",
    }
    probe = Probe()
    resp = _serve(probe, _event(UPLOADS, claims))
    assert resp["statusCode"] == 200
    (request,) = probe.seen
    assert request.email == "researcher@example.org"
    assert request.sub == "user-1"
    assert request.groups == frozenset({"researchers"})


def test_an_access_token_has_no_email() -> None:
    claims = {
        "sub": "user-1",
        "client_id": "web-client-id",
        "token_use": "access",
        "cognito:groups": "[researchers]",
    }
    probe = Probe()
    _serve(probe, _event(UPLOADS, claims))
    assert probe.seen[0].email is None


@pytest.mark.parametrize(
    ("raw", "groups"),
    [
        ("[researchers curators]", {"researchers", "curators"}),
        ("[researchers,curators]", {"researchers", "curators"}),
        (["curators"], {"curators"}),
        ("", set()),
    ],
)
def test_claim_groups_reads_every_rendering(raw: object, groups: set[str]) -> None:
    assert claim_groups({"cognito:groups": raw}) == groups


def test_the_request_carries_path_query_domain_and_clock() -> None:
    probe = Probe()
    event = _event(
        "GET /sessions/{id}",
        {"sub": "u", "cognito:groups": "[researchers]"},
        pathParameters={"id": "s-1"},
        queryStringParameters={"page": "2"},
        body=None,
    )
    event["requestContext"]["domainName"] = "api.example"
    _serve(probe, event, invocation=Invocation(request_id="r-1").at(1234))
    (request,) = probe.seen
    assert request.path("id") == "s-1"
    assert request.query == {"page": "2"}
    assert request.domain == "api.example"
    assert request.now == 1234
    assert request.request_id == "r-1"
    assert request.route.key == "GET /sessions/{id}"


# -- refusals are {error: code, detail: sentence} -----------------------------------


def test_an_unknown_route_is_a_404_before_any_claim_is_read() -> None:
    resp = serve({"routeKey": "GET /nowhere"}, api=Probe(), dispatch={})
    assert resp["statusCode"] == 404
    assert resp["body"] == '{"error": "not_found", "detail": "no such route"}'


def test_a_known_route_outside_the_dispatch_is_a_404() -> None:
    event = _event(UPLOADS, {"sub": "u", "cognito:groups": "[researchers]"})
    resp = serve(event, api=Probe(), dispatch={})
    assert resp["statusCode"] == 404
    assert json.loads(resp["body"]) == {"error": "not_found", "detail": "no such route"}


def test_a_caller_outside_the_route_group_is_a_403_naming_it() -> None:
    probe = Probe()
    resp = _serve(
        probe, _event(IMPORTS, {"sub": "u", "cognito:groups": "[researchers]"})
    )
    assert resp["statusCode"] == 403
    assert json.loads(resp["body"]) == {
        "error": "forbidden",
        "detail": "caller is not in the curators group",
    }
    assert probe.seen == []


def test_an_api_error_body_is_exactly_its_code_and_detail() -> None:
    err = ApiError(403, "not yours", code="dataset_not_yours")
    resp = error_response(err)
    assert resp == {
        "statusCode": 403,
        "headers": {"content-type": "application/json"},
        "body": '{"error": "dataset_not_yours", "detail": "not yours"}',
    }
    assert ApiError(403, "x").code == "forbidden"
    assert {s: ApiError(s, "x").code for s in api_common.CODES} == api_common.CODES


def test_a_raised_api_error_is_its_body() -> None:
    probe = Probe(raises=ApiError(410, "session expired"))
    resp = _serve(
        probe, _event(UPLOADS, {"sub": "u", "cognito:groups": "[researchers]"})
    )
    assert resp["statusCode"] == 410
    assert resp["body"] == '{"error": "gone", "detail": "session expired"}'


def test_any_other_exception_is_a_500_naming_the_request_id_and_one_log_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    probe = Probe(raises=KeyError("boom"))
    resp = _serve(
        probe,
        _event(UPLOADS, {"sub": "u", "cognito:groups": "[researchers]"}),
        invocation=Invocation(request_id="req-9"),
    )
    assert resp["statusCode"] == 500
    assert json.loads(resp["body"]) == {
        "error": "internal_error",
        "detail": "The platform failed on this request. Report request id req-9.",
    }
    out = capsys.readouterr().out
    (line,) = _lines(out)
    assert (line["status"], line["request_id"]) == (500, "req-9")
    (crash,) = [json.loads(x) for x in out.splitlines() if x.startswith('{"unhandled"')]
    assert (crash["unhandled"], crash["request_id"], crash["route"]) == (
        "KeyError",
        "req-9",
        UPLOADS,
    )
    assert "KeyError: 'boom'" in crash["traceback"]


def test_json_body_decodes_base64_and_refuses_with_the_imports_wording() -> None:
    probe = Probe()
    claims = {"sub": "u", "cognito:groups": "[researchers]"}
    encoded = base64.b64encode(b'{"a": 1}').decode()
    _serve(probe, _event(UPLOADS, claims, body=encoded, isBase64Encoded=True))
    _serve(probe, _event(UPLOADS, claims, body="!!", isBase64Encoded=True))
    _serve(probe, _event(UPLOADS, claims, body="[1]"))
    good, bad64, array = probe.seen
    assert good.json_body() == {"a": 1}
    with pytest.raises(ApiError, match="the body is not valid base64 text"):
        bad64.json_body()
    with pytest.raises(ApiError, match="the body must be a JSON object"):
        array.json_body()
    with pytest.raises(ApiError, match="id is required") as caught:
        good.path("id")
    assert caught.value.status == 400


def test_parse_json_keeps_the_upload_wording() -> None:
    with pytest.raises(ApiError, match=r"^body must be JSON$"):
        api_common.parse_json("{")
    with pytest.raises(ApiError, match=r"^body must be a JSON object$"):
        api_common.parse_json("[1]")
    assert api_common.parse_json('{"a": 1}') == {"a": 1}


# -- one timing line per request -----------------------------------------------------


def test_every_answered_request_writes_one_timing_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    timing_log.reset()
    claims = {"sub": "secret-sub", "cognito:groups": "[researchers]"}
    _serve(Probe(), _event(UPLOADS, claims), invocation=Invocation(request_id="r-9"))
    _serve(Probe(), _event(IMPORTS, claims))
    out = capsys.readouterr().out
    first, refused = _lines(out)
    assert first["timing"] == UPLOADS
    assert first["status"] == 200
    assert first["request_id"] == "r-9"
    assert first["cold"] is True
    assert set(first["phases"]) == {"work_ms", "total_ms"}
    assert all(isinstance(v, int) for v in first["phases"].values())
    assert "asset_id" not in first
    assert refused["status"] == 403
    assert refused["request_id"] == "local"
    assert refused["cold"] is False
    assert "secret-sub" not in out


def test_a_route_timing_name_asset_and_cache_reach_the_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    @dataclass
    class Download:
        def probe(self, _request: Request, timing: Timing) -> Response:
            timing.asset_id, timing.cache = "a-1", "hit"
            return json_response(200, {})

    claims = {"sub": "u", "cognito:groups": "[researchers]"}
    serve(_event(DOWNLOAD, claims), api=Download(), dispatch={DOWNLOAD: "probe"})
    (line,) = _lines(capsys.readouterr().out)
    assert line["timing"] == "catalog_download"
    assert line["asset_id"] == "a-1"
    assert line["cache"] == "hit"


def test_invocation_reads_a_lambda_context() -> None:
    @dataclass
    class Context:
        aws_request_id: str = "req-7"

        def get_remaining_time_in_millis(self) -> int:
            return 29_000

    got = Invocation.of(Context())
    assert got == Invocation(request_id="req-7", remaining_ms=29_000)
    assert Invocation.of(None) == Invocation()
    assert got.at(None) is got
    assert got.at(5).now == 5
