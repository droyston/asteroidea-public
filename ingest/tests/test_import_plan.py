"""ImportPlanFunction, offline.

The archives are the recorded `make import` fixtures (`infra/tests/fixtures/curate/`,
see `infra/tests/test_curate.py`); DynamoDB and Lambda are real
boto3 clients under botocore's Stubber, so every call is checked against the service
model and an unexpected call fails the test.
"""

import copy
import json
from pathlib import Path
from typing import Any, Final

import boto3
import pytest
from botocore.config import Config
from botocore.stub import ANY, Stubber

from asteroidea_ingest import import_file, import_plan, sessions
from asteroidea_ingest.import_plan import PlannerDeps, RunStore

from . import ddb_rules

REPO: Final = Path(__file__).resolve().parents[2]
FIXTURES: Final = REPO / "infra" / "tests" / "fixtures" / "curate"
DANDI_SOURCE: Final = "dandi:000623@0.240227.2023"
DANDI_BASE: Final = (
    "https://api.dandiarchive.org/api/dandisets/000623/versions/0.240227.2023"
)
RUNS: Final = "import-runs"
IMPORT_FN: Final = "asteroidea-import"
RUN_ID: Final = "run-1"
CALLER: Final = "curator-sub-1"
NOW: Final = 1_790_000_000


def _load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


class FakeWeb:
    """`import_plan.Web` over the recorded DANDI fixtures; `assets` may be replaced."""

    def __init__(self, n_assets: int | None = None) -> None:
        self.info = _load("dandi_000623_0.240227.2023_info.json")
        self.assets: dict[str, Any] = _load("dandi_000623_0.240227.2023_assets.json")
        self.regions = _load("bucket_regions.json")
        if n_assets is not None:
            first = self.assets["results"][0]
            results = []
            for i in range(n_assets):
                asset = copy.deepcopy(first)
                asset["path"] = f"sub-CS42/sub-CS42_ses-{i:03d}_ecephys.nwb"
                results.append(asset)
            self.assets = {"results": results, "next": None}
        self.calls: list[str] = []

    def get_json(self, url: str) -> dict[str, Any]:
        self.calls.append(url)
        if url == f"{DANDI_BASE}/info/":
            return copy.deepcopy(self.info)
        if url.startswith(f"{DANDI_BASE}/assets/"):
            return copy.deepcopy(self.assets)
        msg = f"unrecorded GET {url}"
        raise AssertionError(msg)

    def post_json(self, url: str, body: dict[str, Any]) -> dict[str, Any]:
        msg = f"unrecorded POST {url} {body}"
        raise AssertionError(msg)

    def list_objects(self, bucket: str, prefix: str) -> dict[str, int]:
        msg = f"unrecorded list {bucket} {prefix}"
        raise AssertionError(msg)

    def bucket_region(self, bucket: str) -> str:
        self.calls.append(f"region {bucket}")
        return str(self.regions[bucket])


def _client(service: str) -> Any:
    client = boto3.client(
        service,
        config=Config(signature_version="v4"),
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )
    return ddb_rules.install(client) if service == "dynamodb" else client


class Rig:
    """Stubbed DynamoDB and Lambda clients; records every write and every invoke."""

    def __init__(self, web: FakeWeb, existing: dict[str, str | None] | None = None):
        self.web = web
        self.ddb = _client("dynamodb")
        self.lam = _client("lambda")
        self.ddb_stub = Stubber(self.ddb)
        self.lam_stub = Stubber(self.lam)
        self.updates: list[dict[str, Any]] = []
        self.invokes: list[dict[str, Any]] = []
        self.ddb.meta.events.register(
            "before-parameter-build.dynamodb.UpdateItem",
            lambda params, **_: self.updates.append(params),
        )
        self.lam.meta.events.register(
            "before-parameter-build.lambda.Invoke",
            lambda params, **_: self.invokes.append(params),
        )
        self.existing = dict(existing or {})
        self.ddb_stub.activate()
        self.lam_stub.activate()

    def expect_updates(self, n: int) -> None:
        for _ in range(n):
            self.ddb_stub.add_response("update_item", {})

    def expect_invokes(self, n: int) -> None:
        for _ in range(n):
            self.lam_stub.add_response(
                "invoke",
                {"StatusCode": 202},
                expected_params={
                    "FunctionName": IMPORT_FN,
                    "InvocationType": "Event",
                    "Payload": ANY,
                },
            )

    def deps(self) -> PlannerDeps:
        return PlannerDeps(
            web=self.web,
            existing=lambda _ds: dict(self.existing),
            runs=RunStore(self.ddb, RUNS),
            lam=self.lam,
            import_function=IMPORT_FN,
            region="us-east-1",
            clock=lambda: float(NOW),
        )

    def run(
        self, include: list[str], source: str = DANDI_SOURCE, **extra: Any
    ) -> dict[str, Any]:
        event = {
            "run_id": RUN_ID,
            "source": source,
            "include": include,
            "caller": CALLER,
            **extra,
        }
        out = import_plan.plan_run(event, self.deps())
        self.ddb_stub.assert_no_pending_responses()
        self.lam_stub.assert_no_pending_responses()
        return out

    def written(self, n: int) -> dict[str, Any]:
        """Return the plain SET values of the n-th UpdateItem."""
        values = sessions.unmarshal(self.updates[n]["ExpressionAttributeValues"])
        return {k.removeprefix(":"): v for k, v in values.items()}


