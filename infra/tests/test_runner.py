"""The kit the LIVE runners share (`runner.py`): report, commit, sign-in and API calls.

`execute`, `must` and `last_json` keep their tests in `test_acceptance.py`. Here the
helpers factored out of the runners run against a fake `urllib3` and a fake AWS session.
"""

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest
import urllib3

from asteroidea_infra import runner
from asteroidea_infra.runner import Proc
from asteroidea_infra.steps import (
    Console,
    Report,
    StepFailureError,
    StepResult,
)

if TYPE_CHECKING:
    from pathlib import Path

    from boto3 import Session


def new_run(tmp_path: Path) -> runner.Run:
    console = Console(headless=True, read=lambda _p: "", write=lambda _t: None)
    run = runner.Run(console, "r1", tmp_path, "profile", "000000000000")
    run.outputs.update({"ApiUrl": "https://api.example/", "access_token": "tok"})
    return run


def fake_request(
    monkeypatch: pytest.MonkeyPatch, status: int = 200, data: bytes = b"{}"
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def request(method: str, url: str, **kwargs: Any) -> SimpleNamespace:
        calls.append({"method": method, "url": url, **kwargs})
        return SimpleNamespace(status=status, data=data)

    monkeypatch.setattr(urllib3, "request", request)
    return calls


def test_commit_sha_is_the_short_sha_of_the_repo_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[dict[str, Any]] = []

    def fake(cmd: list[str], **kwargs: Any) -> Proc:
        seen.append({"cmd": cmd, **kwargs})
        return Proc(0, "abc1234\n", "")

    monkeypatch.setattr(runner, "execute", fake)
    assert runner.commit_sha() == "abc1234"
    assert seen[0]["cmd"] == ["git", "rev-parse", "--short", "HEAD"]
    assert seen[0]["cwd"] == runner.REPO


def test_write_report_puts_report_md_in_the_run_folder(tmp_path: Path) -> None:
    run = new_run(tmp_path)
    report = Report("r1", "headless", "abc1234")
    report.results = [StepResult("Sign in", passed=True, detail="ok", seconds=1.0)]
    path = runner.write_report(run, report)
    assert path == tmp_path / "report.md"
    assert path.read_text() == report.markdown()


def test_write_report_appends_the_tail_and_ends_with_one_newline(
    tmp_path: Path,
) -> None:
    run = new_run(tmp_path)
    report = Report("r1", "headless", "abc1234")
    text = runner.write_report(
        run, report, ["", "## Failures", "", "None.", ""]
    ).read_text()
    assert text.startswith(report.markdown())
    assert text.endswith("## Failures\n\nNone.\n")


def test_api_get_sends_the_bearer_token_and_returns_body_and_wall(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = fake_request(monkeypatch, data=b'{"rows": [1]}')
    body, seconds = runner.api_get(new_run(tmp_path), "/catalog/datasets?x=1")
    assert body == {"rows": [1]}
    assert seconds >= 0
    assert calls[0]["method"] == "GET"
    assert calls[0]["url"] == "https://api.example/catalog/datasets?x=1"
    assert calls[0]["headers"] == {"Authorization": "Bearer tok"}


def test_api_get_names_the_status_and_drops_the_query_from_the_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake_request(monkeypatch, status=403, data=b"nope")
    with pytest.raises(StepFailureError, match=r"GET /x answered 403: nope"):
        runner.api_get(new_run(tmp_path), "/x?secret=1")


def test_api_send_wraps_a_transport_error_as_a_step_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def boom(*_a: object, **_k: object) -> None:
        msg = "connection reset"
        raise urllib3.exceptions.HTTPError(msg)

    monkeypatch.setattr(urllib3, "request", boom)
    with pytest.raises(StepFailureError, match="DELETE /x failed: connection reset"):
        runner.api_send(new_run(tmp_path), "DELETE", "/x")


def test_api_send_posts_a_json_body_with_a_content_type(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = fake_request(monkeypatch, status=201, data=b"{}")
    status, text, _ = runner.api_send(new_run(tmp_path), "POST", "/y", {"a": 1})
    assert (status, text) == (201, "{}")
    assert calls[0]["headers"]["Content-Type"] == "application/json"
    assert json.loads(calls[0]["body"]) == {"a": 1}


def test_sign_in_keeps_both_tokens_and_the_app_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    run = new_run(tmp_path)
    run.session = cast("Session", object())
    run.email = "r@example.invalid"

    def outputs(_session: object, stack: str = "ServicesStack") -> dict[str, str]:
        return {"WebUrl": "https://app.example"} if stack == "DataStack" else {"A": "1"}

    def sso(_s: object, out: Any, email: str, config: Path) -> dict[str, str]:
        assert (out["A"], email, config) == ("1", run.email, run.config_dir)
        return {"AccessToken": "acc", "RefreshToken": "ref"}

    monkeypatch.setattr(runner, "stack_outputs", outputs)
    monkeypatch.setattr(runner, "sandbox_sign_in", sso)
    [result] = runner.sign_in(run)
    assert result.passed
    assert result.note == runner.SANDBOX_CLIENT_NOTE
    assert run.outputs["access_token"] == "acc"
    assert run.outputs["refresh_token"] == "ref"
    assert run.outputs["WebUrl"] == "https://app.example"
