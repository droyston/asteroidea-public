"""SDK artifact records, bodies and refusals."""

import importlib.metadata
import json
from typing import TYPE_CHECKING, Any

import pytest

import asteroidea
from asteroidea import _crc
from asteroidea._artifacts import (
    ArtifactInfo,
    ArtifactRecord,
    ArtifactResult,
    Artifacts,
)
from asteroidea._client import PlatformClient
from asteroidea._http import Reply
from asteroidea._query import FileRef
from asteroidea._run import Run, active
from asteroidea.errors import (
    ArtifactNotFoundError,
    ArtifactRefusedError,
    HasDependentsError,
    IdTokenRequiredError,
    NotAResearcherError,
    NotTheAuthorError,
    PlatformUnreachableError,
    SignInRequiredError,
    UploadIncompleteError,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from asteroidea._auth import Auth

PUT_URL = "https://bucket.example.test/artifacts/u/a1/data/psth.png?sig=x"
GET_URL = "https://bucket.example.test/artifacts/u/a1/data/psth.png?sig=g"
SUMMARY = {
    "artifact_id": "a1",
    "name": "psth.png",
    "size": 7,
    "content_type": "application/octet-stream",
    "snapshot_id": 42,
    "state": "COMMITTED",
    "created_at": 100,
    "finalized_at": 101,
}


class Fake:
    """A stub platform: records the calls and plays one reply per route."""

    def __init__(self, api: str, **replies: Reply) -> None:
        self.api = api
        self.replies = {
            "create": Reply(
                201, json.dumps({"artifact_id": "a1", "url": PUT_URL}).encode()
            ),
            "put": Reply(200, b""),
            "finalize": Reply(200, b'{"state": "COMMITTED"}'),
            "list": Reply(200, json.dumps({"artifacts": [SUMMARY]}).encode()),
            "get": Reply(
                200,
                json.dumps({"record": SUMMARY, "url": GET_URL}).encode(),
            ),
        } | replies
        self.sent: dict[str, Any] = {}
        self.calls: list[tuple[str, str]] = []

    def __call__(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | Path | None,
    ) -> Reply:
        self.calls.append((method, url))
        if url == PUT_URL:
            self.sent["put_headers"] = dict(headers)
            return self.replies["put"]
        self.sent.setdefault("auth", headers.get("Authorization"))
        route = {
            ("POST", f"{self.api}/artifacts"): "create",
            ("POST", f"{self.api}/artifacts/a1/finalize"): "finalize",
            ("GET", f"{self.api}/artifacts"): "list",
            ("GET", f"{self.api}/artifacts/a1"): "get",
        }[method, url]
        if route == "create":
            assert isinstance(body, bytes)
            self.sent["create"] = json.loads(body)
        return self.replies[route]


class Downloads:
    """A stub download seam: writes bytes to the destination."""

    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.urls: list[str] = []

    def __call__(self, url: str, dest: Path) -> int:
        self.urls.append(url)
        if self.status == 200:
            dest.write_bytes(b"png-data")
        return self.status


REF = FileRef("asset-1", "ds", "sub-01/x.nwb", "raw/ds/sub-01/x.nwb", "v1", 10)


@pytest.fixture
def result_file(tmp_path: Path) -> Path:
    path = tmp_path / "psth.png"
    path.write_bytes(b"1234567")
    return path


def save(auth: Auth, fake: Fake, path: Path) -> ArtifactResult:
    return Artifacts(auth, fake).save(
        path, sources=[REF], snapshot_id=42, note="a grid"
    )


def test_save_creates_puts_with_the_checksum_and_finalizes(
    auth: Auth, result_file: Path
) -> None:
    fake = Fake(auth.config.api_url)
    result = save(auth, fake, result_file)
    assert fake.sent["create"] == {
        "name": "psth.png",
        "size": 7,
        "crc64nvme": _crc.crc64nvme(result_file),
        "snapshot_id": 42,
        "sources": [{"asset_id": "asset-1", "s3_version_id": "v1"}],
        "note": "a grid",
        # The version 1 body gains these two fields and nothing else.
        "content_type": "image/png",
        "sdk_version": importlib.metadata.version("asteroidea"),
    }
    assert "schema" not in fake.sent["create"]
    assert fake.sent["put_headers"] == {_crc.HEADER: _crc.crc64nvme(result_file)}
    assert fake.sent["auth"] == f"Bearer {auth.tokens().access_token}"
    assert [m for m, _ in fake.calls] == ["POST", "PUT", "POST"]
    assert (result.artifact_id, result.name, result.size, result.snapshot_id) == (
        "a1",
        "psth.png",
        7,
        42,
    )


@pytest.mark.parametrize(
    ("replies", "error"),
    [
        ({"create": Reply(401, b"")}, SignInRequiredError),
        ({"create": Reply(403, b"{}")}, NotAResearcherError),
        (
            {"create": Reply(403, b'{"error": "not_author", "detail": "Only the a"}')},
            NotTheAuthorError,
        ),
        (
            {"create": Reply(401, b'{"error": "id_token_required", "detail": "x"}')},
            IdTokenRequiredError,
        ),
        ({"create": Reply(500, b"")}, PlatformUnreachableError),
        ({"put": Reply(403, b"")}, UploadIncompleteError),
        (
            {"finalize": Reply(409, b'{"error": "upload_incomplete", "detail": "x"}')},
            UploadIncompleteError,
        ),
        ({"finalize": Reply(404, b"{}")}, ArtifactNotFoundError),
        ({"finalize": Reply(500, b"")}, PlatformUnreachableError),
    ],
)
def test_save_maps_each_failure(
    auth: Auth, result_file: Path, replies: dict[str, Reply], error: type[Exception]
) -> None:
    with pytest.raises(error):
        save(auth, Fake(auth.config.api_url, **replies), result_file)


def test_a_400_carries_the_server_text(auth: Auth, result_file: Path) -> None:
    body = b'{"error": "invalid_request", "detail": "sources must be a list"}'
    fake = Fake(auth.config.api_url, create=Reply(400, body))
    with pytest.raises(ArtifactRefusedError, match="sources must be a list") as caught:
        save(auth, fake, result_file)
    assert "invalid_request" not in str(caught.value)


def test_a_uniform_500_names_the_request_id(auth: Auth, result_file: Path) -> None:
    text = "The platform failed on this request. Report request id r-7."
    body = json.dumps({"error": "internal_error", "detail": text}).encode()
    fake = Fake(auth.config.api_url, finalize=Reply(500, body))
    with pytest.raises(PlatformUnreachableError) as caught:
        save(auth, fake, result_file)
    assert str(caught.value) == text


def test_artifacts_parses_the_list(auth: Auth) -> None:
    found = Artifacts(auth, Fake(auth.config.api_url)).listing()
    assert found == [
        ArtifactInfo(
            "a1", "psth.png", 7, "application/octet-stream", 42, "COMMITTED", 100, 101
        )
    ]


def test_artifacts_keeps_a_null_finalized_at(auth: Auth) -> None:
    row = SUMMARY | {"state": "PENDING", "finalized_at": None}
    fake = Fake(
        auth.config.api_url, list=Reply(200, json.dumps({"artifacts": [row]}).encode())
    )
    assert Artifacts(auth, fake).listing()[0].finalized_at is None


def test_download_writes_the_file_to_a_path_and_to_a_folder(
    auth: Auth, tmp_path: Path
) -> None:
    downloads = Downloads()
    client = Artifacts(auth, Fake(auth.config.api_url), downloads)
    target = tmp_path / "out.png"
    assert client.download("a1", target) == target
    assert target.read_bytes() == b"png-data"
    folder = tmp_path / "dir"
    folder.mkdir()
    assert client.download("a1", folder) == folder / "psth.png"
    assert downloads.urls == [GET_URL, GET_URL]


@pytest.mark.parametrize(
    "reply",
    [
        Reply(404, b'{"error": "not_found", "detail": "no such artifact"}'),
        Reply(
            200,
            json.dumps(
                {"record": SUMMARY | {"state": "PENDING"}, "url": GET_URL}
            ).encode(),
        ),
    ],
)
def test_download_refuses_a_missing_or_unfinished_record(
    auth: Auth, tmp_path: Path, reply: Reply
) -> None:
    downloads = Downloads()
    with pytest.raises(ArtifactNotFoundError):
        Artifacts(auth, Fake(auth.config.api_url, get=reply), downloads).download(
            "a1", tmp_path / "x"
        )
    assert downloads.urls == []
    assert not (tmp_path / "x").exists()


def test_a_failed_download_is_unreachable(auth: Auth, tmp_path: Path) -> None:
    client = Artifacts(auth, Fake(auth.config.api_url), Downloads(status=403))
    with pytest.raises(PlatformUnreachableError):
        client.download("a1", tmp_path / "x")


def test_artifacts_keeps_a_null_snapshot_id(auth: Auth) -> None:
    # A version 2 record saved without a query() snapshot.
    row = SUMMARY | {"snapshot_id": None}
    fake = Fake(
        auth.config.api_url, list=Reply(200, json.dumps({"artifacts": [row]}).encode())
    )
    assert Artifacts(auth, fake).listing()[0].snapshot_id is None


def test_not_author_is_not_a_researcher_refusal(auth: Auth, result_file: Path) -> None:
    body = b'{"error": "not_author", "detail": "Only the author can withdraw it."}'
    fake = Fake(auth.config.api_url, create=Reply(403, body))
    with pytest.raises(NotTheAuthorError, match="Only the author") as caught:
        save(auth, fake, result_file)
    assert not isinstance(caught.value, NotAResearcherError)


def test_id_token_required_says_to_sign_in_again(auth: Auth, result_file: Path) -> None:
    body = b'{"error": "id_token_required", "detail": "The token has no email claim."}'
    fake = Fake(auth.config.api_url, create=Reply(401, body))
    with pytest.raises(IdTokenRequiredError, match="sign_in"):
        save(auth, fake, result_file)


# Bodies shaped like main's `ingest/src/asteroidea_ingest/artifacts.py`
# (`list_records`, `get`, `withdraw`, `delete`, `_authored`; every refusal is
# `{"error": <code>, "detail": <sentence>}`).
X = "3f0c8a8e-5b7e-4d0a-9c39-2f0d6c1b9a11"
Y = "9b2d7c41-0f3e-4a6b-8c5d-1e2f3a4b5c6d"
AUTHOR = "r1@example.invalid"
SHARED = SUMMARY | {"artifact_id": X, "snapshot_id": None, "author_email": AUTHOR}
RECORD_V2 = {
    "schema": "asteroidea.provenance/2",
    **SHARED,
    "author_sub": "sub-r1",
    "data_key": f"artifacts/sub-r1/{X}/data/psth.png",
    "crc64nvme": "AAAAAAAAAAA=",
    "sources": [],
    "note": "first pass",
    "sdk_version": "0.1.0",
    "inputs": [{"kind": "table_snapshot", "id": "asteroidea.units", "version": 7}],
    "run": {"run_id": "r", "function": "rates", "parameters": {}},
}
NOT_AUTHOR = {
    "error": "not_author",
    "detail": "Only the author of this result can change, withdraw or delete it.",
}


def _json(status: int, body: object) -> Reply:
    return Reply(status, json.dumps(body).encode())


class Routes:
    """A stub platform keyed on (method, path with query): one reply each."""

    def __init__(self, api: str, replies: Mapping[tuple[str, str], Reply]) -> None:
        self.api = api
        self.replies = dict(replies)
        self.calls: list[tuple[str, str]] = []

    def __call__(
        self,
        method: str,
        url: str,
        _headers: Mapping[str, str],
        _body: bytes | Path | None,
    ) -> Reply:
        path = url.removeprefix(self.api)
        self.calls.append((method, path))
        return self.replies[method, path]


def _getter(auth: Auth, record: Mapping[str, Any]) -> Routes:
    body = _json(200, {"record": record, "url": GET_URL})
    return Routes(auth.config.api_url, {("GET", f"/artifacts/{X}"): body})


def test_artifacts_all_and_input_send_the_query_and_keep_the_author(
    auth: Auth,
) -> None:
    rows = _json(200, {"artifacts": [SHARED]})
    routes = Routes(
        auth.config.api_url,
        {
            ("GET", "/artifacts?scope=all"): rows,
            ("GET", f"/artifacts?scope=all&input={X}"): rows,
            ("GET", "/artifacts?input=asset%2F1"): rows,
        },
    )
    client = Artifacts(auth, routes)
    assert client.listing("all")[0].author_email == AUTHOR
    assert client.listing("all", X)[0].artifact_id == X
    assert client.listing(input_id="asset/1")[0].snapshot_id is None
    assert routes.calls == [
        ("GET", "/artifacts?scope=all"),
        ("GET", f"/artifacts?scope=all&input={X}"),
        ("GET", "/artifacts?input=asset%2F1"),
    ]


def test_my_listing_sends_no_scope_and_has_no_author(auth: Auth) -> None:
    rows = _json(200, {"artifacts": [SUMMARY]})
    routes = Routes(auth.config.api_url, {("GET", "/artifacts"): rows})
    assert Artifacts(auth, routes).listing()[0].author_email is None


@pytest.mark.parametrize(
    ("scope", "input_id", "query", "body"),
    [
        (
            "every",
            None,
            "scope=every",
            {"error": "invalid_scope", "detail": "scope must be mine or all"},
        ),
        (
            "mine",
            "",
            "input=",
            {
                "error": "invalid_input",
                "detail": "input must be an id of 1 to 256 characters",
            },
        ),
    ],
)
def test_a_refused_listing_carries_the_server_text(
    auth: Auth, scope: str, input_id: str | None, query: str, body: dict[str, str]
) -> None:
    reply = _json(400, body)
    routes = Routes(auth.config.api_url, {("GET", f"/artifacts?{query}"): reply})
    with pytest.raises(ArtifactRefusedError, match=body["detail"]):
        Artifacts(auth, routes).listing(scope, input_id)


def test_open_returns_another_authors_record_and_records_it_in_a_run(
    auth: Auth,
) -> None:
    with Run("follow-up") as r:
        opened = Artifacts(auth, _getter(auth, RECORD_V2)).open(X)
    assert opened == ArtifactRecord(
        X, "psth.png", "COMMITTED", AUTHOR, "first pass", RECORD_V2
    )
    assert list(r.inputs.values()) == [{"kind": "artifact", "id": X, "role": "opened"}]


def test_open_outside_a_run_records_nothing(auth: Auth) -> None:
    assert active() is None
    opened = Artifacts(auth, _getter(auth, RECORD_V2)).open(X)
    assert opened.record["schema"] == "asteroidea.provenance/2"
    assert active() is None


def test_open_of_an_unknown_id_is_not_found(auth: Auth) -> None:
    body = {"error": "not_found", "detail": "no such artifact"}
    routes = Routes(auth.config.api_url, {("GET", f"/artifacts/{X}"): _json(404, body)})
    with Run("follow-up") as r, pytest.raises(ArtifactNotFoundError):
        Artifacts(auth, routes).open(X)
    assert r.inputs == {}


def test_download_of_a_withdrawn_result_works_and_is_recorded_in_a_run(
    auth: Auth, tmp_path: Path
) -> None:
    withdrawn = RECORD_V2 | {"state": "WITHDRAWN"}
    downloads = Downloads()
    with Run("follow-up") as r:
        target = Artifacts(auth, _getter(auth, withdrawn), downloads).download(
            X, tmp_path
        )
    assert target == tmp_path / "psth.png"
    assert target.read_bytes() == b"png-data"
    assert [i["id"] for i in r.inputs.values()] == [X]


def test_a_failed_download_records_nothing(auth: Auth, tmp_path: Path) -> None:
    client = Artifacts(auth, _getter(auth, RECORD_V2), Downloads(status=403))
    with Run("follow-up") as r, pytest.raises(PlatformUnreachableError):
        client.download(X, tmp_path / "x")
    assert r.inputs == {}


def test_withdraw_posts_and_returns_the_new_state(auth: Auth) -> None:
    done = _json(200, {"state": "WITHDRAWN", "artifact_id": X})
    routes = Routes(auth.config.api_url, {("POST", f"/artifacts/{X}/withdraw"): done})
    assert Artifacts(auth, routes).withdraw(X) == "WITHDRAWN"
    assert routes.calls == [("POST", f"/artifacts/{X}/withdraw")]


@pytest.mark.parametrize(
    ("reply", "error", "text"),
    [
        (_json(403, NOT_AUTHOR), NotTheAuthorError, "Only the author"),
        (
            _json(404, {"error": "not_found", "detail": "no such artifact"}),
            ArtifactNotFoundError,
            "No saved result",
        ),
        (
            _json(
                409,
                {
                    "error": "upload_pending",
                    "detail": "This result is not finalized yet. Finalize it first.",
                },
            ),
            ArtifactRefusedError,
            "not finalized yet",
        ),
    ],
)
def test_withdraw_maps_each_refusal(
    auth: Auth, reply: Reply, error: type[Exception], text: str
) -> None:
    routes = Routes(auth.config.api_url, {("POST", f"/artifacts/{X}/withdraw"): reply})
    with pytest.raises(error, match=text) as caught:
        Artifacts(auth, routes).withdraw(X)
    assert not isinstance(caught.value, NotAResearcherError)


def test_has_dependents_is_its_own_error_naming_them(auth: Auth) -> None:
    detail = f"Other results used this one: {Y}. Withdraw it instead."
    reply = _json(409, {"error": "has_dependents", "detail": detail})
    routes = Routes(auth.config.api_url, {("DELETE", f"/artifacts/{X}"): reply})
    api = PlatformClient(auth.config.api_url, "t", routes)
    with pytest.raises(HasDependentsError, match=Y) as caught:
        api.call("DELETE", f"/artifacts/{X}")
    assert isinstance(caught.value, ArtifactRefusedError)
    assert "withdraw_artifact" in str(HasDependentsError())


def test_another_409_is_left_to_the_caller(auth: Auth) -> None:
    reply = _json(409, {"error": "upload_incomplete", "detail": "x"})
    route = f"/artifacts/{X}/finalize"
    api = PlatformClient(
        auth.config.api_url, "t", Routes(auth.config.api_url, {("POST", route): reply})
    )
    assert api.call("POST", route).status == 409


def test_the_package_exports_the_result_functions() -> None:
    names = ("artifacts", "open_artifact", "download_artifact", "withdraw_artifact")
    assert set(names) <= set(asteroidea.__all__)
    assert asteroidea.HasDependentsError is HasDependentsError
    assert asteroidea.ArtifactRecord is ArtifactRecord
