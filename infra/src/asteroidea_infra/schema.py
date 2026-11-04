"""The catalog's Iceberg table specs. Plain data: no `aws_cdk`.

`make catalog-init` creates each table and adds missing optional columns; it never
drops or retypes one. Types are Iceberg primitives, or `list<long>` / `list<double>`.
"""

from typing import Final, NamedTuple

DATABASE: Final = "asteroidea"
TABLE: Final = "assets"
DATASETS: Final = "datasets"
ICEBERG_PREFIX: Final = "iceberg"
PROBE_TABLE: Final = (
    "s1_probe"  # `make live-iceberg-check` only; ResearcherRole cannot read it
)
ASSETS_COUNT_SQL: Final = "SELECT count(*) FROM assets"  # the Athena count; a literal


class Column(NamedTuple):
    """One Iceberg column; `type` is an Iceberg type name."""

    name: str
    type: str
    required: bool


class TableSpec(NamedTuple):
    """One catalog table: its columns and its check-before-append key."""

    name: str
    columns: tuple[Column, ...]
    key: tuple[str, ...]


def _req(name: str, type_: str = "string") -> Column:
    return Column(name, type_, required=True)


def _opt(name: str, type_: str = "string") -> Column:
    return Column(name, type_, required=False)


# The original 13 columns, unchanged.
BASE_ASSETS_COLUMNS: Final = (
    _req("asset_id"),
    _req("session_id"),
    _req("dataset"),
    _req("s3_key"),
    _req("s3_version_id"),
    _req("size_bytes", "long"),
    _req("checksum_crc64nvme"),
    _opt("subject"),
    _opt("bids_session"),
    _opt("datatype"),
    _opt("suffix"),
    _req("extension"),
    _req("committed_at", "timestamp"),
)
# Provenance: optional, so rows from the first version read null.
PROVENANCE_COLUMNS: Final = tuple(
    _opt(name)
    for name in (
        "upload_id",
        "ingest_method",
        "format",
        "checksum_sha256",
        "source_archive",
        "source_dataset",
        "source_version",
        "source_path",
        "source_url",
        "source_digest",
        "license",
        "ingested_by",
    )
)
ASSETS_COLUMNS: Final = (*BASE_ASSETS_COLUMNS, *PROVENANCE_COLUMNS)
_ASSET_SCOPE: Final = (_req("asset_id"), _req("dataset"), _opt("subject"))

