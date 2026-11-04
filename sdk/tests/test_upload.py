import json
from typing import TYPE_CHECKING, Any

import pytest

import asteroidea
from asteroidea import _crc, _upload
from asteroidea._http import Reply
from asteroidea.errors import (
    DatasetNotYoursError,
    FinalizeTimeoutError,
    NotAResearcherError,
    PlatformUnreachableError,
    SignInRequiredError,
    UploadIncompleteError,
    UploadRefusedError,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from asteroidea._auth import Auth

API = "https://api.example.test"
PUT_URL = "https://bucket.example.test/landing/s1/sub-01/anat/sub-01_T1w.nii?sig=x"


class Fake:
    """A stub platform: records calls, plays a script of poll states."""

    def __init__(
        self,
        states: list[str],
        *,
        create: int = 201,
        put: int = 200,
        create_payload: dict[str, str] | None = None,
    ) -> None:
        self.create_payload = create_payload
        self.states = states
        self.create_status = create
        self.put_status = put
        self.finalize_status = 202
        self.calls: list[tuple[str, str]] = []
        self.sent: dict[str, Any] = {}
        self.polls = 0

    def __call__(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | Path | None,
    ) -> Reply:
        self.calls.append((method, url))
        if url == PUT_URL:
            self.sent["put_headers"] = dict(headers)
            return Reply(self.put_status, b"")
        self.sent.setdefault("auth", headers.get("Authorization"))
        if method == "POST" and url == f"{API}/sessions":
            assert isinstance(body, bytes)
            self.sent["create_bytes"] = body
            self.sent["create"] = json.loads(body)
            if self.create_status == 201:
                payload = {"session_id": "s1", "expires_at": 1, "url": PUT_URL}
            else:
                payload = self.create_payload or {
                    "error": "invalid_request",
                    "detail": "dataset must match ^[a-z0-9]",
                }
            return Reply(self.create_status, json.dumps(payload).encode())
        if url.endswith("/finalize"):
            return Reply(self.finalize_status, b'{"state": "FINALIZING"}')
        self.polls += 1
        state = self.states[min(self.polls - 1, len(self.states) - 1)]
        body_ = {"state": state}
        if state == "COMMITTED":
            body_ |= {"asset_id": "a-1", "snapshot_id": "42"}
        return Reply(200, json.dumps(body_).encode())


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps = 0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps += 1
        assert self.sleeps < 500, "the poll loop must end"
        self.now += seconds


@pytest.fixture
def sample(tmp_path: Path) -> Path:
    path = tmp_path / "ds" / "sub-01" / "anat" / "sub-01_T1w.nii"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"x" * 1000)
    return path


def uploader(auth: Auth, fake: Fake, clock: Clock) -> _upload.Uploader:
    return _upload.Uploader(auth, transport=fake, clock=clock, sleep=clock.sleep)


def test_relpath_starts_at_the_first_subject_folder(
    sample: Path, tmp_path: Path
) -> None:
    assert _upload.relpath_of(sample) == "sub-01/anat/sub-01_T1w.nii"
    assert _upload.relpath_of(tmp_path / "data.bin") == "data.bin"


def test_upload_creates_puts_finalizes_and_polls(auth: Auth, sample: Path) -> None:
    fake, clock = Fake(["FINALIZING", "ACCEPTED", "COMMITTED"]), Clock()
    result = uploader(auth, fake, clock).upload(sample, "ds1")
    assert result.asset_id == "a-1"
    assert result.snapshot_id == 42
    assert fake.sent["create"] == {
        "dataset": "ds1",
        "path": "sub-01/anat/sub-01_T1w.nii",
        "size": 1000,
        "crc64nvme": _crc.crc64nvme(sample),
    }
    assert fake.sent["auth"] == "Bearer the-access-token"
    assert fake.sent["put_headers"] == {_crc.HEADER: _crc.crc64nvme(sample)}
    order = [m + " " + u.rsplit("/", 1)[-1].split("?")[0] for m, u in fake.calls]
    assert order[:3] == ["POST sessions", "PUT sub-01_T1w.nii", "POST finalize"]
    assert fake.polls == 3


def test_a_row_not_visible_60_seconds_after_the_202_times_out(
    auth: Auth, sample: Path
) -> None:
    fake, clock = Fake(["FINALIZING"]), Clock()
    with pytest.raises(FinalizeTimeoutError, match="not visible"):
        uploader(auth, fake, clock).upload(sample, "ds1")
    assert 60.0 <= clock.now < 62.0
    assert fake.polls >= 60


def test_a_row_visible_at_59_seconds_is_not_a_timeout(auth: Auth, sample: Path) -> None:
    fake, clock = Fake(["FINALIZING"] * 59 + ["COMMITTED"]), Clock()
    assert uploader(auth, fake, clock).upload(sample, "ds1").asset_id == "a-1"


def test_incomplete_raises_upload_incomplete(auth: Auth, sample: Path) -> None:
    fake, clock = Fake(["FINALIZING", "INCOMPLETE"]), Clock()
    with pytest.raises(UploadIncompleteError, match="upload again"):
        uploader(auth, fake, clock).upload(sample, "ds1")


def test_a_refused_put_raises_upload_incomplete_and_never_finalizes(
    auth: Auth, sample: Path
) -> None:
    fake, clock = Fake(["COMMITTED"], put=400), Clock()
    with pytest.raises(UploadIncompleteError):
        uploader(auth, fake, clock).upload(sample, "ds1")
    assert not any(u.endswith("/finalize") for _, u in fake.calls)


