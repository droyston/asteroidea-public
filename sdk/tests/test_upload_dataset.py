import json
import uuid
from typing import TYPE_CHECKING, Any

import pytest

import asteroidea
from asteroidea import _upload
from asteroidea._http import Reply
from asteroidea.errors import (
    DatasetNotYoursError,
    UploadIncompleteError,
    UploadRefusedError,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from asteroidea._auth import Auth

API = "https://api.example.test"
PUT = "https://bucket.example.test/put/"


class Platform:
    """A stub platform for many sessions; refuses paths and scripts commit errors."""

    def __init__(
        self,
        *,
        refuse: set[str] | None = None,
        not_yours: bool = False,
        commit_error: dict[str, dict[str, str]] | None = None,
        polls_before_commit: int = 1,
    ) -> None:
        self.refuse = refuse or set()
        self.not_yours = not_yours
        self.commit_error = commit_error or {}
        self.polls_before_commit = polls_before_commit
        self.created: list[dict[str, Any]] = []
        self.paths: dict[str, str] = {}
        self.puts: list[str] = []
        self.finalized: list[str] = []
        self.polls: dict[str, int] = {}

    def refusal(self) -> Reply:
        if not self.not_yours:
            return Reply(409, b"{}")
        payload = {
            "error": "dataset_not_yours",
            "detail": "dataset study belongs to another researcher; "
            "upload under another dataset name",
        }
        return Reply(403, json.dumps(payload).encode())

    def __call__(
        self,
        method: str,
        url: str,
        _headers: Mapping[str, str],
        body: bytes | Path | None,
    ) -> Reply:
        if url.startswith(PUT):
            self.puts.append(url.removeprefix(PUT))
            return Reply(200, b"")
        if method == "POST" and url == f"{API}/sessions":
            assert isinstance(body, bytes)
            sent = json.loads(body)
            self.created.append(sent)
            if self.not_yours or sent["path"] in self.refuse:
                return self.refusal()
            sid = f"s{len(self.created)}"
            self.paths[sid] = sent["path"]
            reply = {"session_id": sid, "expires_at": 1, "url": PUT + sid}
            return Reply(201, json.dumps(reply).encode())
        sid = url.split("/")[4]
        if url.endswith("/finalize"):
            self.finalized.append(sid)
            return Reply(202, b"{}")
        self.polls[sid] = self.polls.get(sid, 0) + 1
        if self.polls[sid] <= self.polls_before_commit:
            return Reply(200, b'{"state": "FINALIZING"}')
        if sid in self.commit_error:
            payload = {"state": "FINALIZING", "commit_error": self.commit_error[sid]}
        else:
            payload = {"state": "COMMITTED", "asset_id": "a-" + sid, "snapshot_id": "7"}
        return Reply(200, json.dumps(payload).encode())


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        assert self.now < 10000, "the poll loop must end"


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "ds"
    (root / "sub-01" / "anat").mkdir(parents=True)
    (root / "dataset_description.json").write_text(
        json.dumps({"Name": "My study", "License": "CC0", "BIDSVersion": "1.9"})
    )
    (root / "sub-01" / "anat" / "sub-01_T1w.nii").write_bytes(b"a" * 50)
    (root / "README").write_bytes(b"read me")
    return root


def run(
    auth: Auth,
    platform: Platform,
    root: Path,
    include: list[str] | None = None,
    **kwargs: bool,
) -> _upload.DatasetUploadResult:
    clock = Clock()
    uploader = _upload.Uploader(
        auth, transport=platform, clock=clock, sleep=clock.sleep
    )
    return _upload.upload_dataset(uploader, root, "study", include, **kwargs)


def test_three_files_keep_their_paths_and_share_one_upload_id(
    auth: Auth, tree: Path
) -> None:
    platform = Platform()
    result = run(auth, platform, tree)
    assert [c["path"] for c in platform.created] == [
        "README",
        "dataset_description.json",
        "sub-01/anat/sub-01_T1w.nii",
    ]
    assert sorted(r.path for r in result.committed) == sorted(
        c["path"] for c in platform.created
    )
    assert result.failed == []
    ids = {c["upload_id"] for c in platform.created}
    assert ids == {result.upload_id}
    assert str(uuid.UUID(result.upload_id)) == result.upload_id
    assert all(
        c["dataset_meta"] == {"title": "My study", "license": "CC0"}
        for c in platform.created
    )
    assert platform.puts == ["s1", "s2", "s3"]
    assert platform.finalized == ["s1", "s2", "s3"]


def test_files_outside_subject_folders_keep_their_relpath(
    auth: Auth, tree: Path
) -> None:
    (tree / "code").mkdir()
    (tree / "code" / "run.py").write_bytes(b"x")
    (tree / "derivatives" / "sub-02").mkdir(parents=True)
    (tree / "derivatives" / "sub-02" / "x.txt").write_bytes(b"x")
    platform = Platform()
    run(auth, platform, tree)
    paths = [c["path"] for c in platform.created]
    assert "code/run.py" in paths  # relpath_of would flatten it to run.py
    assert "derivatives/sub-02/x.txt" in paths  # and this to sub-02/x.txt
    assert _upload.relpath_of(tree / "code" / "run.py") == "run.py"


def test_a_tree_without_a_description_sends_no_dataset_meta(
    auth: Auth, tmp_path: Path
) -> None:
    root = tmp_path / "bare"
    root.mkdir()
    (root / "a.txt").write_bytes(b"a")
    platform = Platform()
    run(auth, platform, root)
    assert "dataset_meta" not in platform.created[0]
    assert "upload_id" in platform.created[0]


def test_include_selects_files(auth: Auth, tree: Path) -> None:
    platform = Platform()
    run(auth, platform, tree, ["sub-*/*/*", "README"])
    assert [c["path"] for c in platform.created] == [
        "README",
        "sub-01/anat/sub-01_T1w.nii",
    ]


def test_a_refused_file_is_reported_and_the_rest_commit(auth: Auth, tree: Path) -> None:
    platform = Platform(refuse={"README"})
    result = run(auth, platform, tree)
    assert len(result.committed) == 2
    assert [rel for rel, _ in result.failed] == ["README"]
    assert "already exists" in result.failed[0][1]
    assert platform.puts == ["s2", "s3"]


def test_every_file_failing_raises(auth: Auth, tree: Path) -> None:
    refuse = {"README", "dataset_description.json", "sub-01/anat/sub-01_T1w.nii"}
    with pytest.raises(UploadRefusedError):
        run(auth, Platform(refuse=refuse), tree)


def test_a_commit_error_is_listed_with_its_class_and_message(
    auth: Auth, tree: Path
) -> None:
    error = {"class": "ValueError", "message": "bad checksum"}
    platform = Platform(commit_error={"s2": error})
    result = run(auth, platform, tree)
    assert len(result.committed) == 2
    ((rel, text),) = result.failed
    assert rel == "dataset_description.json"
    assert "ValueError" in text
    assert "bad checksum" in text


def test_single_upload_surfaces_the_commit_error(auth: Auth, tree: Path) -> None:
    error = {"class": "ValueError", "message": "bad checksum"}
    platform = Platform(commit_error={"s1": error})
    clock = Clock()
    uploader = _upload.Uploader(
        auth, transport=platform, clock=clock, sleep=clock.sleep
    )
    with pytest.raises(UploadIncompleteError, match=r"ValueError: bad checksum"):
        uploader.upload(tree / "README", "study", "README")


def test_polling_stops_at_60_seconds_plus_10_per_file(auth: Auth, tree: Path) -> None:
    platform = Platform(polls_before_commit=10**6, commit_error={})
    clock = Clock()
    uploader = _upload.Uploader(
        auth, transport=platform, clock=clock, sleep=clock.sleep
    )
    with pytest.raises(Exception, match="not visible yet"):
        _upload.upload_dataset(uploader, tree, "study", None)
    assert 60 + 30 <= clock.now < 60 + 30 + 2


def test_a_dataset_not_yours_refusal_stops_at_the_first_file(
    auth: Auth, tree: Path
) -> None:
    platform = Platform(not_yours=True)
    with pytest.raises(DatasetNotYoursError, match="another dataset name"):
        run(auth, platform, tree)
    assert len(platform.created) == 1
    assert platform.puts == []


# -- sandbox= ------------------------------------------


def test_a_sandbox_dataset_marks_every_file(auth: Auth, tree: Path) -> None:
    platform = Platform()
    run(auth, platform, tree, sandbox=True)
    assert len(platform.created) == 3
    assert all(
        c["dataset_meta"] == {"title": "My study", "license": "CC0", "tier": "sandbox"}
        for c in platform.created
    )


def test_a_sandbox_tree_without_a_description_sends_only_the_tier(
    auth: Auth, tmp_path: Path
) -> None:
    root = tmp_path / "bare"
    root.mkdir()
    (root / "a.txt").write_bytes(b"a")
    platform = Platform()
    run(auth, platform, root, sandbox=True)
    assert platform.created[0]["dataset_meta"] == {"tier": "sandbox"}


def test_public_upload_dataset_forwards_sandbox(
    auth: Auth, tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[object] = []

    def spy(*_: object, sandbox: bool = False) -> None:
        seen.append(sandbox)

    monkeypatch.setattr(asteroidea, "default_auth", lambda: auth)
    monkeypatch.setattr(_upload, "upload_dataset", spy)
    asteroidea.upload_dataset(tree, dataset="study")
    asteroidea.upload_dataset(tree, dataset="study", sandbox=True)
    assert seen == [False, True]
