"""Live test: shared labels.

Run with `make live T=tests/live/test_rf_labels.py` after `make deploy` of the
stacks. Collected only under `make live` (`ASTEROIDEA_LIVE=1`); run by hand only.

Two sandbox-test researchers (the conftest `auth` and `researcher_b`): A posts, B lists
it as not its own, B's PATCH and DELETE are 403, A's PATCH and DELETE succeed. The
stored provenance must hold a snapshot id for each of `intervals`, `events` and
`asset_headers`: this is the live check that LabelsRole may read
`"<table>$snapshots"`. A null `snapshots` is a STOP: Athena or Glue refused the
metadata read, and the contract in ADR-0018 needs a decision. Stand-in:
sign-in is the headless `sandbox-test` client. The test deletes the label it creates.
"""

import json
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import quote

import pytest

import asteroidea
from asteroidea._http import send

if TYPE_CHECKING:
    from collections.abc import Iterator

    from asteroidea._auth import Auth

CS62_R2: Final = "1d50b507-a8a1-58d5-83a6-199f232a1582"
SNAPSHOT_TABLES: Final = ("intervals", "events", "asset_headers")
FORBIDDEN: Final = 403
NOT_FOUND: Final = 404
NO_CONTENT: Final = 204
TWO: Final = 2
LABEL: Final = {"name": "rf-labels live", "note": "", "start_s": 1.0, "stop_s": 2.5}


def _call(
    auth: Auth, method: str, path: str, body: dict[str, Any] | None = None
) -> tuple[int, dict[str, Any]]:
    headers = {"Authorization": f"Bearer {auth.tokens().access_token}"}
    payload = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        payload = json.dumps(body).encode()
    reply = send(method, _url(auth, path), headers, payload)
    return reply.status, json.loads(reply.body) if reply.body else {}


def _url(auth: Auth, path: str) -> str:
    return f"{auth.config.api_url.rstrip('/')}{path}"


@pytest.fixture
def created(auth: Auth) -> Iterator[dict[str, Any]]:
    """A's new label; deleted by A afterwards whatever the test did."""
    status, label = _call(auth, "POST", f"/files/{CS62_R2}/labels", LABEL)
    assert status == 201, label
    yield label
    _call(auth, "DELETE", f"/files/{CS62_R2}/labels/{label['label_id']}")


def test_b_lists_a_label_as_not_its_own(
    auth: Auth, researcher_b: Auth, created: dict[str, Any]
) -> None:
    assert created["mine"] is True
    status, body = _call(researcher_b, "GET", f"/files/{CS62_R2}/labels")
    assert status == 200, body
    (found,) = [x for x in body["labels"] if x["label_id"] == created["label_id"]]
    assert found["mine"] is False
    assert found["name"] == LABEL["name"]
    assert "author_sub" not in found
    _, mine = _call(auth, "GET", f"/files/{CS62_R2}/labels")
    assert next(x for x in mine["labels"] if x["label_id"] == created["label_id"])[
        "mine"
    ]


def test_sdk_reads_a_label_as_not_mine(
    researcher_b: Auth, created: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The SDK read: B's `asteroidea.labels(asset_id)` lists A's label."""
    monkeypatch.setattr("asteroidea._labels.default_auth", lambda: researcher_b)
    table = asteroidea.labels(CS62_R2)
    (found,) = [x for x in table.to_pylist() if x["label_id"] == created["label_id"]]
    assert found["mine"] is False
    assert found["name"] == LABEL["name"]
    assert json.loads(found["snapshots"]) == created["snapshots"]


def test_b_cannot_patch_or_delete_a_label(
    auth: Auth, researcher_b: Auth, created: dict[str, Any]
) -> None:
    path = f"/files/{CS62_R2}/labels/{created['label_id']}"
    edit = {**LABEL, "name": "hijacked", "version": created["version"]}
    assert _call(researcher_b, "PATCH", path, edit)[0] == FORBIDDEN
    assert _call(researcher_b, "DELETE", path)[0] == FORBIDDEN
    _, body = _call(auth, "GET", f"/files/{CS62_R2}/labels")
    kept = next(x for x in body["labels"] if x["label_id"] == created["label_id"])
    assert kept["name"] == LABEL["name"]
    assert kept["version"] == created["version"]


def test_a_patches_then_deletes(auth: Auth, created: dict[str, Any]) -> None:
    path = f"/files/{CS62_R2}/labels/{created['label_id']}"
    edit = {**LABEL, "name": "renamed", "version": created["version"]}
    status, body = _call(auth, "PATCH", path, edit)
    assert status == 200, body
    assert body["name"] == "renamed"
    assert body["version"] == created["version"] + 1
    assert _call(auth, "PATCH", path, edit)[0] == 409  # the version moved on
    assert _call(auth, "DELETE", path)[0] == NO_CONTENT
    assert _call(auth, "DELETE", path)[0] == NOT_FOUND


def test_provenance_snapshots_are_stored_not_null(created: dict[str, Any]) -> None:
    """Live check: LabelsRole read `"<table>$snapshots"` for all three tables."""
    snapshots = created["snapshots"]
    assert snapshots is not None, (
        "STOP: the $snapshots read was refused; store null and ask the maintainers "
        "whether ADR-0018's contract may be relaxed for labels"
    )
    for table in SNAPSHOT_TABLES:
        assert snapshots[table], (table, snapshots)
    assert created["s3_version_id"]


def test_rates_align_to_every_label_of_a_name(
    auth: Auth, researcher_b: Auth, created: dict[str, Any]
) -> None:
    """L4: `align=label_name:<name>` has one trial per label of that name (all authors).

    A's label comes from the fixture; B posts a second one of the same name, so the
    count crosses authors. The expected count is read from the labels list.
    """
    status, second = _call(researcher_b, "POST", f"/files/{CS62_R2}/labels", LABEL)
    assert status == 201, second
    try:
        _, listed = _call(auth, "GET", f"/files/{CS62_R2}/labels")
        expected = sum(x["name"] == LABEL["name"] for x in listed["labels"])
        assert expected >= TWO
        assert created["label_id"] in {x["label_id"] for x in listed["labels"]}
        name = quote(str(LABEL["name"]), safe="")
        status, body = _call(
            auth,
            "GET",
            f"/compute/rates?asset_id={CS62_R2}&units=0&align=label_name:{name}"
            "&pre_s=1&post_s=1&bin_s=0.5",
        )
        assert status == 200, body
        assert body["n_trials"] == expected
    finally:
        _call(researcher_b, "DELETE", f"/files/{CS62_R2}/labels/{second['label_id']}")
