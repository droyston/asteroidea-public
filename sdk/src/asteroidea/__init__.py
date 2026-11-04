"""Python SDK for the asteroidea platform."""

from pathlib import Path
from typing import TYPE_CHECKING

from asteroidea import (
    _artifacts,
    _browse,
    _creds,
    _image,
    _labels,
    _nwb,
    _query,
    _run,
    _session,
    _sql,
    _studies,
    _summary,
    _upload,
)
from asteroidea._artifacts import ArtifactInfo, ArtifactRecord, ArtifactResult
from asteroidea._auth import default_auth
from asteroidea._query import FileRef, QueryResult
from asteroidea._run import Run
from asteroidea._sample import write_sample
from asteroidea._session import Session
from asteroidea._summary import EventRates, SessionSummary
from asteroidea._upload import DatasetUploadResult, UploadResult
from asteroidea.errors import (
    ArtifactNotFoundError,
    ArtifactRefusedError,
    AssetNotFoundError,
    AsteroideaError,
    ConfigMissingError,
    CredentialsUnavailableError,
    DatasetNotYoursError,
    ExtraMissingError,
    FinalizeTimeoutError,
    HasDependentsError,
    IdTokenRequiredError,
    NestedRunError,
    NotAResearcherError,
    NotNwbError,
    NotTheAuthorError,
    PlatformUnreachableError,
    QueryError,
    ReadInterruptedError,
    SignInFailedError,
    SignInRequiredError,
    SnapshotGoneError,
    UnknownFilterError,
    UploadIncompleteError,
    UploadRefusedError,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from typing import Any

    import pyarrow as pa

    from asteroidea._remote import RemoteFile

__all__ = [
    "ArtifactInfo",
    "ArtifactNotFoundError",
    "ArtifactRecord",
    "ArtifactRefusedError",
    "ArtifactResult",
    "AssetNotFoundError",
    "AsteroideaError",
    "ConfigMissingError",
    "CredentialsUnavailableError",
    "DatasetNotYoursError",
    "DatasetUploadResult",
    "EventRates",
    "ExtraMissingError",
    "FileRef",
    "FinalizeTimeoutError",
    "HasDependentsError",
    "IdTokenRequiredError",
    "NestedRunError",
    "NotAResearcherError",
    "NotNwbError",
    "NotTheAuthorError",
    "PlatformUnreachableError",
    "QueryError",
    "QueryResult",
    "ReadInterruptedError",
    "Run",
    "Session",
    "SessionSummary",
    "SignInFailedError",
    "SignInRequiredError",
    "SnapshotGoneError",
    "UnknownFilterError",
    "UploadIncompleteError",
    "UploadRefusedError",
    "UploadResult",
    "artifacts",
    "assets",
    "datasets",
    "download_artifact",
    "labels",
    "links",
    "load_session",
    "open_artifact",
    "open_file",
    "open_image",
    "open_nwb",
    "participants",
    "query",
    "run",
    "save_artifact",
    "session_summary",
    "sessions",
    "sessions_index",
    "sign_in",
    "sql",
    "studies",
    "subjects",
    "upload",
    "upload_dataset",
    "withdraw_artifact",
    "write_sample",
]


def sign_in(config_path: Path | str | None = None) -> None:
    """Sign in once; later calls reuse the saved sign-in and print no address.

    Without a saved sign-in this prints a login address. Open it in any browser,
    sign in, and paste the address the browser ends on (or its code) back here.
    `config_path` names a platform.json; the default is
    ~/.config/asteroidea/platform.json.
    """
    auth = default_auth(config_path)
    auth.sign_in()
    _creds.credentials(auth)


def upload(
    path: Path | str,
    *,
    dataset: str,
    relpath: str | None = None,
    sandbox: bool = False,
) -> UploadResult:
    """Upload one file into `dataset` and wait until it is in the catalog.

    `relpath` is the path inside the dataset; by default it starts at the first
    `sub-*` folder of `path`, else it is the file name. `sandbox=True` marks the
    dataset as a sandbox (test) dataset, which the data home folds away.
    """
    uploader = _upload.Uploader(default_auth())
    return uploader.upload(Path(path), dataset, relpath, sandbox=sandbox)


def query(*, snapshot_id: int | None = None, **filters: str) -> QueryResult:
    """Return the catalog rows that match the filters, with the snapshot id.

    Filters: dataset, subject, bids_session, datatype, suffix, extension (for
    example `subject="01"`). `snapshot_id` reads the catalog as it was then.
    """
    result = _query.query(default_auth(), snapshot_id=snapshot_id, **filters)
    _run.record_query(result)
    return result


def open_file(asset_id: str) -> RemoteFile:
    """Return a read-only seekable file; each read is one ranged request.

    Inside a `run()`, the asset and its opened version are recorded.
    """
    remote = _image.open_asset(default_auth(), asset_id)
    _run.record_asset(asset_id, remote.source.version_id, "open_file")
    return remote


def open_nwb(asset_id: str) -> object:
    """Return a pynwb NWBFile read lazily in 4 MiB blocks; needs asteroidea[nwb].

    Inside a `run()`, the asset and its opened version are recorded.
    """
    remote = _image.open_asset(default_auth(), asset_id)
    _run.record_asset(asset_id, remote.source.version_id, "open_nwb")
    return _nwb.read_nwb(remote)


def run(name: str, params: Mapping[str, Any] | None = None) -> Run:
    """Open a run that records what your code reads, to save with its result.

    Use it as `with run("rates", {"bin_s": 0.05}) as r:`, then `r.save_artifact(p)`.

    While the run is open, every `sql()` table snapshot, `query()` file and
    snapshot, `open_file`, `open_nwb` or `open_image` asset version, file version
    read by `load_session` or a `Session`, result opened or downloaded, and
    `Session.rates` or `Session.tuning` defaults and recipe version is recorded,
    and
    `r.save_artifact(path, note=None)` saves the file with that record. A run
    inside an open run raises `NestedRunError`.
    """
    return Run(name, params)


def open_image(asset_id: str) -> object:
    """Return a NIfTI image (.nii or .nii.gz) read lazily; needs asteroidea[nifti].

    Inside a `run()`, the asset and its opened version are recorded.
    """
    remote = _image.open_asset(default_auth(), asset_id)
    _run.record_asset(asset_id, remote.source.version_id, "open_image")
    return _image.image_over(remote)


def sql(query: str, /) -> pa.Table:
    """Run a SQL query over the catalog tables and return an Arrow table.

    Views: assets, datasets, subjects, subject_links, sessions, asset_headers,
    series, electrodes, units, intervals, plus `subject_index` and `files`.
    """
    return _sql.sql(default_auth(), query)


def datasets() -> pa.Table:
    """Return every dataset."""
    return _browse.datasets(default_auth())


def subjects(*, dataset: str | None = None) -> pa.Table:
    """Return subjects (with `subject_key`), optionally of one dataset."""
    return _browse.subjects(default_auth(), dataset=dataset)


def sessions(*, dataset: str | None = None, subject: str | None = None) -> pa.Table:
    """Return sessions, optionally of one dataset or subject."""
    return _browse.sessions(default_auth(), dataset=dataset, subject=subject)


def studies(*, tier: str | None = None) -> pa.Table:
    """Return studies with their datasets, one row per study and dataset.

    Columns: study, title, authors, lab, citation, doi, dataset, role,
    dataset_title, tier. `tier` (`curated`, `upload` or `sandbox`) keeps only
    datasets of that tier; a dataset with no stored tier is `curated` when it was
    imported and `upload` otherwise.
    """
    return _studies.studies(default_auth(), tier=tier)


def participants(
    *, study: str | None = None, has: Sequence[str] | None = None
) -> pa.Table:
    """Return one row per participant `subject_key`.

    Columns: subject_key, datasets, subjects (as `<dataset>/<subject>`) and
    modalities, each a list. Modalities use the web app's names: units, lfp,
    ieeg, anat, func, task, else `other:<name>`. `study` keeps participants in
    that study's datasets; `has=["anat", "units"]` keeps those with all listed.
    """
    return _studies.participants(default_auth(), study=study, has=has)


def links(*, study: str | None = None, subject_key: str | None = None) -> pa.Table:
    """Return subject links, with status, linked_by, linked_at and basis.

    A link with no stored status reads `pending`. `study` keeps links of that
    study's datasets; `subject_key` keeps links to that participant.
    """
    return _studies.links(default_auth(), study=study, subject_key=subject_key)


def assets(
    *,
    dataset: str | None = None,
    subject: str | None = None,
    bids_session: str | None = None,
    file_format: str | None = None,
) -> pa.Table:
    """Return assets joined to their file headers."""
    return _browse.assets(
        default_auth(),
        dataset=dataset,
        subject=subject,
        bids_session=bids_session,
        file_format=file_format,
    )


def upload_dataset(
    root: Path | str,
    *,
    dataset: str,
    include: Sequence[str] | None = None,
    sandbox: bool = False,
) -> DatasetUploadResult:
    """Upload a folder into `dataset`, each file under its path relative to `root`.

    `include` lists glob patterns (relative paths with `/`) to upload only some
    files. `dataset_description.json` in `root` gives the dataset its title and
    licence. Failures are listed in the result's `failed`; the call raises only
    when every file failed. `sandbox=True` marks the dataset as a sandbox (test)
    dataset.
    """
    uploader = _upload.Uploader(default_auth())
    return _upload.upload_dataset(
        uploader, Path(root), dataset, include, sandbox=sandbox
    )


def save_artifact(
    path: Path | str,
    *,
    sources: Sequence[FileRef],
    snapshot_id: int,
    note: str | None = None,
) -> ArtifactResult:
    """Save a file as a result every researcher can read, with the files it came from.

    `sources` are the `FileRef`s of the catalog files it was computed from and
    `snapshot_id` the snapshot of the `query()` that returned them.
    """
    return _artifacts.save_artifact(
        Path(path), sources=sources, snapshot_id=snapshot_id, note=note
    )


def artifacts(scope: str = "mine", input_id: str | None = None) -> list[ArtifactInfo]:
    """Return saved results, newest first (at most 100).

    `scope="mine"` is your own results; `scope="all"` is the newest finished
    results of every author, each with its `author_email`. `input_id` (an asset
    id or a result id) keeps only the results made from it.
    """
    return _artifacts.artifacts(scope, input_id)


def open_artifact(artifact_id: str) -> ArtifactRecord:
    """Return any author's finished result: its summary and its whole record.

    Inside a `run()`, the result is recorded as an input of the run.
    """
    return _artifacts.open_artifact(artifact_id)


def download_artifact(artifact_id: str, dest: Path | str) -> Path:
    """Save any author's finished result's file to `dest` (a file or a folder).

    Returns the path written. Inside a `run()`, the result is recorded as an
    input of the run.
    """
    return _artifacts.download_artifact(artifact_id, Path(dest))


def withdraw_artifact(artifact_id: str) -> str:
    """Withdraw your own finished result; it stays readable, marked WITHDRAWN.

    Returns the new state. Another author's result raises `NotTheAuthorError`.
    """
    return _artifacts.withdraw_artifact(artifact_id)


def labels(asset_id: str) -> pa.Table:
    """Return the labels researchers put on a file, as an Arrow table.

    Columns: label_id, name, note, start_s, stop_s, series, mine (true for your
    own labels), created_at, updated_at, version, s3_version_id and snapshots
    (the catalog snapshot ids at the time, as JSON text). Earliest start first;
    an empty table when the file has none. Read only: labels are made and
    edited in the web app.
    """
    return _labels.labels_of(asset_id)


def sessions_index() -> pa.Table:
    """Return every session: `session_key`, dataset, subject, session, start time.

    `session_key` is `<dataset>/<subject>/<session or _>`; `_` stands for files
    with no `ses-` label. Start time and description come from `sessions` when the
    session has an NWB file.
    """
    return _session.sessions_index()


def session_summary(dataset: str, subject: str, session: str) -> SessionSummary | None:
    """Return one session's stored summary, or `None` when none is built yet.

    The summary is what the web app's session page draws: the units table
    (`units`, with each unit's `location`), the events (`events`) and, per
    averaged event type, every unit's rate and z per bin (`rates[event_type]`,
    an `EventRates` with `units`, `edges` and `responsive`). It is read, not
    computed, so it returns in seconds. `session` is the `ses-` label, or `_`.
    """
    return _summary.session_summary(default_auth(), dataset, subject, session)


def load_session(dataset: str, subject: str, session: str) -> Session:
    """Load one session (files, units, spikes, events; LFP, rates and tuning).

    `session` is the `ses-` label, or `_` when there is none. Needs the
    `asteroidea[nwb]` extra for `.lfp`.
    """
    return _session.load_session(dataset, subject, session)
