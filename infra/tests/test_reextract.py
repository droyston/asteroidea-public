"""`make reextract`, offline: stubbed clients, SQLite."""

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, NoReturn, cast

import pytest
from botocore.exceptions import ClientError
from pyiceberg.catalog.sql import SqlCatalog

from asteroidea_infra import reextract, schema
from asteroidea_infra.catalog_init import iceberg_schema
from asteroidea_infra.reextract import Request
from asteroidea_ingest import commit, sessions
from asteroidea_ingest.catalog_writer import CatalogWriter

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from boto3 import Session
    from pyiceberg.catalog import Catalog

NOW: Final = 1_000_000
RUN: Final = "run-2"
NWB: Final = "sub-01/ses-A/ecephys/sub-01_ses-A_ecephys.nwb"
CSV: Final = "stimuli/info.csv"
EVENTS: Final = "sub-01/func/sub-01_task-x_events.tsv"
MACHINE_ARN: Final = "arn:aws:states:us-east-1:0:stateMachine:asteroidea-reextract"


class Forbidden:
    """A client stand-in that fails the test on any method it was not given."""

    def __getattr__(self, name: str) -> NoReturn:
        msg = f"unexpected call: {name}"
        raise AssertionError(msg)


class FakeDdb(Forbidden):
    """The sessions table: put (conditional on absence), get, scan, SET update."""

    def __init__(self) -> None:
        self.items: dict[str, dict[str, Any]] = {}
        self.puts = 0

    def put_item(self, **kw: Any) -> dict[str, Any]:
        item = sessions.unmarshal(kw["Item"])
        assert kw["ConditionExpression"] == "attribute_not_exists(session_id)"
        assert item["session_id"] not in self.items
        self.puts += 1
        self.items[item["session_id"]] = item
        return {}

    def get_item(self, **kw: Any) -> dict[str, Any]:
        item = self.items.get(sessions.unmarshal(kw["Key"])["session_id"])
        return {"Item": sessions.marshal(item)} if item is not None else {}

    def scan(self, **_kw: Any) -> dict[str, Any]:
        return {"Items": [sessions.marshal(i) for i in self.items.values()]}

    def update_item(self, **kw: Any) -> dict[str, Any]:
        # Only `SET #n = :v, ...`; the condition is the code under test's business.
        key = sessions.unmarshal(kw["Key"])["session_id"]
        values = sessions.unmarshal(kw["ExpressionAttributeValues"])
        for clause in kw["UpdateExpression"].removeprefix("SET ").split(", "):
            name, value = (part.strip() for part in clause.split("="))
            self.items[key][kw["ExpressionAttributeNames"][name]] = values[value]
        return {}


class FakeSfn(Forbidden):
    """The state machine; `worker` plays the Map (derive, then commit) per item."""

    def __init__(self, ddb: FakeDdb) -> None:
        self.ddb = ddb
        self.started: list[dict[str, Any]] = []
        self.worker: Callable[[dict[str, Any], FakeDdb], None] = _done
        self.error: Exception | None = None

    def start_execution(self, **kw: Any) -> dict[str, Any]:
        if self.error is not None:
            raise self.error
        self.started.append(kw)
        for item in json.loads(kw["input"])["assets"]:
            self.worker(item, self.ddb)
        return {"executionArn": f"{MACHINE_ARN}:{kw['name']}"}

    @property
    def sent(self) -> list[dict[str, Any]]:
        """Every Map item of every started execution, in order."""
        return [a for kw in self.started for a in json.loads(kw["input"])["assets"]]


def _done(body: dict[str, Any], ddb: FakeDdb) -> None:
    ddb.items[body["session_id"]] |= {
        "reextract_state": "DONE",
        "tables_replaced": ["electrodes", "asset_headers"],
    }


class FakeCfn(Forbidden):
    def describe_stacks(self, **_kw: Any) -> dict[str, Any]:
        outputs = [
            {"OutputKey": "ReextractStateMachineArn", "OutputValue": MACHINE_ARN}
        ]
        return {"Stacks": [{"Outputs": outputs}]}

    def describe_stack_resources(self, **_kw: Any) -> dict[str, Any]:
        resource = {
            "LogicalResourceId": "SessionsTable1",
            "ResourceType": "AWS::DynamoDB::Table",
            "PhysicalResourceId": "sessions",
        }
        return {"StackResources": [resource]}