def _payloads(rig: Rig) -> list[dict[str, Any]]:
    return [json.loads(i["Payload"]) for i in rig.invokes]


# -- 50 files, all at once ---------------------------------------------------


def test_a_51_file_plan_is_refused_and_invokes_nothing() -> None:
    """Red proof: raise MAX_RUN_FILES to 51 and this test fails."""
    rig = Rig(FakeWeb(n_assets=51))
    rig.expect_updates(2)  # PLANNING, then REFUSED; no invoke response queued
    out = rig.run(["sub-CS42/*"])
    assert out["state"] == import_plan.REFUSED
    assert rig.invokes == []
    refused = rig.written(1)
    assert refused["state"] == import_plan.REFUSED
    assert refused["refusal"].startswith("over 50 files")
    assert "selects 51" in refused["refusal"]
    assert rig.updates[1]["ConditionExpression"] == "#state = :planning"
    # Refused before any bucket-region HEAD: only the two DANDI listing GETs ran.
    assert not [c for c in rig.web.calls if c.startswith("region")]


def test_50_files_are_invoked_all_at_once_one_async_invoke_each() -> None:
    rig = Rig(FakeWeb(n_assets=50))
    rig.expect_updates(2)
    rig.expect_invokes(50)
    out = rig.run(["sub-CS42/*"])
    assert out == {
        "run_id": RUN_ID,
        "state": import_plan.RUNNING,
        "invoked": 50,
        "already_imported": 0,
    }
    assert len(rig.invokes) == 50
    assert {i["InvocationType"] for i in rig.invokes} == {"Event"}
    running = rig.written(1)
    assert running["state"] == import_plan.RUNNING
    assert running["dataset"] == "dandi-000623"
    assert len(running["files"]) == 50
    # Each recorded session id is the one ImportFunction derives.
    for f, payload in zip(running["files"], _payloads(rig), strict=True):
        assert f["relpath"] == payload["relpath"]
        assert f["session_id"] == import_file.session_id(
            "dandi-000623", payload["relpath"], payload["source"]["version"]
        )


def test_each_payload_is_the_event_make_import_sends() -> None:
    """Same payload as `curate.run`: the planned event without `session_id`."""
    rig = Rig(FakeWeb())
    rig.expect_updates(2)
    rig.expect_invokes(1)
    rig.run(["sub-CS42/sub-CS42_ses-P42CSR2_behavior+ecephys.nwb"])
    (payload,) = _payloads(rig)
    assert set(payload) == {
        "mode",
        "dataset",
        "relpath",
        "size",
        "source",
        "dataset_meta",
        "caller",
        "ancillary",
    }
    assert payload["caller"] == CALLER
    assert payload["mode"] == import_file.MODE_HTTPS  # dandiarchive is us-east-2
    assert payload["dataset_meta"]["title"]
    assert payload["dataset_meta"]["license"]
    assert "tier" not in payload["dataset_meta"]  # no sandbox flag, no tier
    # ImportFunction accepts it.
    import_file.ImportEvent.parse(payload)


def test_a_sandbox_run_marks_each_files_dataset_meta_tier() -> None:
    """`sandbox` on the run becomes `dataset_meta.tier` on every file."""
    rig = Rig(FakeWeb(n_assets=3))
    rig.expect_updates(2)
    rig.expect_invokes(3)
    rig.run(["sub-CS42/*"], sandbox=True)
    payloads = _payloads(rig)
    assert len(payloads) == 3
    for payload in payloads:
        assert payload["dataset_meta"]["tier"] == "sandbox"
        assert payload["dataset_meta"]["title"]
        parsed = import_file.ImportEvent.parse(payload)
        assert parsed.dataset_meta["tier"] == "sandbox"


# -- the run item -----------------------------------------------------------------