TABLES: Final = (
    TableSpec(TABLE, ASSETS_COLUMNS, ("session_id",)),
    TableSpec(
        DATASETS,
        (
            _req("dataset"),
            _opt("title"),
            _opt("description"),
            _opt("license"),
            _opt("source_archive"),
            _opt("source_dataset"),
            _opt("source_version"),
            _opt("doi"),
            _opt("url"),
            _req("ingest_method"),
            _req("created_by"),
            _req("created_at", "timestamp"),
            # curated | upload | sandbox; null reads as curated for an import,
            # else upload.
            _opt("tier"),
        ),
        ("dataset",),
    ),
    TableSpec(
        "subjects",
        (
            _req("dataset"),
            _req("subject"),
            _opt("species"),
            _opt("sex"),
            _opt("age"),
            _req("source"),
            _opt("source_subject_id"),
            _req("source_asset_id"),
            _req("extracted_at", "timestamp"),
        ),
        ("dataset", "subject"),
    ),
    TableSpec(
        "subject_links",
        (
            _req("dataset"),
            _req("subject"),
            _req("subject_key"),
            _opt("note"),
            _req("curated_by"),
            _req("created_at", "timestamp"),
            _opt("status"),  # null reads as pending
            _opt("linked_by"),
            _opt("linked_at", "timestamp"),
            _opt("basis"),
        ),
        ("dataset", "subject"),
    ),
    TableSpec(
        "sessions",
        (
            _req("dataset"),
            _req("subject"),
            _req("bids_session"),
            _opt("start_time", "timestamp"),
            _opt("description"),
            _req("source_asset_id"),
            _req("extracted_at", "timestamp"),
        ),
        ("dataset", "subject", "bids_session"),
    ),
    TableSpec(
        "asset_headers",
        (
            _req("asset_id"),
            _req("dataset"),
            _req("format"),
            _req("extractor_version"),
            _req("extracted_at", "timestamp"),
            _opt("nwb_version"),
            _opt("nwb_identifier"),
            _opt("session_description"),
            _opt("session_start_time", "timestamp"),
            _opt("institution"),
            _opt("lab"),
            _opt("n_units", "long"),
            _opt("n_electrodes", "long"),
            _opt("n_trials", "long"),
            _opt("nifti_shape", "list<long>"),
            _opt("nifti_voxel_mm", "list<double>"),
            _opt("nifti_datatype"),
            _opt("tr_seconds", "double"),
            _opt("sidecar_json"),
        ),
        ("asset_id",),
    ),
    TableSpec(
        "series",
        (
            _req("asset_id"),
            _req("dataset"),
            _opt("subject"),
            _opt("bids_session"),
            _req("path"),
            _req("neurodata_type"),
            _opt("shape", "list<long>"),
            _opt("dtype"),
            _opt("rate_hz", "double"),
            _opt("starting_time_s", "double"),
            _opt("has_timestamps", "boolean"),
            _opt("unit"),
        ),
        ("asset_id",),
    ),
    TableSpec(
        "electrodes",
        (
            *_ASSET_SCOPE,
            _opt("bids_session"),
            _req("electrode_index", "long"),
            _opt("electrode_id", "long"),
            _opt("location"),
            _opt("group_name"),
            _opt("x", "double"),
            _opt("y", "double"),
            _opt("z", "double"),
            _opt("columns_json"),
        ),
        ("asset_id",),
    ),
    TableSpec(
        "units",
        (
            *_ASSET_SCOPE,
            _opt("bids_session"),
            _req("unit_index", "long"),
            _opt("unit_id"),
            _opt("electrode_index", "long"),
            _opt("location"),
            _opt("group_name"),
            _req("spike_count", "long"),
            _opt("first_spike_s", "double"),
            _opt("last_spike_s", "double"),
            _opt("mean_rate_hz", "double"),
            _opt("columns_json"),
        ),
        ("asset_id",),
    ),
    TableSpec(
        "intervals",
        (
            *_ASSET_SCOPE,
            _opt("bids_session"),
            _req("table_name"),
            _req("row_index", "long"),
            _req("start_time", "double"),
            _opt("stop_time", "double"),
            _opt("columns_json"),
        ),
        ("asset_id",),
    ),
    TableSpec(
        "unit_spikes",
        (
            *_ASSET_SCOPE,
            _opt("bids_session"),
            _req("unit_index", "long"),
            _req("spike_count", "long"),
            _opt("spike_times", "list<double>"),
        ),
        ("asset_id",),
    ),
    TableSpec(
        "events",
        (
            *_ASSET_SCOPE,
            _opt("bids_session"),
            _req("events_for"),
            _req("row_index", "long"),
            _req("onset_s", "double"),
            _opt("duration_s", "double"),
            _opt("trial_type"),
            _opt("columns_json"),
        ),
        ("asset_id",),
    ),
    TableSpec(
        "studies",
        (
            _req("study"),
            _req("title"),
            _opt("authors"),
            _opt("lab"),
            _opt("citation"),
            _opt("doi"),
            _opt("note"),
            _req("curated_by"),
            _req("created_at", "timestamp"),
        ),
        ("study",),
    ),
    TableSpec(
        "study_datasets",
        (
            _req("study"),
            _req("dataset"),
            _opt("role"),
            _req("curated_by"),
            _req("created_at", "timestamp"),
        ),
        ("study", "dataset"),
    ),
)
SPECS: Final = {spec.name: spec for spec in TABLES}
ASSETS: Final = SPECS[TABLE]
DATASETS_SPEC: Final = SPECS[DATASETS]
# ResearcherRole's Glue grant names each of these and nothing else, so `PROBE_TABLE`
# stays unreadable.
TABLE_NAMES: Final = tuple(spec.name for spec in TABLES)