@dataclass
class FakeSession:
    """Records every client name asked for; `lambda` fails the test."""

    ddb: FakeDdb = field(default_factory=FakeDdb)
    asked: list[str] = field(default_factory=list)
    sfn: FakeSfn = field(init=False)

    def __post_init__(self) -> None:
        self.sfn = FakeSfn(self.ddb)

    def client(self, name: str) -> Any:
        self.asked.append(name)
        assert name != "lambda", "make reextract must never create a Lambda client"
        clients = {"dynamodb": self.ddb, "stepfunctions": self.sfn}
        return (clients | {"cloudformation": FakeCfn()})[name]


def _row(
    asset: str, relpath: str, dataset: str = "ds1", **extra: Any
) -> dict[str, Any]:
    return {
        "asset_id": asset,
        "session_id": f"s-{asset}",
        "dataset": dataset,
        "s3_key": f"raw/{dataset}/{relpath}",
        "s3_version_id": "v1",
        "size_bytes": 100,
        "checksum_crc64nvme": "c",
        "extension": "." + relpath.split(".", 1)[1],
        "committed_at": commit.utc_now_naive(),
        "format": commit.file_format("." + relpath.split(".", 1)[1]),
        **extra,
    }


@pytest.fixture
def catalog(tmp_path: Path) -> Iterator[Catalog]:
    cat = SqlCatalog(
        "test",
        uri=f"sqlite:///{tmp_path}/catalog.db",
        warehouse=f"file://{tmp_path}/warehouse",
    )
    cat.create_namespace(schema.DATABASE)
    cat.create_table(f"{schema.DATABASE}.{schema.TABLE}", iceberg_schema(schema.ASSETS))
    writer = CatalogWriter(cat)
    for row in (
        _row("a1", NWB),
        _row("a2", CSV),
        _row("a3", EVENTS),
        _row("a4", NWB, dataset="ds2"),
    ):
        writer.append_if_absent(schema.TABLE, {"asset_id": row["asset_id"]}, [row])
    yield cat
    cat.engine.dispose()


def _deps(session: FakeSession, sleeps: list[float] | None = None) -> reextract.Deps:
    deps = reextract.connect(cast("Session", session), RUN)
    return reextract.Deps(
        table=deps.table,
        sfn=deps.sfn,
        state_machine_arn=deps.state_machine_arn,
        run_id=RUN,
        now=lambda: NOW,
        sleep=(sleeps if sleeps is not None else []).append,
    )


def _go(
    session: FakeSession,
    catalog: Catalog,
    request: Request,
    sleeps: list[float] | None = None,
) -> int:
    return reextract.run(
        reextract.list_assets(catalog, request), request, _deps(session, sleeps)
    )


def _ids(catalog: Catalog, request: Request) -> list[str]:
    return [str(r["asset_id"]) for r in reextract.list_assets(catalog, request)]


# -- listing --


def test_list_assets_filters_by_dataset_and_asset(catalog: Catalog) -> None:
    assert _ids(catalog, Request()) == ["a1", "a2", "a3", "a4"]
    assert _ids(catalog, Request(dataset="ds2")) == ["a4"]
    assert _ids(catalog, Request(asset="a3")) == ["a3"]
    assert _ids(catalog, Request(dataset="ds1", asset="a4")) == []


def test_the_map_item_carries_the_asset_fields() -> None:
    fields = reextract.asset_fields(_row("a1", NWB))
    assert fields == {
        "asset_id": "a1",
        "dataset": "ds1",
        "relpath": NWB,
        "s3_key": f"raw/ds1/{NWB}",
        "s3_version_id": "v1",
        "file_format": "nwb",
        "size": "100",
    }


# -- one execution per run; nothing invokes a function --


def test_the_run_uses_only_dynamodb_stepfunctions_and_cloudformation(
    catalog: Catalog,
) -> None:
    session = FakeSession()
    code = reextract.execute(
        cast("Session", session),
        catalog,
        Request(formats=("nwb",)),
        RUN,
    )
    assert code == 0
    assert set(session.asked) <= {"dynamodb", "stepfunctions", "cloudformation"}
    assert "lambda" not in session.asked
    assert "sqs" not in session.asked  # the state machine sends the pointers
    (started,) = session.sfn.started  # one execution for the run
    assert started["stateMachineArn"] == MACHINE_ARN
    assert started["name"] == f"{RUN}-0"
    body = json.loads(started["input"])
    assert body["run_id"] == RUN
    assert [m["asset_id"] for m in body["assets"]] == ["a1", "a4"]
    assert {m["session_id"] for m in body["assets"]} == {
        f"reextract:{RUN}:a1",
        f"reextract:{RUN}:a4",
    }
    assert {m["run_id"] for m in body["assets"]} == {RUN}


# -- the run --


