"""Live test: re-extract builds the LFP derived objects.

Run with `make live T=tests/live/test_rf_lfp.py` after the deploy
(`make deploy SANDBOX_TEST=1`). Collected only under `make live` (`ASTEROIDEA_LIVE=1`);
run by hand only.

The test runs `make reextract ASSET=<CS62 R2>`, which re-extracts the asset
through the extract FIFO; the NWB extraction then queues one LFP build. It
waits for the build's `manifest.json` in the derived bucket within the queue
backlog allowance plus the builder's timeout, checks the manifest lists LFP_micro and
LFP_macro, and records the build wall time to `~/asteroidea-live/rf-lfp-<sha>.json`,
where `<sha>` is `git rev-parse HEAD` of this checkout: deploy that head first.
Nothing else is written to the sandbox beyond what `make reextract` writes.

A second check covers the two routes, run after the build test of the same module:
`/compute/overview` for LFP_micro returns 56 channels, and `/compute/spectrogram`
for channel 0, 60 s from t = 300 s returns inside the deadline. The first (cold)
call and the warm p50 of 10 window calls are merged into the same
`rf-lfp-<sha>.json`. Sign-in is the headless `sandbox-test` client (a
stand-in, the conftest `auth` fixture).
"""

import json
import statistics
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from asteroidea._http import send
from asteroidea_infra.admin import stack_outputs
from asteroidea_infra.runner import execute
from asteroidea_ingest import lfp_index, lfp_overview, timing

if TYPE_CHECKING:
    import boto3

    from asteroidea._auth import Auth

CS62_R2: Final = "1d50b507-a8a1-58d5-83a6-199f232a1582"
SERIES: Final = ("LFP_micro", "LFP_macro")
# The extract queue's wait plus one extraction, then one build at its timeout.
BUDGET_S: Final = timing.EXTRACT_BACKLOG_SECONDS + timing.LFP_BUILD_TIMEOUT_SECONDS
POLL_S: Final = 10.0
REEXTRACT_TIMEOUT_S: Final = 300.0
OUT_DIR: Final = Path.home() / "asteroidea-live"
REPO: Final = Path(__file__).resolve().parents[2]
MICRO_CHANNELS: Final = 56  # CS62 R2 LFP_micro
WINDOW_S: Final = 60
WINDOW_START_S: Final = 300
WARM_CALLS: Final = 10
OK: Final = 200
# compute.py answers 503 at the Lambda's remaining time minus 2 s (28 s timeout).
DEADLINE_S: Final = 26.0


def _head_sha() -> str:
    done = execute(["git", "-C", str(REPO), "rev-parse", "HEAD"], cwd=None)
    assert done.code == 0, done.err
    return done.out.strip()


def _fresh_manifest(s3: Any, bucket: str, since: datetime) -> dict[str, Any] | None:
    """Return the newest manifest of CS62 R2 written after ``since``, else None."""
    listed = s3.list_objects_v2(Bucket=bucket, Prefix=f"lfp/{CS62_R2}/")
    fresh = [
        o
        for o in listed.get("Contents", [])
        if o["Key"].endswith("/manifest.json") and o["LastModified"] >= since
    ]
    if not fresh:
        return None
    newest = max(fresh, key=lambda o: o["LastModified"])
    body = s3.get_object(Bucket=bucket, Key=newest["Key"])["Body"].read()
    return {"key": newest["Key"], "at": newest["LastModified"], **json.loads(body)}


def test_reextract_builds_the_lfp_index_and_overview_within_the_timeout(
    admin_session: boto3.Session,
) -> None:
    bucket = stack_outputs(admin_session, "DataStack")["DerivedBucketName"]
    s3 = admin_session.client("s3")
    started = datetime.now(UTC).replace(microsecond=0)
    clock = time.monotonic()
    done = execute(
        ["make", "reextract", f"ASSET={CS62_R2}"],
        cwd=REPO,
        timeout=REEXTRACT_TIMEOUT_S,
    )
    assert done.code == 0, (done.out + done.err)[-1200:]
    manifest = None
    while manifest is None and time.monotonic() - clock < BUDGET_S:
        time.sleep(POLL_S)
        manifest = _fresh_manifest(s3, bucket, started)
    wall_s = time.monotonic() - clock
    assert manifest is not None, (
        f"no manifest under lfp/{CS62_R2}/ within {BUDGET_S} s: read the "
        "LfpBuildFunction logs and the LfpBuildDlqUrl queue"
    )
    names = sorted(e["path"].rstrip("/").split("/")[-2] for e in manifest["series"])
    assert names == sorted(SERIES), (names, manifest["skipped"])
    for entry in manifest["series"]:
        index_blob = s3.get_object(Bucket=bucket, Key=entry["index_key"])["Body"].read()
        overview_blob = s3.get_object(Bucket=bucket, Key=entry["overview_key"])[
            "Body"
        ].read()
        assert len(index_blob) == entry["index_bytes"]
        assert len(overview_blob) == entry["overview_bytes"]
        index = lfp_index.loads(index_blob)
        overview = lfp_overview.loads(overview_blob)
        assert overview.db.shape[0] == index.shape[1]
    sha = _head_sha()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    record = {
        "sha": sha,
        "asset_id": CS62_R2,
        "manifest_key": manifest["key"],
        "reextract_to_manifest_s": wall_s,
        "manifest_written_after_start_s": (manifest["at"] - started).total_seconds(),
        "build_seconds": manifest["build_seconds"],
        "requests": manifest["requests"],
        "bytes_fetched": manifest["bytes_fetched"],
        "size_bytes": manifest["size_bytes"],
        "series": [
            {k: e[k] for k in ("path", "shape", "index_bytes", "overview_bytes")}
            for e in manifest["series"]
        ],
    }
    (OUT_DIR / f"rf-lfp-{sha}.json").write_text(json.dumps(record, indent=2))
    assert manifest["build_seconds"] < timing.LFP_BUILD_TIMEOUT_SECONDS


