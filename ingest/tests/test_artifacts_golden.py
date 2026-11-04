"""The results bodies the web parses, as golden files.

Two researchers save three results over the assets of the batch-lookup golden: a
baseline, a rates result that used it and two files, and a tuning result that used
the rates result. The All listing and the rates record are written to
`web/e2e/fixtures/ux/` (regenerate with `GOLDEN_WRITE=1`).
"""

import json
import uuid
from typing import TYPE_CHECKING, Any, Final

from asteroidea_ingest import artifacts

from .conftest import CRC, jwt_event
from .test_artifacts import FakeBucket, api
from .test_catalog_batch import EVENTS, NWB
from .test_catalog_studies import check_golden

if TYPE_CHECKING:
    import pytest

ADA, GRACE = "sub-ada", "sub-grace"
IDS: Final = [
    "0b6f3e52-5a1d-4c3e-9f10-1a2b3c4d5e01",
    "0b6f3e52-5a1d-4c3e-9f10-1a2b3c4d5e02",
    "0b6f3e52-5a1d-4c3e-9f10-1a2b3c4d5e03",
]
DAY = 86_400
OCT_12 = 1_791_800_000


def _send(
    s3: FakeBucket, route: str, sub: str, now: int, **kw: Any
) -> tuple[int, dict[str, Any]]:
    event = jwt_event(route, sub=sub, body=kw.get("body"), sid=kw.get("aid"))
    event["queryStringParameters"] = kw.get("query")
    claims = event["requestContext"]["authorizer"]["jwt"]["claims"]
    claims["email"] = f"{sub.removeprefix('sub-')}@example.test"
    resp = api(s3).handle(event, now=now)
    return int(resp["statusCode"]), json.loads(resp["body"])


def _save(s3: FakeBucket, sub: str, now: int, body: dict[str, Any]) -> str:
    full = {"schema": artifacts.SCHEMA_V2, "crc64nvme": CRC, **body}
    status, out = _send(s3, "POST /artifacts", sub, now, body=full)
    assert status == 201, out
    aid = str(out["artifact_id"])
    key = f"artifacts/{sub}/{aid}/data/{body['name']}"
    s3.heads[key] = {"ContentLength": body["size"], "ChecksumCRC64NVME": CRC}
    status, _ = _send(s3, "POST /artifacts/{id}/finalize", sub, now, aid=aid)
    assert status == 200
    return aid


def results_bucket(monkeypatch: pytest.MonkeyPatch) -> FakeBucket:
    ids = iter(IDS)
    monkeypatch.setattr(artifacts.uuid, "uuid4", lambda: uuid.UUID(next(ids)))
    s3 = FakeBucket()
    run = {"run_id": "r-1", "sdk_version": "0.9.0", "defaults_version": "2026.10"}
    baseline = _save(
        s3,
        ADA,
        OCT_12,
        {
            "name": "baseline-rates.csv",
            "size": 2048,
            "inputs": [{"kind": "asset", "id": NWB, "version": "3HL4kqcurrent"}],
            "run": {**run, "function": "rates", "parameters": {"bin_s": 0.1}},
        },
    )
    rates = _save(
        s3,
        ADA,
        OCT_12 + 2 * DAY,
        {
            "name": "spike-rates.csv",
            "size": 3_145_728,
            "note": "Rates around scene cuts.",
            "inputs": [
                {
                    "kind": "asset",
                    "id": NWB,
                    "version": "3HL4kqcurrent",
                    "role": "spikes",
                },
                {
                    "kind": "asset",
                    "id": EVENTS,
                    "version": "9KJ2xWcurrent",
                    "role": "alignment",
                },
                {"kind": "artifact", "id": baseline, "role": "baseline"},
                {"kind": "table_snapshot", "id": "units", "version": 8123456789},
            ],
            "run": {
                **run,
                "function": "analysis.rates",
                "parameters": {"event": "scene_cut", "bin_s": 0.1, "window_s": 3},
            },
        },
    )
    _save(
        s3,
        GRACE,
        OCT_12 + 2 * DAY + 600,
        {
            "name": "tuning-curves.csv",
            "size": 4096,
            "inputs": [{"kind": "artifact", "id": rates, "role": "rates"}],
            "run": {**run, "function": "tuning", "parameters": {"attribute": "face"}},
        },
    )
    return s3


def test_all_listing_matches_the_golden(monkeypatch: pytest.MonkeyPatch) -> None:
    s3 = results_bucket(monkeypatch)
    status, body = _send(s3, "GET /artifacts", ADA, OCT_12, query={"scope": "all"})
    assert status == 200
    assert [r["source_asset_ids"] for r in body["artifacts"]] == [
        [],
        [NWB, EVENTS],
        [NWB],
    ]
    check_golden("artifacts-all.golden.json", body)


def test_record_matches_the_golden(monkeypatch: pytest.MonkeyPatch) -> None:
    s3 = results_bucket(monkeypatch)
    status, body = _send(s3, "GET /artifacts/{id}", GRACE, OCT_12, aid=IDS[1])
    assert status == 200
    assert body["record"]["author_email"] == "ada@example.test"
    check_golden("artifact-record.golden.json", body)


def test_used_by_listing_matches_the_golden(monkeypatch: pytest.MonkeyPatch) -> None:
    s3 = results_bucket(monkeypatch)
    query = {"scope": "all", "input": IDS[1]}
    status, body = _send(s3, "GET /artifacts", ADA, OCT_12, query=query)
    assert status == 200
    assert [r["name"] for r in body["artifacts"]] == ["tuning-curves.csv"]
    check_golden("artifacts-used-by.golden.json", body)