def test_a_run_writes_one_tracking_item_per_asset_and_prints_pass(
    catalog: Catalog, capsys: pytest.CaptureFixture[str]
) -> None:
    session = FakeSession()
    assert _go(session, catalog, Request(dataset="ds1")) == 0
    out = capsys.readouterr().out
    assert "PASS a1" in out
    assert "PASS a3" in out
    assert f"SKIP a2 {CSV}: no extractor" in out  # a csv has no extractor
    assert "summary: 2 PASS, 0 FAIL, 1 SKIP" in out
    item = session.ddb.items[f"reextract:{RUN}:a1"]
    assert item["reextract_lease_until"] == NOW + 600
    assert item["expires_at"] == NOW + 7 * 24 * 3600
    assert not {"owner_sub", "created_at", "state", "extract_state"} & item.keys()


def test_a_failed_asset_names_the_tables_already_replaced_and_exits_1(
    catalog: Catalog, capsys: pytest.CaptureFixture[str]
) -> None:
    session = FakeSession()

    def fail(body: dict[str, Any], ddb: FakeDdb) -> None:
        ddb.items[body["session_id"]] |= {
            "reextract_state": "FAILED",
            "reextract_error": {"class": "RuntimeError", "message": "boom"},
            "tables_replaced": ["series", "units"],
        }

    session.sfn.worker = fail
    assert _go(session, catalog, Request(asset="a1")) == 1
    out = capsys.readouterr().out
    assert "FAIL a1" in out
    assert "RuntimeError: boom" in out
    assert "tables replaced before the failure: series, units" in out
    assert "summary: 0 PASS, 1 FAIL" in out


def test_the_cli_polls_every_ten_seconds_until_the_item_settles(
    catalog: Catalog,
) -> None:
    session = FakeSession()
    session.sfn.worker = lambda _body, _ddb: None  # stays QUEUED
    sleeps: list[float] = []
    clock = iter(range(NOW, NOW + 10_000, 700))
    deps = _deps(session, sleeps)
    deps = reextract.Deps(
        table=deps.table,
        sfn=deps.sfn,
        state_machine_arn=deps.state_machine_arn,
        run_id=RUN,
        now=lambda: next(clock),
        sleep=sleeps.append,
    )
    request = Request(asset="a1")
    code = reextract.run(reextract.list_assets(catalog, request), request, deps)
    assert code == 1  # never finished: FAIL after the timeout
    assert set(sleeps) == {10}


# -- DRY and filters --


def test_dry_run_lists_and_sends_nothing(
    catalog: Catalog, capsys: pytest.CaptureFixture[str]
) -> None:
    session = FakeSession()
    assert _go(session, catalog, Request(dry=True)) == 0
    out = capsys.readouterr().out
    assert "would send a1" in out
    assert "would send a4" in out
    assert "SKIP a2" in out
    assert session.sfn.sent == []
    assert session.ddb.puts == 0
    assert session.ddb.items == {}


def test_formats_limits_the_assets(catalog: Catalog) -> None:
    session = FakeSession()
    assert _go(session, catalog, Request(formats=("tsv",))) == 0
    assert [m["asset_id"] for m in session.sfn.sent] == ["a3"]


# -- the lease --


def _other_run(session: FakeSession, **fields: Any) -> str:
    item = sessions.reextract_item("run-1", "a1", NOW - 100) | fields
    session.ddb.items[item["session_id"]] = item
    return str(item["session_id"])


def test_another_runs_live_lease_refuses_the_asset_and_names_run_and_end(
    catalog: Catalog, capsys: pytest.CaptureFixture[str]
) -> None:
    session = FakeSession()
    _other_run(session)  # QUEUED, lease until NOW - 100 + 600
    assert _go(session, catalog, Request(asset="a1")) == 1
    out = capsys.readouterr().out
    assert "refused, run run-1 holds a live lease until 1000500" in out
    assert session.sfn.sent == []
    assert session.ddb.puts == 0


def test_force_expires_that_lease_and_then_sends(
    catalog: Catalog, capsys: pytest.CaptureFixture[str]
) -> None:
    session = FakeSession()
    old = _other_run(session)
    assert _go(session, catalog, Request(asset="a1", force=True)) == 0
    out = capsys.readouterr().out
    assert f"FORCE {old}: lease expired" in out
    assert session.ddb.items[old]["reextract_lease_until"] == NOW - 1
    assert [m["asset_id"] for m in session.sfn.sent] == ["a1"]


