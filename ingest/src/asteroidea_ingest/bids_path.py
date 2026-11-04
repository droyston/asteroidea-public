"""Split a BIDS relative path into catalog columns; no header checks."""

from pathlib import PurePosixPath
from typing import NamedTuple


class BidsParts(NamedTuple):
    """Columns derived from the path; `None` where the path does not carry them."""

    subject: str | None
    bids_session: str | None
    datatype: str | None
    suffix: str | None
    extension: str


def _entity(name: str, prefix: str) -> str | None:
    for part in name.split("_"):
        if part.startswith(prefix):
            return part
    return None


def parse(relpath: str) -> BidsParts:
    """Parse `sub-01/[ses-01/]anat/sub-01_T1w.nii` into its parts.

    The extension is everything from the first dot of the file name (`.nii.gz`).
    """
    path = PurePosixPath(relpath)
    stem, dot, ext = path.name.partition(".")
    extension = f".{ext}" if dot else ""
    suffix = stem.rsplit("_", 1)[-1] if "_" in stem else None
    folders = path.parts[:-1]
    subject = next((p for p in folders if p.startswith("sub-")), None)
    if subject is None:
        subject = _entity(stem, "sub-")
    bids_session = next((p for p in folders if p.startswith("ses-")), None)
    if bids_session is None:
        bids_session = _entity(stem, "ses-")
    datatype = (
        folders[-1]
        if folders and not folders[-1].startswith(("sub-", "ses-"))
        else None
    )
    return BidsParts(subject, bids_session, datatype, suffix, extension)


def entity_key(relpath: str) -> str:
    """Return the directory plus the file name's entities, minus suffix and extension.

    `sub-01/ses-1/func/sub-01_ses-1_task-x_run-1_events.tsv` and `..._run-1_bold.nii.gz`
    share the key `sub-01/ses-1/func/sub-01_ses-1_task-x_run-1`. A name with no
    underscore has no suffix to drop and returns its stem.
    """
    path = PurePosixPath(relpath)
    stem = path.name.partition(".")[0]
    entities = stem.rsplit("_", 1)[0] if "_" in stem else stem
    return str(path.parent / entities) if path.parent.parts else entities