def _get(auth: Auth, path: str) -> tuple[int, dict[str, Any], float]:
    """GET ``path``; return the status, the JSON body and the wall seconds."""
    headers = {"Authorization": f"Bearer {auth.tokens().access_token}"}
    started = time.perf_counter()
    reply = send("GET", f"{auth.config.api_url.rstrip('/')}{path}", headers)
    seconds = time.perf_counter() - started
    return reply.status, json.loads(reply.body) if reply.body else {}, seconds


def _micro_series(admin_session: boto3.Session) -> str:
    """Return the ElectricalSeries path of LFP_micro from the newest manifest."""
    bucket = stack_outputs(admin_session, "DataStack")["DerivedBucketName"]
    s3 = admin_session.client("s3")
    listed = s3.list_objects_v2(Bucket=bucket, Prefix=f"lfp/{CS62_R2}/")
    keys = [o for o in listed.get("Contents", []) if o["Key"].endswith("manifest.json")]
    assert keys, "no manifest: run the build test of this module first"
    newest = max(keys, key=lambda o: o["LastModified"])
    body = s3.get_object(Bucket=bucket, Key=newest["Key"])["Body"].read()
    paths = [e["path"] for e in json.loads(body)["series"]]
    (micro,) = [p for p in paths if p.rstrip("/").split("/")[-2] == "LFP_micro"]
    return str(micro)


def test_overview_and_window_routes_answer_inside_the_deadline(
    auth: Auth, admin_session: boto3.Session
) -> None:
    """Overview: 56 channels. Window: channel 0, 60 s at t=300; cold call, warm p50."""
    series = _micro_series(admin_session)
    status, overview, overview_s = _get(
        auth, f"/compute/overview?asset_id={CS62_R2}&series={series}"
    )
    assert status == OK, overview
    if "url" in overview:  # above the body cap: a presigned GET of the binary object
        assert overview["shape"][0] == MICRO_CHANNELS, overview["shape"]
        assert overview["format"].startswith("ASTLFPOV")
        mode = "presigned"
    else:
        assert len(overview["channels"]) == MICRO_CHANNELS
        assert len(overview["power_db"]) == MICRO_CHANNELS
        mode = "json"
    path = (
        f"/compute/spectrogram?asset_id={CS62_R2}&series={series}&channel=0"
        f"&start_s={WINDOW_START_S}&duration_s={WINDOW_S}"
    )
    status, window, cold_s = _get(auth, path)
    assert status == OK, window
    assert cold_s < DEADLINE_S
    assert window["trace"]["values"], window.keys()
    warm: list[float] = []
    for _ in range(WARM_CALLS):
        status, window, seconds = _get(auth, path)
        assert status == OK, window
        warm.append(seconds)
    sha = _head_sha()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    target = OUT_DIR / f"rf-lfp-{sha}.json"
    record: dict[str, Any] = (
        json.loads(target.read_text()) if target.exists() else {"sha": sha}
    )
    record["overview"] = {"mode": mode, "seconds": overview_s}
    record["window"] = {
        "channel": 0,
        "start_s": WINDOW_START_S,
        "duration_s": WINDOW_S,
        "cold_s": cold_s,
        "warm_s": warm,
        "warm_p50_s": statistics.median(warm),
        "warm_max_s": max(warm),
        "bytes_read": window["bytes_read"],
        "requests": window["requests"],
        "server_fetch_ms": window["fetch_ms"],
        "server_compute_ms": window["compute_ms"],
    }
    target.write_text(json.dumps(record, indent=2))
    assert max(warm) < DEADLINE_S
