import json
from typing import TYPE_CHECKING, Any

import pytest

import asteroidea
from asteroidea import _summary
from asteroidea._http import Reply
from asteroidea.errors import QueryError, SignInRequiredError

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from asteroidea._auth import Auth

ROUTE = "/catalog/session-summary?dataset=d1&subject=sub-CS62&session=ses-P62CSR2"
EVENTS: dict[str, Any] = {
    "asset_id": "a1",
    "dataset": "d1",
    "event_types": [
        {
            "name": "scene_cut",
            "averaging": "average",
            "n_events": 2,
            "events": [
                {"onset_s": 1.5, "stop_s": None, "attributes": {"cut_kind": "scene"}},
                {"onset_s": 4.0, "stop_s": None, "attributes": {"cut_kind": "cont"}},
            ],
        },
        {
            "name": "movie_onset",
            "averaging": "single",
            "n_events": 1,
            "events": [{"onset_s": 0.0, "stop_s": 1.0, "attributes": {}}],
        },
    ],
}
UNITS: dict[str, Any] = {
    "rows": [
        {"unit_index": 0, "location": "amygdala_left", "spike_count": 10},
        {"unit_index": 1, "location": "hippocampus_right", "spike_count": 4},
    ],
    "page": 0,
    "more": False,
}
RATES: dict[str, Any] = {
    "edges": [-1.0, 0.0, 1.0, 2.0],
    "units": [
        {"unit_index": 0, "rate_hz": [1, 2, 3], "z": [0, 1.5, -0.5]},
        {"unit_index": 1, "rate_hz": [0, 0, 0], "z": None},
    ],
    "n_trials": 2,
    "align": "event:scene_cut",
    "source": "catalog",
    "label": "exploratory screen",
    "defaults": {"version": "1", "baseline": "pre"},
    "responsive": [
        {
            "unit_index": 0,
            "location": "amygdala_left",
            "p": 0.01,
            "q": 0.02,
            "screened": True,
        },
    ],
}
MANIFEST: dict[str, Any] = {
    "schema": "asteroidea.session-summary/1.1",
    "dataset": "d1",
    "subject": "sub-CS62",
    "session": "ses-P62CSR2",
    "asset_id": "a1",
    "units_table": {"rows": 2, "more": False},
}
PARTS: dict[str, Any] = {
    "events": EVENTS,
    "units": UNITS,
    "rates-scene_cut": RATES,
    "tuning-scene_cut-cut_kind": {"units": []},
}


class Fake:
    def __init__(self, replies: Mapping[str, Reply]) -> None:
        self.replies = replies
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
        return self.replies[url]


def reply(body: object, status: int = 200) -> Reply:
    return Reply(status, json.dumps(body).encode())


def read(auth: Auth, fake: Fake) -> _summary.SessionSummary | None:
    return _summary.session_summary(auth, "d1", "sub-CS62", "ses-P62CSR2", fake)


def test_an_inline_summary_becomes_units_events_and_rates(auth: Auth) -> None:
    url = auth.config.api_url + ROUTE
    body = {
        "status": "built",
        "delivery": "inline",
        "manifest": MANIFEST,
        "parts": PARTS,
    }
    fake = Fake({url: reply(body)})
    got = read(auth, fake)
    assert got is not None
    assert fake.calls == [("GET", url, f"Bearer {auth.tokens().access_token}")]
    assert got.asset_id == "a1"
    assert got.units.column("location").to_pylist() == [
        "amygdala_left",
        "hippocampus_right",
    ]
    assert got.events.to_pylist()[0] == {
        "event_type": "scene_cut",
        "averaging": "average",
        "onset_s": 1.5,
        "stop_s": None,
        "attributes": '{"cut_kind": "scene"}',
    }
    assert got.events.num_rows == 3
    assert list(got.rates) == ["scene_cut"]
    rates = got.rates["scene_cut"]
    assert rates.edges == (-1.0, 0.0, 1.0, 2.0)
    assert rates.n_trials == 2
    assert rates.units.to_pylist() == [
        {"unit_index": 0, "rate_hz": [1.0, 2.0, 3.0], "z": [0.0, 1.5, -0.5]},
        {"unit_index": 1, "rate_hz": [0.0, 0.0, 0.0], "z": None},
    ]
    assert rates.responsive.column("q").to_pylist() == [0.02]
    assert rates.label == "exploratory screen"
    assert "tuning-scene_cut-cut_kind" in got.parts


def test_presigned_parts_are_fetched_without_the_sign_in_header(auth: Auth) -> None:
    url = auth.config.api_url + ROUTE
    signed = {
        name: f"https://bucket.example/{name}?X-Amz-Signature=s" for name in PARTS
    }
    body = {
        "status": "built",
        "delivery": "presigned",
        "manifest": MANIFEST,
        "part_urls": signed,
        "expires_in": 900,
    }
    replies = {url: reply(body)} | {signed[n]: reply(PARTS[n]) for n in PARTS}
    fake = Fake(replies)
    got = read(auth, fake)
    assert got is not None
    assert got.rates["scene_cut"].units.num_rows == 2
    assert got.units.num_rows == 2
    assert fake.calls[1:] == [("GET", signed[n], None) for n in PARTS]


def test_a_failed_presigned_part_names_the_part(auth: Auth) -> None:
    url = auth.config.api_url + ROUTE
    signed = {"units": "https://bucket.example/units?X-Amz-Signature=s"}
    body = {
        "status": "built",
        "delivery": "presigned",
        "manifest": MANIFEST,
        "part_urls": signed,
        "expires_in": 900,
    }
    fake = Fake({url: reply(body), signed["units"]: Reply(403, b"<Error/>")})
    with pytest.raises(QueryError, match="part units answered 403"):
        read(auth, fake)


def test_missing_is_none(auth: Auth) -> None:
    url = auth.config.api_url + ROUTE
    assert read(auth, Fake({url: reply({"status": "missing"})})) is None


def test_a_refusal_carries_the_route_code_and_detail(auth: Auth) -> None:
    url = auth.config.api_url + ROUTE
    refused = {"error": "summary_unreadable", "detail": "k: S3 AccessDenied"}
    fake = Fake({url: reply(refused, 502)})
    with pytest.raises(QueryError, match="502 summary_unreadable: k: S3 AccessDenied"):
        read(auth, fake)


def test_a_401_is_sign_in_required(auth: Auth) -> None:
    url = auth.config.api_url + ROUTE
    with pytest.raises(SignInRequiredError):
        read(auth, Fake({url: reply({"error": "unauthorized"}, 401)}))


def test_the_names_are_exported() -> None:
    for name in ("session_summary", "SessionSummary", "EventRates"):
        assert name in asteroidea.__all__
