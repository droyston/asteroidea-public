import json
from typing import TYPE_CHECKING

import pytest

from asteroidea import _labels
from asteroidea._http import Reply
from asteroidea.errors import (
    NotAResearcherError,
    PlatformUnreachableError,
    SignInRequiredError,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from asteroidea._auth import Auth

LABEL: dict[str, object] = {
    "label_id": "l1",
    "asset_id": "a/1",
    "name": "burst",
    "note": "n",
    "start_s": 1.0,
    "stop_s": 2.5,
    "series": "lfp",
    "mine": True,
    "created_at": 100,
    "updated_at": 101,
    "version": 2,
    "s3_version_id": "v1",
    "snapshots": {"intervals": "7", "events": None, "asset_headers": "9"},
    "dataset": "ds",
}


class Fake:
    def __init__(self, reply: Reply) -> None:
        self.reply = reply
        self.calls: list[tuple[str, str, str | None]] = []

    def __call__(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | Path | None,
    ) -> Reply:
        assert body is None
        self.calls.append((method, url, headers.get("Authorization")))
        return self.reply


def ok(*items: dict[str, object]) -> Reply:
    return Reply(200, json.dumps({"labels": list(items)}).encode())


def test_labels_reads_the_route_with_the_token_and_shapes_the_table(
    auth: Auth,
) -> None:
    fake = Fake(ok(LABEL))
    table = _labels.labels(auth, "a/1", fake)
    assert fake.calls == [
        (
            "GET",
            f"{auth.config.api_url.rstrip('/')}/files/a%2F1/labels",
            f"Bearer {auth.tokens().access_token}",
        )
    ]
    assert table.column_names == [
        "label_id",
        "name",
        "note",
        "start_s",
        "stop_s",
        "series",
        "mine",
        "created_at",
        "updated_at",
        "version",
        "s3_version_id",
        "snapshots",
    ]
    (row,) = table.to_pylist()
    assert row["label_id"] == "l1"
    assert row["mine"] is True
    assert row["stop_s"] == 2.5
    assert json.loads(row["snapshots"]) == LABEL["snapshots"]


def test_missing_series_and_null_snapshots_are_nulls(auth: Auth) -> None:
    bare = {k: v for k, v in LABEL.items() if k != "series"} | {"snapshots": None}
    (row,) = _labels.labels(auth, "x", Fake(ok(bare))).to_pylist()
    assert row["series"] is None
    assert row["snapshots"] is None


def test_no_labels_is_an_empty_typed_table(auth: Auth) -> None:
    table = _labels.labels(auth, "x", Fake(ok()))
    assert table.num_rows == 0
    assert "start_s" in table.column_names


@pytest.mark.parametrize(
    ("reply", "error"),
    [
        (Reply(401, b""), SignInRequiredError),
        (Reply(403, b"{}"), NotAResearcherError),
        (Reply(500, b""), PlatformUnreachableError),
        (Reply(404, b"{}"), PlatformUnreachableError),
    ],
)
def test_each_failure_maps_to_an_sdk_error(
    auth: Auth, reply: Reply, error: type[Exception]
) -> None:
    with pytest.raises(error):
        _labels.labels(auth, "x", Fake(reply))


def test_a_uniform_500_names_the_request_id(auth: Auth) -> None:
    text = "The platform failed on this request. Report request id r-3."
    body = json.dumps({"error": "internal_error", "detail": text}).encode()
    with pytest.raises(PlatformUnreachableError) as caught:
        _labels.labels(auth, "x", Fake(Reply(500, body)))
    assert str(caught.value) == text