def test_an_expired_lease_a_finished_item_and_my_own_run_do_not_refuse(
    catalog: Catalog,
) -> None:
    session = FakeSession()
    _other_run(session, reextract_lease_until=NOW - 1)  # expired
    done = sessions.reextract_item("run-0", "a1", NOW)
    done["reextract_state"] = "DONE"  # finished: its lease is moot
    session.ddb.items[done["session_id"]] = done
    assert _go(session, catalog, Request(asset="a1")) == 0
    assert len(session.sfn.sent) == 1


def test_a_live_lease_on_another_asset_does_not_refuse(catalog: Catalog) -> None:
    session = FakeSession()
    item = sessions.reextract_item("run-1", "a4", NOW)
    session.ddb.items[item["session_id"]] = item
    assert _go(session, catalog, Request(asset="a1")) == 0


def test_force_in_a_dry_run_expires_nothing(catalog: Catalog) -> None:
    session = FakeSession()
    old = _other_run(session)
    lease = session.ddb.items[old]["reextract_lease_until"]
    assert _go(session, catalog, Request(asset="a1", force=True, dry=True)) == 1
    assert session.ddb.items[old]["reextract_lease_until"] == lease


# -- the start: one execution per run, chunked at the Step Functions limits --


def _rows(count: int, relpath: str = NWB) -> list[dict[str, Any]]:
    return [_row(f"a{i:04d}", relpath) for i in range(count)]


def test_a_run_of_more_than_500_assets_starts_one_execution_per_500(
    capsys: pytest.CaptureFixture[str],
) -> None:
    session = FakeSession()
    deps = _deps(session)
    assert reextract.run(_rows(1001), Request(), deps) == 0
    started = session.sfn.started
    assert [kw["name"] for kw in started] == [f"{RUN}-0", f"{RUN}-1", f"{RUN}-2"]
    sizes = [len(json.loads(kw["input"])["assets"]) for kw in started]
    assert sizes == [500, 500, 1]
    assert "summary: 1001 PASS, 0 FAIL" in capsys.readouterr().out


def _items(count: int, relpath: str) -> list[dict[str, str]]:
    return [
        {"session_id": f"reextract:{RUN}:{r['asset_id']}", "run_id": RUN}
        | reextract.asset_fields(r)
        for r in _rows(count, relpath)
    ]


def test_long_path_items_split_by_bytes_and_each_input_fits() -> None:
    """A 190-character relpath: about 580 B an item, so 500 exceed 256 KiB."""
    relpath = "sub-" + "x" * 150 + "/ses-A/ecephys/sub-01_ses-A_ecephys.nwb"
    items = _items(1000, relpath)
    assert 550 < len(json.dumps(items[0]).encode()) < 620
    with pytest.raises(ValueError, match="over 262144"):
        reextract.execution_input(RUN, items[:500])
    parts = reextract.chunks(RUN, items)
    assert [i for part in parts for i in part] == items  # every item, in order
    sizes = [len(reextract.execution_input(RUN, part).encode()) for part in parts]
    assert all(size <= reextract.MAX_INPUT_BYTES for size in sizes)
    assert all(size > reextract.MAX_INPUT_BYTES - 700 for size in sizes[:-1])  # full
    assert len(parts) == 3


def test_an_item_too_large_alone_fails_only_its_own_execution(
    capsys: pytest.CaptureFixture[str],
) -> None:
    session = FakeSession()
    rows = [*_rows(2), _row("big", "sub-01/" + "y" * 300_000 + ".nwb")]
    assert reextract.run(rows, Request(), _deps(session)) == 1
    assert [len(json.loads(kw["input"])["assets"]) for kw in session.sfn.started] == [2]
    out = capsys.readouterr().out
    assert "PASS a0000" in out
    assert "FAIL big" in out
    assert "ValueError: the execution did not start: execution input is" in out


def test_an_execution_that_cannot_start_fails_its_assets_with_the_error(
    catalog: Catalog, capsys: pytest.CaptureFixture[str]
) -> None:
    session = FakeSession()
    session.sfn.error = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "no"}},
        "StartExecution",
    )
    assert _go(session, catalog, Request(formats=("nwb",))) == 1
    out = capsys.readouterr().out
    assert "FAIL a1" in out
    assert "FAIL a4" in out
    assert "ClientError: the execution did not start" in out
    assert "AccessDeniedException" in out
    item = session.ddb.items[f"reextract:{RUN}:a1"]
    assert item["reextract_state"] == "FAILED"
    assert item["tables_replaced"] == []


def test_a_run_with_nothing_to_send_starts_no_execution(catalog: Catalog) -> None:
    session = FakeSession()
    assert _go(session, catalog, Request(formats=("csv",))) == 0
    assert session.sfn.started == []