def test_the_run_starts_planning_with_its_caller_for_the_by_caller_index() -> None:
    rig = Rig(FakeWeb())
    rig.expect_updates(2)
    rig.expect_invokes(1)
    rig.run(["sub-CS42/sub-CS42_ses-P42CSR2_behavior+ecephys.nwb"])
    start = rig.updates[0]
    assert start["TableName"] == RUNS
    assert sessions.unmarshal(start["Key"]) == {"run_id": RUN_ID}
    assert start["ConditionExpression"] == (
        "attribute_not_exists(#run_id) OR #state = :planning"
    )
    assert (
        "#created_at = if_not_exists(#created_at, :created_at)"
        in (start["UpdateExpression"])
    )
    values = rig.written(0)
    assert values["state"] == import_plan.PLANNING
    assert values["caller_sub"] == CALLER
    assert values["source"] == DANDI_SOURCE
    assert values["created_at"] == values["updated_at"] == NOW


def test_a_retry_after_planning_does_nothing() -> None:
    rig = Rig(FakeWeb())
    rig.ddb_stub.add_client_error(
        "update_item", service_error_code=sessions.CONDITIONAL_FAILED
    )
    out = rig.run(["sub-CS42/*"])
    assert out == {"run_id": RUN_ID, "outcome": import_plan.NOTHING_TO_DO}
    assert rig.invokes == []
    assert rig.web.calls == []


# -- plan-time refusals -----------------------------------------------------------


def _refusal(rig: Rig, include: list[str], source: str = DANDI_SOURCE) -> str:
    rig.expect_updates(2)
    out = rig.run(include, source)
    assert out["state"] == import_plan.REFUSED
    assert rig.invokes == []
    assert rig.written(1)["state"] == import_plan.REFUSED
    return str(rig.written(1)["refusal"])


def test_nothing_selected_is_refused() -> None:
    assert "selects no file" in _refusal(Rig(FakeWeb()), ["nothing/*"])


def test_no_licence_is_refused() -> None:
    web = FakeWeb()
    web.info["metadata"]["license"] = []
    assert "no license; nothing invoked" in _refusal(Rig(web), ["sub-CS42/*"])


def test_no_title_is_refused() -> None:
    web = FakeWeb()
    web.info["name"] = ""
    assert "no title" in _refusal(Rig(web), ["sub-CS42/*"])


def test_a_second_version_is_refused() -> None:
    path = "sub-CS42/sub-CS42_ses-P42CSR2_behavior+ecephys.nwb"
    rig = Rig(FakeWeb(), existing={path: "0.230101.0000"})
    assert import_plan.SECOND_VERSION in _refusal(rig, [path])


def test_an_unknown_source_is_refused() -> None:
    assert "unknown source" in _refusal(Rig(FakeWeb()), ["*"], source="figshare:1@1")


@pytest.mark.parametrize("include", ["sub-CS42/*", [1], None])
def test_a_malformed_include_is_refused(include: Any) -> None:
    rig = Rig(FakeWeb())
    rig.expect_updates(2)
    event = {
        "run_id": RUN_ID,
        "source": DANDI_SOURCE,
        "include": include,
        "caller": CALLER,
    }
    out = import_plan.plan_run(event, rig.deps())
    assert out["refusal"] == f"{import_plan.BAD_EVENT}"
    assert rig.invokes == []


@pytest.mark.parametrize("missing", ["run_id", "caller"])
def test_an_event_without_run_id_or_caller_raises_and_writes_nothing(
    missing: str,
) -> None:
    rig = Rig(FakeWeb())
    event = {"run_id": RUN_ID, "source": DANDI_SOURCE, "include": [], "caller": "c"}
    del event[missing]
    with pytest.raises(ValueError, match=missing):
        import_plan.plan_run(event, rig.deps())
    assert rig.updates == []


# -- already imported --------------------------------------------------------------


def test_already_imported_files_are_skipped_and_an_all_done_run_is_done() -> None:
    path = "sub-CS42/sub-CS42_ses-P42CSR2_behavior+ecephys.nwb"
    rig = Rig(FakeWeb(), existing={path: "0.240227.2023"})
    rig.expect_updates(2)
    out = rig.run([path])
    assert out["state"] == import_plan.DONE
    assert out["invoked"] == 0
    assert rig.invokes == []
    done = rig.written(1)
    assert done["files"] == []
    assert done["already_imported"] == [path]


def test_a_partly_imported_run_invokes_only_the_new_files() -> None:
    web = FakeWeb(n_assets=3)
    first = web.assets["results"][0]["path"]
    rig = Rig(web, existing={first: "0.240227.2023"})
    rig.expect_updates(2)
    rig.expect_invokes(2)
    out = rig.run(["sub-CS42/*"])
    assert out["invoked"] == 2
    assert first not in {p["relpath"] for p in _payloads(rig)}
    assert rig.written(1)["already_imported"] == [first]
