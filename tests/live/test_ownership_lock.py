"""Live test: dataset ownership and the raw Object Lock.

Run with `make live T=tests/live/test_ownership_lock.py` after the deploy.
Every test touches the sandbox.

Stand-in: sign-in is the headless `sandbox-test` client (the conftest
`auth` fixture for researcher A; the same flow for researcher B).

Not tested here: a delete of the locked raw version. The raw bucket policy
already refuses every delete except CommitRole's, and CommitRole trusts only the Lambda
service, so no test holds its credentials; delete enforcement is proven on a scratch
bucket.

Left behind: the locked raw version of researcher A's file cannot be deleted for the
retention period (1 day in the sandbox, `RAW_RETENTION_DAYS`), so the raw bucket keeps
that object, and the owners table keeps the claim rows of the fresh dataset name and
of `dandi-000623`. Researcher B is deleted at the end of the module.

Red proof: with the lock code reverted, the retention test (step b) finds no
retention (`get_object_retention` raises `NoSuchObjectLockConfiguration`).
"""

import importlib.util
import secrets
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, NamedTuple

import pytest

from asteroidea import _query, _sample, _upload
from asteroidea._http import Reply, send
from asteroidea.errors import DatasetNotYoursError
from asteroidea_infra import reclaim
from asteroidea_infra.admin import raw_bucket_name, stack_outputs
from asteroidea_ingest import sessions

if TYPE_CHECKING:
    from collections.abc import Mapping

    import boto3

    from asteroidea._auth import Auth


def _conftest() -> Any:
    """Load the conftest by path: the one module that cannot use its own loader."""
    path = Path(__file__).with_name("conftest.py")
    spec = importlib.util.spec_from_file_location("_oa_conftest", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_INGEST: Final = vars(_conftest())["sibling"]("test_ingest_core")
Live = _INGEST.Live
invocations = _INGEST.invocations
report = _INGEST.report

EXEMPLAR: Final = "dandi-000623"
TOLERANCE: Final = timedelta(minutes=5)
LOGS_WITHIN: Final = 60.0
NOT_YOURS: Final = "belongs to another researcher"


class Owned(NamedTuple):
    """Researcher A's first upload: its result, row and the start of the run."""

    dataset: str
    path: Path
    result: _upload.UploadResult
    row: dict[str, Any]
    started_ms: int


class Calls:
    """A transport that records every request and sends it on."""

    def __init__(self) -> None:
        self.seen: list[tuple[str, str]] = []

    def __call__(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | Path | None,
    ) -> Reply:
        self.seen.append((method, url))
        return send(method, url, headers, body)

    def puts(self) -> list[str]:
        """Return the URLs of the PUT requests (the landing uploads)."""
        return [url for method, url in self.seen if method == "PUT"]


@pytest.fixture(scope="module")
def owned(auth: Auth) -> Owned:
    """Step a: researcher A uploads one small file to a fresh dataset; it commits."""
    dataset, path = _sample.write_sample("lock")
    started_ms = int(time.time() * 1000)
    result = _upload.Uploader(auth).upload(path, dataset, sandbox=True)
    found = _query.query(auth, dataset=dataset)
    assert [r["asset_id"] for r in found.rows] == [result.asset_id]
    return Owned(dataset, path, result, found.rows[0], started_ms)


def retention_days(admin: boto3.Session, function: str) -> int:
    """Read the deployed commit function's `RAW_RETENTION_DAYS`."""
    config = admin.client("lambda").get_function_configuration(FunctionName=function)
    return int(config["Environment"]["Variables"]["RAW_RETENTION_DAYS"])


def test_the_raw_version_is_locked_to_committed_at_plus_retention(
    admin_session: boto3.Session, owned: Owned
) -> None:
    """Step b (the test's red): governance retention equals committed_at plus N days."""
    outputs = stack_outputs(admin_session)
    days = retention_days(admin_session, outputs["CommitFunctionName"])
    row = owned.row
    kept = admin_session.client("s3").get_object_retention(
        Bucket=raw_bucket_name(admin_session),
        Key=row["s3_key"],
        VersionId=row["s3_version_id"],
    )["Retention"]
    committed = row["committed_at"]
    committed_at = (
        committed.replace(tzinfo=UTC) if committed.tzinfo is None else committed
    )
    until = kept["RetainUntilDate"]
    drift = abs(until - (committed_at + timedelta(days=days)))
    report(
        f"OA lock: {kept['Mode']} until {until.isoformat()}, committed_at "
        f"{committed_at.isoformat()}, RAW_RETENTION_DAYS {days}, drift {drift}"
    )
    assert kept["Mode"] == "GOVERNANCE"
    assert drift <= TOLERANCE, (until, committed_at, days)
    assert until > datetime.now(UTC) - TOLERANCE


def test_another_researcher_is_refused_and_nothing_lands(
    researcher_b: Auth, owned: Owned
) -> None:
    """Step c: B uploading to A's dataset gets the server's text; no landing PUT."""
    calls = Calls()
    with pytest.raises(DatasetNotYoursError) as raised:
        _upload.Uploader(researcher_b, transport=calls).upload(
            owned.path, owned.dataset, sandbox=True
        )
    assert NOT_YOURS in str(raised.value)
    assert calls.puts() == [], calls.seen


def test_the_owner_uploads_again_to_the_same_dataset(auth: Auth, owned: Owned) -> None:
    """Step d: A continues in its own dataset (a second file, a new path)."""
    result = _upload.Uploader(auth).upload(
        owned.path,
        owned.dataset,
        relpath=f"{owned.path.parent.name}-again/{owned.path.name}",
        sandbox=True,
    )
    assert result.dataset == owned.dataset
    assert result.asset_id != owned.result.asset_id


def test_a_dataset_that_predates_ownership_is_refused_as_admin_owned(
    auth: Auth, tmp_path: Path
) -> None:
    """Step e: an exemplar dataset has raw objects and no owner row (admin-owned)."""
    path = tmp_path / f"probe-{secrets.token_hex(3)}.txt"
    path.write_text("ownership probe")
    calls = Calls()
    with pytest.raises(DatasetNotYoursError) as raised:
        _upload.Uploader(auth, transport=calls).upload(
            path, EXEMPLAR, relpath=f"probe/{path.name}", sandbox=True
        )
    assert NOT_YOURS in str(raised.value)
    assert calls.puts() == [], calls.seen


def test_log_whether_the_first_commit_was_a_cold_start(
    admin_session: boto3.Session, owned: Owned
) -> None:
    """Step f: log the first commit's `Init Duration`; no assertion."""
    outputs = stack_outputs(admin_session)
    function = outputs["CommitFunctionName"]
    table = sessions.SessionTable(
        admin_session.client("dynamodb"), reclaim.sessions_table_name(admin_session)
    )
    # `invocations` reads only `.admin`; the extract name is not used here.
    shell = Live(admin_session, outputs, table, function, function)
    deadline = time.monotonic() + LOGS_WITHIN
    found = invocations(shell, function, owned.started_ms - 60_000)
    while not found and time.monotonic() < deadline:
        time.sleep(5)
        found = invocations(shell, function, owned.started_ms - 60_000)
    if not found:
        report(
            f"OA cold start: no REPORT line within {LOGS_WITHIN:.0f} s (not asserted)"
        )
        return
    first = found[0]
    kind = "cold start" if first.init_ms is not None else "warm"
    report(
        f"OA cold start: first commit since the run began was {kind}"
        f"{'' if first.init_ms is None else f', Init Duration {first.init_ms} ms'}"
        " (not asserted)"
    )
