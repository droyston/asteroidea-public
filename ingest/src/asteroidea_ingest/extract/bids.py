"""BIDS text files: JSON sidecars, `participants.tsv`, `*_events.tsv`."""

import csv
import io
import json
from typing import TYPE_CHECKING, Any, Final

from asteroidea_ingest import bids_path, commit

if TYPE_CHECKING:
    from datetime import datetime

MAX_TEXT_BYTES: Final = 1024 * 1024
PARTICIPANTS: Final = "participants.tsv"
SOURCE: Final = PARTICIPANTS
NOT_AVAILABLE: Final = "n/a"
EVENTS_SUFFIX: Final = "_events.tsv"
EVENTS_KNOWN: Final = frozenset({"onset", "duration", "trial_type"})


def extractor_for(relpath: str) -> str | None:
    """Return the extractor's name for a path, or `None` when the format has none.

    One answer for the handler's dispatch and `make reextract`'s SKIP line: `nwb`,
    `nifti`, `json`, `participants` (`participants.tsv`) or `events` (`*_events.tsv`).
    """
    fmt = commit.file_format(bids_path.parse(relpath).extension)
    if fmt == "tsv":
        name = relpath.rsplit("/", 1)[-1]
        if name == PARTICIPANTS:
            return "participants"
        return "events" if name.endswith(EVENTS_SUFFIX) else None
    return fmt if fmt in {"nwb", "nifti", "json"} else None


class BidsError(ValueError):
    """A BIDS text file is not usable."""


def sidecar_json(text: str) -> str:
    """Return the sidecar as canonical JSON (sorted keys); invalid JSON raises."""
    try:
        loaded: object = json.loads(text)
    except json.JSONDecodeError as exc:
        msg = f"sidecar is not valid JSON: {exc}"
        raise BidsError(msg) from exc
    return json.dumps(loaded, sort_keys=True)


def _cell(record: dict[str, str | None], name: str) -> str | None:
    value = (record.get(name) or "").strip()
    return None if value in {"", NOT_AVAILABLE} else value


def participants_rows(
    text: str,
    *,
    dataset: str,
    source_asset_id: str,
    extracted_at: datetime,
) -> list[dict[str, Any]]:
    """Return one `subjects` row per distinct `participant_id` (first row wins).

    ``subject`` always carries the ``sub-`` prefix, as `assets.subject` does.
    """
    reader = csv.DictReader(io.StringIO(text), delimiter="\t")
    if reader.fieldnames is None or "participant_id" not in reader.fieldnames:
        msg = "participants.tsv has no participant_id column"
        raise BidsError(msg)
    seen: dict[str, dict[str, Any]] = {}
    for record in reader:
        ident = _cell(record, "participant_id")
        if ident is None:
            continue
        subject = ident if ident.startswith("sub-") else f"sub-{ident}"
        seen.setdefault(
            subject,
            {
                "dataset": dataset,
                "subject": subject,
                "species": _cell(record, "species"),
                "sex": _cell(record, "sex"),
                "age": _cell(record, "age"),
                "source": SOURCE,
                "source_subject_id": None,
                "source_asset_id": source_asset_id,
                "extracted_at": extracted_at,
            },
        )
    return list(seen.values())


def _number(record: dict[str, str | None], name: str) -> float | None:
    value = _cell(record, name)
    if value is None:
        return None
    try:
        return float(value)
    except ValueError as exc:
        msg = f"events.tsv column {name} holds {value!r}, not a number"
        raise BidsError(msg) from exc


def events_rows(
    text: str, *, scope: dict[str, Any], events_for: str
) -> list[dict[str, Any]]:
    """Return one `events` row per data row of a BIDS `*_events.tsv`.

    ``n/a`` and empty cells are null; columns beyond onset, duration and trial_type go
    to ``columns_json`` as sorted-key JSON (text values). A row without a numeric onset
    raises: ``onset_s`` is required.
    """
    reader = csv.DictReader(io.StringIO(text), delimiter="\t")
    if reader.fieldnames is None or "onset" not in reader.fieldnames:
        msg = "events.tsv has no onset column"
        raise BidsError(msg)
    rows: list[dict[str, Any]] = []
    for position, record in enumerate(reader):
        onset = _number(record, "onset")
        if onset is None:
            msg = f"events.tsv row {position} has no onset"
            raise BidsError(msg)
        extras = {
            name: value
            for name in reader.fieldnames
            if name not in EVENTS_KNOWN and (value := _cell(record, name)) is not None
        }
        rows.append(
            {
                **scope,
                "events_for": events_for,
                "row_index": position,
                "onset_s": onset,
                "duration_s": _number(record, "duration"),
                "trial_type": _cell(record, "trial_type"),
                "columns_json": json.dumps(extras, sort_keys=True) if extras else None,
            }
        )
    return rows