@pytest.mark.parametrize(
    ("status", "words"),
    [(400, "refused the upload"), (409, "already exists")],
)
def test_api_400_and_409_become_plain_messages(
    auth: Auth, sample: Path, status: int, words: str
) -> None:
    fake, clock = Fake(["COMMITTED"], create=status), Clock()
    with pytest.raises(UploadRefusedError, match=words) as caught:
        uploader(auth, fake, clock).upload(sample, "ds1")
    assert "HTTP" not in str(caught.value)
    assert "invalid_request" not in str(caught.value)
    assert not any(m == "PUT" for m, _ in fake.calls)


def test_401_and_403_ask_for_sign_in_or_an_administrator(
    auth: Auth, sample: Path
) -> None:
    clock = Clock()
    with pytest.raises(SignInRequiredError):
        uploader(auth, Fake([], create=401), clock).upload(sample, "ds1")
    with pytest.raises(NotAResearcherError):
        uploader(auth, Fake([], create=403), clock).upload(sample, "ds1")


def test_403_with_the_not_yours_code_raises_dataset_not_yours(
    auth: Auth, sample: Path
) -> None:
    text = (
        "dataset ds1 belongs to another researcher; upload under another dataset name"
    )
    fake = Fake(
        [], create=403, create_payload={"error": "dataset_not_yours", "detail": text}
    )
    with pytest.raises(DatasetNotYoursError) as caught:
        uploader(auth, fake, Clock()).upload(sample, "ds1")
    assert isinstance(caught.value, UploadRefusedError)
    assert not isinstance(caught.value, NotAResearcherError)
    assert str(caught.value) == text


def test_403_with_the_old_code_key_still_raises_dataset_not_yours(
    auth: Auth, sample: Path
) -> None:
    text = "dataset ds1 belongs to another researcher"
    fake = Fake(
        [], create=403, create_payload={"error": text, "code": "dataset_not_yours"}
    )
    with pytest.raises(DatasetNotYoursError) as caught:
        uploader(auth, fake, Clock()).upload(sample, "ds1")
    assert str(caught.value) == text


def test_a_400_from_an_older_api_shows_its_error_text(auth: Auth, sample: Path) -> None:
    fake = Fake([], create=400, create_payload={"error": "size must be an integer"})
    with pytest.raises(UploadRefusedError, match="size must be an integer"):
        uploader(auth, fake, Clock()).upload(sample, "ds1")


def test_a_uniform_500_names_the_request_id(auth: Auth, sample: Path) -> None:
    text = "The platform failed on this request. Report request id r-42."
    fake = Fake(
        [], create=500, create_payload={"error": "internal_error", "detail": text}
    )
    with pytest.raises(PlatformUnreachableError) as caught:
        uploader(auth, fake, Clock()).upload(sample, "ds1")
    assert str(caught.value) == text


def test_403_with_another_code_keeps_the_researcher_mapping(
    auth: Auth, sample: Path
) -> None:
    fake = Fake([], create=403, create_payload={"error": "forbidden", "detail": "x"})
    with pytest.raises(NotAResearcherError):
        uploader(auth, fake, Clock()).upload(sample, "ds1")


# -- sandbox= ------------------------------------------


def test_sandbox_upload_sends_dataset_meta_tier(auth: Auth, sample: Path) -> None:
    fake, clock = Fake(["COMMITTED"]), Clock()
    uploader(auth, fake, clock).upload(sample, "ds1", sandbox=True)
    assert fake.sent["create"]["dataset_meta"] == {"tier": "sandbox"}


def test_without_sandbox_the_create_body_is_byte_identical(
    auth: Auth, sample: Path
) -> None:
    """A call without `sandbox` sends exactly the bytes it sent before it existed."""
    today = json.dumps(
        {
            "dataset": "ds1",
            "path": "sub-01/anat/sub-01_T1w.nii",
            "size": 1000,
            "crc64nvme": _crc.crc64nvme(sample),
        }
    ).encode()
    fake, clock = Fake(["COMMITTED"]), Clock()
    uploader(auth, fake, clock).upload(sample, "ds1")
    assert fake.sent["create_bytes"] == today
    fake, clock = Fake(["COMMITTED"]), Clock()
    uploader(auth, fake, clock).upload(sample, "ds1", sandbox=False)
    assert fake.sent["create_bytes"] == today


def test_sandbox_batch_keeps_the_batch_meta(auth: Auth, sample: Path) -> None:
    fake, clock = Fake(["COMMITTED"]), Clock()
    batch = {"upload_id": "u", "dataset_meta": {"title": "t"}}
    uploader(auth, fake, clock).open_session(
        sample, "ds1", batch=_upload.sandbox_batch(batch)
    )
    assert fake.sent["create"]["dataset_meta"] == {"title": "t", "tier": "sandbox"}
    assert batch == {"upload_id": "u", "dataset_meta": {"title": "t"}}  # not mutated


def test_public_upload_forwards_sandbox(
    auth: Auth, sample: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[object] = []

    class Spy:
        def __init__(self, _auth: object) -> None:
            pass

        def upload(self, *_: object, sandbox: bool = False) -> None:
            seen.append(sandbox)

    monkeypatch.setattr(asteroidea, "default_auth", lambda: auth)
    monkeypatch.setattr(_upload, "Uploader", Spy)
    asteroidea.upload(sample, dataset="ds1")
    asteroidea.upload(sample, dataset="ds1", sandbox=True)
    assert seen == [False, True]
