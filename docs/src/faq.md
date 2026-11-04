# FAQ

An internal page of answers to questions that came up while onboarding. It is written for the people who build
and run the platform, not for researchers (they have [For researchers](researchers.md)). Each section opens
with a short version, then the detail, then a few short questions that point back at it. Where a claim is
inferred, an estimate or an opinion, it says so ("unverified", "estimate", "opinion"); read those as open.
The deeper reference is the arc42 chapters, the ADRs and the [developer guide](developers.md).

Questions by part:

**Part 1: The platform**

1. [Data ingestion: upload to catalog](#1-data-ingestion-upload-to-catalog): "I uploaded a file; why can't I query it yet?", "Why two stores?"
2. [File formats: what is actually materialized?](#2-file-formats-what-is-actually-materialized)
3. [Tech selection: why Iceberg and CloudFront?](#3-tech-selection-why-iceberg-and-cloudfront)

**Part 2: Data types**

4. [Spikes, LFP and MRI](#4-spikes-lfp-and-mri)

**Part 3: The SDK**

5. [The SDK: install, data access and access logs](#5-the-sdk-install-data-access-and-access-logs)
6. [What a researcher can do through the SDK alone](#6-what-a-researcher-can-do-through-the-sdk-alone)
7. [How much data can the SDK load?](#7-how-much-data-can-the-sdk-load)

**Part 4: The web app**

8. [The web app: definition and deployment](#8-the-web-app-definition-and-deployment)
9. [The web app's limits and how powerful a visualization it could run](#9-what-are-the-web-apps-limits-how-powerful-a-visualization-could-it-run): "Why does the app say 'too large'?", "Can I add a charting library?"
10. [Could the web app read tables and files directly like the SDK?](#10-could-the-web-app-read-tables-and-files-directly-like-the-sdk-why-doesnt-it): "Is a presigned URL safe?"
11. [If the browser read directly, would we still need the API layer?](#11-if-the-browser-read-directly-would-we-still-need-the-api-layer-what-is-its-value): "Why does the SDK still call the API?"


**Part 6: What if**

13. [Why not Snowflake or dbt?](#13-why-not-snowflake-or-dbt)
14. [Design notes: a continuous data source (not built)](#14-design-notes-a-continuous-data-source-not-built)

---

## Part 1: The platform

### 1. Data ingestion: upload to catalog

**Short version:** file bytes go straight to S3; a DynamoDB "session" tracks each upload while it is in flight;
only a finalized, checksum-verified file gets rows in the Iceberg catalog. See also the
[runtime view](06-runtime.md).

**Steps** (`asteroidea.upload` / `upload_dataset`; curator imports from DANDI/OpenNeuro take the same path):

1. **Create:** the SDK calls `POST /sessions` with dataset, path, size and a CRC64NVME checksum. It gets a
   session id and a presigned S3 URL. The session starts OPEN in DynamoDB.
2. **PUT:** bytes go directly to `landing/` in the raw bucket. The API never carries file bytes (API Gateway
   caps a payload at 10 MB). A single PUT goes up to 5 GiB; multipart is not built.
3. **Finalize:** a conditional update moves the session OPEN to FINALIZING (so a second finalize never starts a
   second run). It answers 202 and queues a commit message.
4. **Commit** (`CommitFunction`): verify checksum and size. A mismatch sets INCOMPLETE and nothing is copied.
   Otherwise claim the session (ACCEPTED, with a lease), copy to `raw/`, and append the `assets` row in
   Iceberg. The session becomes COMMITTED with `asset_id` and `snapshot_id`.
5. **Extract** (`ExtractFunction`): picks an extractor by path (NWB, NIfTI, BIDS sidecar, `participants.tsv`,
   `events.tsv`) and writes the detail tables (subjects, series, units, electrodes, events, ...).
6. **Report:** the SDK polls `GET /sessions/{id}` every second until COMMITTED, and gives up after 60 s
   (`FinalizeTimeoutError`).

#### What is DynamoDB for?

In-flight, mutable upload state. Iceberg only gets finalized records ([ADR-0010](adr/0010-dynamodb-upload-sessions.md)).

- An upload is not one event: a DICOM series is thousands of files; a SpikeGLX `.meta` is useless without its
  `.bin`. Something must hold progress, checksums, leases, attempt counts and each step's outcome.
- Per-item updates are what DynamoDB is good at. In Iceberg, every small update would pile up snapshots and
  delete files, and corpus queries would see half-uploaded assets.
- Zero idle cost (on-demand), no VPC, and abandoned sessions expire by TTL (`expires_at`).
- It is also the claim-once and lease machinery that makes retries safe (conditional updates; `make reclaim`
  for stuck sessions). Import runs use the same table.
- Tradeoff: in-flight uploads are invisible to Athena and DuckDB, by design.

#### How is the lakehouse written and read?

14 Iceberg v2 tables registered in Glue (`assets`, `datasets`, `subjects`, `sessions`, `series`, `units`,
`unit_spikes`, `events`, ...) over S3. They are Glue-managed, not S3 Tables. CloudFormation could not create
the Iceberg table, so `make catalog-init` creates and evolves them through PyIceberg.

**Write:**

- Only two writers: commit (`assets`) and extract (detail tables), via PyIceberg.
- Each is fed by an SQS FIFO queue with one message group, so each writer runs alone. That is the concurrency
  control.
- Writes are idempotent. Asset id is `uuid5(raw_key@version_id)` with check-before-append. Extract uses
  `CatalogWriter.replace` (delete + append in one transaction per table; atomic per table, not across tables).
- `make reextract` backfills the whole catalog (Step Functions fan-out, still one committer).

**Read:**

- Athena: corpus SQL, server-side, from the API (`athena_query.py`). Every cell comes back as text and is
  parsed back into types.
- SDK: DuckDB and Polars read the tables directly (via PyIceberg) with temporary credentials from a Cognito
  identity pool ([ADR-0017](adr/0017-identity-pool-data-access.md)), plus ranged reads of raw files.
- Every result can cite the snapshot id it read, so the query re-runs on identical data.

**Questions**

- *I uploaded a file; why can't I query it yet?* The session must reach COMMITTED, then extraction must run.
- *Why two stores?* DynamoDB holds mutable in-flight state; Iceberg holds only finalized records (above).
- *Where are my bytes?* `landing/`, then `raw/`. They never pass through the API.
- *Athena or SDK for reads?* Athena for server-side corpus SQL behind the API; the SDK reads directly. See
  [section 7](#7-how-much-data-can-the-sdk-load).
- *What does a snapshot id buy me?* A query that re-runs on identical data.

---

### 2. File formats: what is actually materialized?

**Short version:** your files are never transcoded. NWB, NIfTI and everything else are stored byte for byte.
The platform adds Iceberg tables (Parquet), a few compact binary LFP files, and JSON documents.

| Store | What | Format | Writer |
|---|---|---|---|
| raw `landing/` then `raw/` | Your file, exactly as sent; version-pinned, Object Lock | original bytes | presigned PUT, then commit copy |
| warehouse | 14 Iceberg v2 tables | **Parquet** data files (PyIceberg's only write format; not set explicitly) + Iceberg JSON/Avro metadata | commit + extract writers |
| derived `lfp/` | per NWB per series: `index.bin`, `overview.bin`; `manifest.json` last | custom binary | `LfpBuildFunction` |
| derived `summary/` | per dataset: `_dataset.json`, `manifest.json`, `units.json`, `events.json`, per-session parts | JSON | summary build after each import or re-extract |
| derived `rows/` | re-extract bundles `rows.jsonl.gz` + manifest; expire after 7 days | gzipped JSON lines | `DeriveFunction` |
| artifacts | your saved file + `provenance.json`; lookup lists `index/by-id`, `by-input`, `by-time` | file as-is + JSON | `save_artifact` / `PlatformFunction` |

Also: the site bucket (built app, SDK wheel, notebook, handout `manifest.json`, and the `index.json` public
DANDI/OpenNeuro list from `make harvest`); Athena results (Athena's CSV); upload sessions live in DynamoDB,
not S3.

**Custom binary formats (LFP):**

- `index.bin` (v1, `lfp_index.py`): an 8-byte `ASTLFPIX` magic, a u32 header length, a JSON header, then three
  little-endian arrays over the HDF5 chunk grid: byte offset (u64), stored size (u32), filter mask (u32). That
  is 16 bytes per chunk (a 732k x 56 series is about 57 KB). It is enough to read any window **without h5py**.
  Only deflate or no filter; shuffle is refused.
- `overview.bin` (`lfp_overview.py`): a per-channel spectrogram, 1 s steps, 64 log bands from 1 to 250 Hz,
  float16 dB, laid out [channel, time, band] so one channel is one contiguous byte range.

**What the format decides** (commit maps the extension to `nwb`, `nifti`, `json`, `tsv`, `csv` or `other`):

| Input | Materialized beyond the raw copy |
|---|---|
| NWB | `series`, `electrodes`, `units`, `unit_spikes`, `intervals`, `subjects`, `sessions`, `asset_headers`; LFP index + overview; feeds summaries |
| NIfTI | `asset_headers` only |
| BIDS `*_events.tsv` | `events` |
| `participants.tsv` | `subjects` |
| BIDS JSON sidecar | `asset_headers` |
| `.csv`, anything else | `assets` row only (SKIPPED) |

Notes: bulk signals (raw ephys, LFP samples, voxels) exist only in the original files. Spike times are the one
bulk-ish thing copied into the catalog, so spike compute does not need the NWB. The SDK caches nothing to
disk. Everything except artifacts is rebuildable from `raw/` (`make reextract`, the LFP rebuild message,
`make summaries`).

**Questions**

- *Does the platform convert my NWB?* No; it stores it byte for byte and derives tables beside it.
- *Can I upload CSV?* Yes, but it only gets an `assets` row.
- *What is rebuildable if derived data is lost?* Everything except artifacts, from `raw/`.
- *Where do saved results live?* In the artifacts store, as your file plus `provenance.json`.

---

### 3. Tech selection: why Iceberg and CloudFront?

**Short version:** serverless, zero idle cost and stable CDK constructs drove both choices. The common thread
could be one "design principles" paragraph.

#### Iceberg (+ Athena, DuckDB, Polars)

See [ADR-0003](adr/0003-iceberg-athena-catalog.md). The catalog is small (metadata about files, not the
files). The starting requirement was "AWS duckdb if possible, or rds, and glue". Deciding needs: no idle cost,
reproducible queries, and direct reads from the researcher's own Python.

- **No idle cost:** tables are S3 files plus Glue entries (first million objects and requests free). Athena
  bills per TB scanned and nothing when idle.
- **Reproducibility:** each commit is a snapshot, and a result cites the snapshot id it read. That is the base
  of the provenance model.
- **Open format, many engines:** Athena server-side, DuckDB and Polars locally; no engine lock-in.
- **Python write path:** PyIceberg append, overwrite and upsert for the ingest Lambdas.

Rejected: a Postgres-backed catalog (Aurora Serverless v2, or DuckLake/Lakekeeper/Polaris, which need
Postgres), because it is an always-on VPC database for metadata-sized tables, and Aurora bills storage while
paused and takes about 15 s to resume. Snowflake and Databricks: cost and lock-in at this scale (see
[section 13](#13-why-not-snowflake-or-dbt)). Hosted DuckDB on AWS: no AWS-managed offering found, so DuckDB
stays as a reader.

Paid for: pre-1.0 write libraries and contradictory docs; CloudFormation could not create the table (hence
`make catalog-init`); concurrent commits are not assumed safe (that test never ran), answered by a
single-writer FIFO per writer; S3 Tables were never evaluated, so tables are Glue-managed and the ADR stays
Provisional. It pairs with DynamoDB: Iceberg is bad at frequent small updates, so in-flight state lives in
DynamoDB.

#### CloudFront (S3 SPA with OAC)

See [ADR-0007](adr/0007-s3-cloudfront-spa.md). The starting requirement was "amplify or fargate/other as appropriate". The app
needs no server-side rendering.

- No servers, and near-zero idle cost (CloudFront free flat-rate plan: 1M requests, 100 GB).
- Stable CDK: `S3BucketOrigin.with_origin_access_control` keeps the bucket private (OAI is legacy).
- Deep links come free from the 403/404 to `index.html` fallback.

Rejected: Amplify Hosting (its CDK construct is an experimental alpha package) and Fargate (no SSR need).

Paid for: everything runs in the browser, so there are no secrets in the app and every protected action goes
through the API; downloads are 300 s presigned GETs; a tab opened before a publish can request deleted chunks
(the app shows "Reload to continue"); no WAF and no custom domain.

**Questions**

- *Why not just Postgres?* An always-on VPC database for metadata-sized tables, with no snapshots to cite.
- *What is a snapshot id and why does every result carry one?* The Iceberg commit a result read; it makes the
  query reproducible.
- *Why both Athena and DuckDB?* Athena runs corpus SQL server-side behind the API; DuckDB reads the same
  tables from the researcher's machine.
- *Why not Amplify?* Its CDK construct is an experimental alpha, and the app needs no SSR.

---

## Part 2: Data types

### 4. Spikes, LFP and MRI

**Short version:** spike times are copied into the catalog; LFP stays in the file with a precomputed index and
overview; MRI keeps header fields only.

#### Single-unit spikes (NWB)

**Extract:** `ExtractFunction` reads the NWB with `pynwb` and `remfile` over ranged reads at the pinned version
(no download) and fills `units` (one summary row per unit, with location), `unit_spikes` (one row per unit,
all spike times as `list<double>`), `electrodes`, `series`, `intervals`, `subjects` and `sessions`. Spike times
live **in the catalog**. Every analysis time is a double in seconds.

**Compute:** all arithmetic is in one module, `asteroidea.analysis`, used by both the SDK and the API, so the
browser and Python agree.

- Web: `GET /compute/rates` reads units and spikes via Athena (it falls back to the NWB file if a unit has no
  `unit_spikes` rows, and reports `catalog` or `file`), aligns to events, labels or frames, bins, and applies
  the responsive screen. The body is capped at 5 MiB. `GET /compute/tuning` serves a tuning screen.
- SDK: `load_session(...)` gives `.units`, `.spikes`, `.rates()`, `.tuning()` and `.to_pynapple()`.
- A stored per-session summary in the derived bucket feeds the session page and `session_summary()`.

**Events:** from `_events.tsv` (the `events` table), NWB intervals, stimulus files or labels; a per-dataset
JSON recipe resolves each event type.

#### LFP

Too big for the catalog, so an index instead of rows.

- After extraction, an NWB is queued to `LfpBuildFunction`. It opens the file once with h5py over ranged reads
  and, per `ElectricalSeries`, writes a chunk-offset index (the byte range of every chunk) and an overview (a
  coarse whole-recording spectrogram, 1 s segments) to `derived/lfp/<asset>/<version>/...`, manifest last. The
  largest test file took 54.6 s.
- Unindexable series (no rate, no chunks, shuffle filter, non-numeric) are skipped, with the reason in the
  manifest. Until the manifest exists, LFP routes answer 404 `overview_not_built`.
- Read: `/compute/overview` returns the precomputed overview. `/compute/spectrogram` plans byte ranges from the
  index (a 60 s window is 6 requests, about 470 KB), decodes without h5py and runs `analysis.spectrogram`. The
  window maximum is 1,200 s.
- SDK: `Session.lfp(series, channels, start_s, duration_s)` and `Session.band_power(event_type)` (`nwb` extra).

#### MRI (NIfTI)

Headers only.

- Extraction uses `nibabel` and fills `asset_headers` (shape, datatype, ...). No voxel data goes in the
  catalog. BIDS sidecars and `participants.tsv` add metadata and subjects.
- The web Image tab fetches the whole file once by presigned GET (version-pinned) and renders it with NiiVue
  (a lazy-loaded chunk): three orthogonal slices respecting the scan's own affine, with a readout in mm,
  voxel and intensity. It refuses a file over 64 MiB compressed, over 512 MiB decompressed (from the catalog
  header), or 4D.
- SDK: `open_image(asset_id)` hands a lazy file to nibabel; `img.dataobj[:, :, 48]` fetches only that slice
  (one slice of a 3 MB file: 33 KB in 1.4 to 3.9 s).
- Gaps: no MRI analysis; no QC or age filter (neither NIfTI nor BIDS defines a QC field). MRI ties to ephys
  through participants: `participants(has=["anat","units"])`.

| | Catalog holds | Bulk data | Derived |
|---|---|---|---|
| Spikes | units, all spike times, electrodes, intervals, events | NWB in `raw/` | session summaries |
| LFP | series metadata | NWB in `raw/`, read by byte range | chunk index + overview |
| MRI | header fields | NIfTI in `raw/`, read by slice or whole | none |

**Questions**

- *Why does the LFP tab say "overview not built"?* The `LfpBuildFunction` manifest does not exist yet, or the
  series was skipped as unindexable (the reason is in the manifest).
- *Why does the Image tab refuse my scan?* It is over 64 MiB compressed, over 512 MiB decompressed, or 4D.
- *Are spike times in the catalog or the file?* In the catalog (`unit_spikes`).
- *Will the web app and Python give the same PSTH?* Yes; both run `asteroidea.analysis`.
- *Which file types get catalog rows?* See the table in [section 2](#2-file-formats-what-is-actually-materialized).

---

## Part 3: The SDK

### 5. The SDK: install, data access and access logs

**Short answer:** a Python package, `asteroidea`, that reads S3 and the Iceberg catalog directly with temporary
AWS credentials tied to your sign-in. Those reads leave almost no trail the platform records: S3, CloudFront
and API Gateway access logs are deliberately off for now. What is recorded is what you write (uploads,
results, labels) and the API calls you make.

**Package:** `asteroidea` 0.1.0, Apache-2.0, Python 3.14 or newer. Core pins: `boto3`, `awscrt` (CRC64NVME),
`pyiceberg[pyarrow,glue]` (`query()`) and `duckdb` (`sql()`). Extras: `nifti` (nibabel), `analysis` (polars,
numpy), `nwb` (pynwb, h5py) and `pynapple`. The entry point is `asteroidea-setup` (see
[section 6](#6-what-a-researcher-can-do-through-the-sdk-alone)). The wheel is served from the hosted app and
checksum-verified. The pins are in the [developer guide](developers.md).

**What each dependency is for:**

| Dependency | What the SDK uses it for |
|---|---|
| `boto3` | The SDK's AWS calls. The Cognito identity-pool credential exchange (`_creds.py :: identity_client`), the ranged S3 `GetObject` reads of raw files (`_remote.py`, one ranged GET per read) and the Glue `GetTable` calls that find each catalog table's Iceberg metadata (`_sql.py :: metadata_locations`). Uploads do not use it: file bytes go to the presigned URL over plain HTTP (`_upload.py`, `_http.py`). |
| `awscrt` | Computes the CRC64NVME checksum of a file before upload (`_crc.py :: crc64nvme`). |
| `pyiceberg[pyarrow,glue]` | Reads the Iceberg catalog through Glue with the researcher's credentials. `query()` scans the `assets` table and returns file references with the snapshot id; `snapshot_id=` scans an older snapshot (`_query.py`). pyarrow carries the Arrow tables the SDK returns. |
| `duckdb` | The local SQL engine behind `sql()`. The browse helpers (`datasets()`, `subjects()`, `sessions()`, `assets()`) and the reads inside `load_session` go through `sql()` (`_browse.py`, `_session.py`). It installs its Iceberg and S3 extensions, binds one view per catalog table over that table's Iceberg metadata, and rebinds a view when the metadata location changes (`_sql.py :: Engine`). |

Optional extras, each imported only when used:

- `nifti` (nibabel): lazy NIfTI reads, `open_image` (`_image.py`).
- `nwb` (pynwb, h5py): lazy NWB reads, `open_nwb`, LFP windows (`_nwb.py`).
- `analysis` (polars, numpy): `asteroidea.analysis`, the module the compute Lambda image also copies in, so
  `rates`, `tuning` and `band_power` compute locally with the same code (`analysis.py`).
- `pynapple`: `to_pynapple()` (`_pynapple.py`).

`asteroidea-setup` installs the extras `nwb` and `nifti` (`_setup.py :: EXTRAS`). It then installs the
platform's notebook pins, which include polars, pynapple, matplotlib, JupyterLab and ipykernel
(`notebook.py :: NOTEBOOK_PINS`), so a set-up environment can use `analysis` and `to_pynapple()` as well. A
plain `pip install asteroidea` gets only the core four.

**Data access path:**

1. Sign in with Cognito (copy-paste URL + PKCE); the ID token is cached locally
   ([ADR-0009](adr/0009-sdk-copy-paste-login.md)).
2. A Cognito identity pool exchanges the ID token for temporary `ResearcherRole` credentials, cached and
   refreshed before expiry (`_creds.py`; [ADR-0017](adr/0017-identity-pool-data-access.md)).
3. Direct reads: `s3:GetObject` and `GetObjectVersion` on `raw/` (ListBucket only under `raw/*`); Glue
   `GetDatabase(s)` and `GetTable(s)` plus warehouse reads for DuckDB and PyIceberg; Athena start, get and
   stop on the workgroup (granted; the SDK does not currently use it). A separate grant allows upload Put and
   Get. All of it is read-only on data.
4. Everything else goes through the API with the ID token; API functions use their own roles.

No SDK read passes through platform code.

**What gets recorded:**

- *Platform state you create:* DynamoDB upload sessions (`owner_sub`, dataset, path, size, checksum,
  timestamps, states; TTL-expired). `assets` rows (provenance, kept in snapshots). Dataset ownership (the first
  uploader's sub). Results: `provenance.json` with author sub and email, inputs, snapshot id, time and note.
  Inside `run()` it also holds every query, SQL snapshot, file opened and result read during the run; that is
  the one place reads get recorded, and only on save. Result records never expire. Labels and links record who
  and when.
- *CloudWatch Logs (API and Lambda only):* one structured timing line per API request (route, phases, cold
  start, status, `asset_id`, request id), which by design **never carries a token, user sub or URL**
  (`timing_log.py`). The logs show that an asset was requested via the API, not by whom. The re-extract state
  machine logs executions in full. Athena workgroup metrics hold counts, not users.
- *Explicitly not recorded* (cdk-nag acknowledgements in `nag.py`: `AwsSolutions-S1`, `AwsSolutions-CFR3` and
  `AwsSolutions-APIG1`, each with a reason of the form "no reader in a single-researcher sandbox; revisit with
  the audit work"): S3 server access logs (no record of who read a raw file), CloudFront access logs, and API
  Gateway access logs.
- *Outside the repo (unverified):* the repo defines no CloudTrail trail (a case-insensitive grep of
  `infra/src` for `cloudtrail` returned nothing). AWS's default 90-day Event history records management
  events, so Cognito credential exchanges and probably Glue `GetTable` calls likely appear there. S3 object
  reads are data events, which are not logged unless a trail is configured for them. Whether the sandbox
  account has an org or account trail needs a live check with the sandbox profile.

**Net:** who uploaded what, and who saved which result from what, is knowable. Who read which raw file or
catalog rows is not, unless it was done inside a saved `run()`. This read-audit gap is worth stating plainly.

**Questions**

- *What does setup put on my machine?* `platform.json`, a Python 3.14 environment, the SDK with readers, a
  kernel and the exemplar notebook ([section 6](#6-what-a-researcher-can-do-through-the-sdk-alone)).
- *Why does the SDK install DuckDB? Isn't the data already cataloged in the platform?* Yes, it is
  cataloged. But the catalog is stored data, not a query server: Iceberg is Parquet data files plus metadata
  in S3, registered in Glue. Something has to run the SQL. Server-side that is Athena, which the API uses. In
  the SDK it is DuckDB, running on your machine over the same Parquet files, with your temporary credentials
  (`_sql.py`). Compared with calling Athena, that has no per-query scan charge, no query-queue wait, and
  returns local Arrow tables that go straight into pandas or polars; `query()` can also pin an older
  snapshot. `ResearcherRole` is granted Athena actions (`identity.py`), but nothing in `sdk/src` calls
  Athena (a case-insensitive grep returned nothing). So Iceberg is the format, the platform catalogs the
  data, and DuckDB is the engine that queries it locally.
- *Does the SDK go through the API?* Reads of S3 and the catalog do not; uploads, results,
  `participants()` and `session_summary()` do.
- *Is my data access logged?* Mostly not (above).
- *Can the SDK write to the catalog?* No; writes go through upload sessions.

---

### 6. What a researcher can do through the SDK alone

**Short version:** nearly everything except the DANDI/OpenNeuro survey, curator imports and label writing.
The researcher-facing reference is [For researchers](researchers.md).

**Setup:** one command from the app's Get started page
(`uvx --from '<host>/sdk/<wheel>' asteroidea-setup <host>`). It saves `~/.config/asteroidea/platform.json`,
signs in (paste the login URL back), creates `~/asteroidea-env` (Python 3.14), installs the SDK with the
NWB and NIfTI readers, registers a kernel and downloads the exemplar notebook. Then `asteroidea.sign_in()`.

**Discover data:** `datasets()`, `subjects()`, `sessions()`, `studies(tier=)`, `participants(has=["anat","units"])`
(participants with MRI *and* ephys), `links()`, `assets()`, `query(**filters)` (rows + snapshot id) and
`sql("SELECT ...")` (DuckDB over catalog views).

- `studies()` and `links()` are local DuckDB over the catalog (`_studies.py :: studies`, `links`).
- `participants()` is different: it reads the API routes `GET /catalog/studies` and `GET /catalog/links` (the
  same two answers the web app's home page reads), so it takes seconds, and it raises `QueryError` when more
  than 5000 links match (pass `study=` to narrow it). A dataset's modalities are those of its last stored
  summary.

**Read files lazily:** `open_nwb`, `open_image` (NIfTI) and `open_file` (any format); only touched bytes are
fetched.

**Upload:** `upload(path, dataset=...)`, `upload_dataset(root, ...)`, and `sandbox=True` for test data. The
first uploader owns a dataset name; only the owner adds files.

**Analyze a session:** `load_session(dataset, subject, session)` gives units, spikes, events, series and LFP;
`.rates()` (PSTH + responsive screen), `.tuning()`, `.band_power()` and `.to_pynapple()`.
`session_summary()` reads the stored summary the web session page draws. These are **exploratory screens**
(raw p + BH q), not finished analyses.

**Save results with provenance:** `save_artifact(path, sources=..., snapshot_id=...)`. Inside
`with asteroidea.run(name, params):` every query, SQL snapshot, file opened and result read is recorded
automatically, with the analysis defaults used. Then `artifacts()`, `open_artifact()`, `download_artifact()`
and `withdraw_artifact()`. Every researcher can read every result.

**Probably web-only** (inferred from the API surface, not verified): Discover (the DANDI/OpenNeuro survey),
curator imports, writing labels (`labels()` is read-only in the SDK), and renaming or deleting results (the
docs say the author can; the SDK exports only `withdraw_artifact`).

**Docs drift:** `researchers.md` ("Signing in to the web app") still says the app has
four pages (Data, Discover, My results, Get started). The router (`web/src/router/routes.ts :: ROUTES`) now
registers `/`, `/s/:study`, `/d/:dataset`, a participant page, a session page, `/uploads`, `/files/:asset_id`,
Discover and its imports, Results and a result page, and Start. The drift holds; fixing it is outside this
page.


**Questions**

- *How do I get set up?* The Get started page, then `sign_in()`.
- *Can I do everything from Python?* Not the web-only items above.
- *How do I make a result reproducible?* Save it inside `run()` with the snapshot id.
- *Who can see my uploads and results?* Every signed-in researcher can read every result; only the dataset
  owner adds files to a dataset.
- *Is a `rates()` result publishable?* No; it is an exploratory screen.

---

### 7. How much data can the SDK load?

This covers API Gateway, Lambda and Athena limits.

**Short answer:** SDK reads do **not** go through API Gateway, Lambda or Athena. They read S3 and Iceberg
directly with temporary AWS credentials, so service limits do not apply to reads; your machine's memory does.
The API is used for uploads, results and a few summary reads, and carries small JSON, never file bytes.

| SDK call | Path | Limits |
|---|---|---|
| `sql()`, `datasets()`, `subjects()`, `sessions()`, `studies()`, `links()`, `assets()`, `load_session()` | local DuckDB over Iceberg in S3 | memory, S3 bandwidth |
| `query()` | local PyIceberg scan | same |
| `participants()` | two API reads (`GET /catalog/studies`, `GET /catalog/links`) | API body caps; 5000 links |
| `open_file`, `open_nwb`, `open_image`, `Session.lfp`, `band_power` | ranged S3 `GetObject` on the raw file | none from the API |
| `upload`, `upload_dataset` | JSON via API; bytes by presigned PUT | 5 GiB per file |
| `save_artifact`, `artifacts`, `open_artifact`, `withdraw_artifact` | JSON via API; file by presigned PUT/GET | small bodies |
| `session_summary()` | `GET /catalog/session-summary` | API body caps |

The SDK never calls `/compute/*`; `rates`, `tuning` and `band_power` run the same `asteroidea.analysis`
locally. Only the web app meets the 5 MiB compute cap.

**How much you can load:**

- Catalog: `sql()` returns an in-memory Arrow table, so RAM is the limit. DuckDB reads only the needed columns
  and files; the catalog is metadata plus spike times.
- Raw files: never loaded whole unless asked. In `open_file`, each `read(n)` is one ranged GET with no cache.
  `open_nwb` and `.nii.gz` use a block cache of **4 MiB blocks, at most 64 (256 MiB)**, LRU
  (`_blocks.py :: BlockFile`, `BLOCK_SIZE`, `MAX_BLOCKS`), so memory is bounded regardless of file size.
  `open_image` on `.nii` reads just the slice indexed. `Session.lfp(..., start_s, duration_s)` reads only that
  window.
- Long reads: credentials refresh before expiry (`_creds.py`); an expiry mid-read refreshes and retries the
  range once, else `ReadInterruptedError`.
- Uploads: **1 B to 5 GiB** per file (the single-PUT limit; `upload_rules.py :: MAX_SIZE`, checked in
  `uploads_api.py`); multipart is not built. Dataset metadata is at most 4 KiB. The finalize wait is 60 s.

**API side (the web app and the few SDK API calls):**

- API Gateway: 10 MB request bodies and a **30 s integration timeout** (503). Errors have a fixed shape
  (`error` code + message).
- Lambda's 6 MiB response limit: functions cap bodies at **5 MiB** (`catalog_api.py :: MAX_BODY_BYTES`, and the
  same value in `compute_api.py`) and return **413 `too_large`** naming the fix (fewer units, a shorter
  window, `raster=0`). Compute routes estimate size from spike counts before building.
- Bigger payloads bypass Lambda: downloads, large LFP overviews and artifact files are presigned S3 URLs.
- Lambda time: compute routes return **503 `slow`** when the remaining time minus 2 s runs out; the
  spectrogram window is at most 1,200 s. Commit is async behind a queue.
- Athena: API-only. Fixed SQL with execution parameters; over budget gives **503**; a failure gives
  **502 `upstream_failed`**; asset rows are cached for 300 s; billed per TB scanned.

**Questions**

- *Is there a query limit from Python?* No API limit. RAM is the limit, so push filters into SQL.
- *Can I upload more than 5 GiB?* Not yet.
- *Why did the web app say "too large" when the SDK worked?* The web app goes through the 5 MiB API cap; the
  SDK reads S3 directly.
- *Will a 20 GB NWB fill my memory?* No; the block cache caps at 256 MiB.

---

## Part 4: The web app

### 8. The web app: definition and deployment

**Short version:** a static React single-page app on S3 behind CloudFront, published separately from the
infrastructure deploy.

**Definition:** `web/` is React 19, Vite and TypeScript, built with npm, outside the uv workspace. Gates:
`make web-check` (Biome, strict `tsc`, Vitest, Vite build, bundle-size cap) and `make web-e2e` (Playwright,
headless Chromium at 1440 x 900).

Rules: it draws what the API returns. It may compute over returned data for interactive inspection; saved
results and provenance come only from the Python analysis module. PKCE sign-in to Cognito, with tokens in
`sessionStorage`; the sign-in control is hidden when the three `VITE_*` names are unset.

**Hosting** ([ADR-0007](adr/0007-s3-cloudfront-spa.md)): a static SPA in a private S3 bucket behind CloudFront
with origin access control (`web_hosting.py :: WebHosting`, built in **DataStack**: `data_stack.py` creates
`WebHosting(self, "Web")`; the ADR's body says ServicesStack, and a later update of the ADR moved it). No
servers; data comes from API Gateway and Lambda. Deep links work through CloudFront's 403/404 fallback to
`index.html`.

**Local dev:** `make web` (Vite on `WEB_PORT`, default 5173), `make web-kill`.

**Deploy:** separate steps.

1. `make deploy SANDBOX_TEST=1` creates or updates the bucket and distribution with the rest of the
   infrastructure.
2. `make web-publish` (live) builds with the deployed ids, uploads the app plus the public `platform.json` to
   `site/` and the SDK wheel, notebook and `manifest.json` to `handout/`, and invalidates CloudFront. It prints
   PASS or FAIL per step, and refuses first if `web/public/index.json`'s schema differs from the code (run
   `make harvest`).
3. `make live-web-smoke` visits every page headless after each publish.

When a change alters API error bodies, publish the web app **before** deploying: the new app and SDK read both
the old and new error body.

**Questions**

- *Why this stack?* See [section 3](#3-tech-selection-why-iceberg-and-cloudfront).
- *Why did the app say "Reload to continue"?* A tab opened before a publish asked for chunks that the publish
  deleted.

---

### 9. What are the web app's limits? How powerful a visualization could it run?

**Short version:** hosting is not the limit. A static SPA on CloudFront runs entirely in the researcher's
browser, so it is as capable as a modern browser (WebGL is already in use; WebGPU, workers and WASM are
available). The real limits are how data reaches the browser, a rule about what the app may compute, and the
app's own size budgets. The rendering capacities below are general browser figures, not measured in this repo.

**Built with today** (read from `web/package.json` and `web/bundle-budget.json`):

- Few libraries: React 19, TanStack Table, NiiVue (a WebGL MRI viewer) and fonts. No charting library.
- Hand-written plots in `web/src/plots` (`heatmap`, `raster`, `trace`, `curve`, `axis`): Canvas 2D for the
  pixels, with SVG on top for axes and overlays (`web/src/signals/Frame.tsx :: Frame`). Plot width is clamped
  to 280 to 1100 px.
- WebGL is already used for MRI through NiiVue, loaded as its own lazy chunk.
- Size budgets (`web/bundle-budget.json`): the main bundle is capped at 127.5 KB (it measures
  around 81 KB today). Each lazy chunk has its own cap (viewer 392.5 KB, signals 30 KB, research
  27 KB, and so on). `make web-check` fails over any cap. A heavy library can be added as an on-demand chunk
  with a new budget line.

**The real limits:**

1. **Getting data to the browser.** API responses are capped at 5 MiB of JSON
   (`catalog_api.py :: MAX_BODY_BYTES` and `compute_api.py :: MAX_BODY_BYTES`, under the 6 MiB buffered
   Lambda limit). The HTTP API integration limit is 30 s; the compute function times out at 28 s and the
   catalog function at 20 s (`timing.py`). A warm compute call took a median of 2.79 s in the live check
   ([runtime chapter](06-runtime.md)). So one interaction brings in about 5 MB, not 500 MB. Larger data
   must come by presigned S3 GET (whole MRI files up to 64 MiB compressed, LFP overviews, downloads), which
   scales well. The browser holds no AWS credentials (see [section 10](#10-could-the-web-app-read-tables-and-files-directly-like-the-sdk-why-doesnt-it)),
   so it cannot read Iceberg, Parquet or raw files itself.
2. **The compute rule.** The app draws what the API returns. It may compute over returned data for
   interactive inspection (axes, filters, scaling, one value against another). Saved results and provenance
   come only from the Python analysis module, so binning, statistics and spectra are computed server-side by
   `asteroidea.analysis`.
3. **Server compute behind it.** `ComputeFunction` is a Lambda container configured with 2048 MB and a 28 s
   timeout (`functions.py`, `timing.py`). Lambda itself allows up to 15 minutes, but not behind the 30 s API
   limit. Heavier work needs an async job pattern (submit, poll, collect from S3) that does not exist for the
   web app.
4. **No push channel.** Plain HTTP request and response; no WebSocket API. Live or streaming views need new
   infrastructure.
5. **Browser memory.** A tab practically uses about 1 to 4 GB (general figure). The MRI viewer refuses a file
   over 512 MiB decompressed, and any 4D file, for this reason (`web/src/mri/mri.ts ::
   MAX_DECOMPRESSED_BYTES`, `MAX_COMPRESSED_BYTES`).

**How powerful** (general figures, not measured here):

| Technology | Comfortable scale | Here today |
|---|---|---|
| SVG | thousands of elements | axes, overlays |
| Canvas 2D | about 10^5 to 10^6 points per redraw; image-sized rasters and heatmaps | rasters, PSTH heatmaps, spectrograms, traces |
| WebGL2 | millions of points or triangles at 60 fps; volume rendering, meshes | NiiVue slice views (inferred; NiiVue can also do 3D, meshes and overlays) |
| WebGPU | compute shaders: GPU binning, filtering, level-of-detail | not used |
| Workers + WASM | off-thread decoding; DuckDB-WASM or Arrow over Parquet | not used |

Realistic within the current limits:

- Spike rasters of hundreds of units by thousands of trials, if the server sends compact binned or binary data
  rather than JSON lists of spike times (JSON hits 5 MiB first).
- LFP traces and spectrograms over long recordings, by the existing pattern: precomputed binary overviews by
  presigned GET, exact windows on zoom.
- 3D brain views: an MRI volume with electrode positions or unit locations overlaid, or surface meshes (NiiVue
  supports these; the work is mostly supplying coordinates).
- Linked interactive views: brushing a time range updates rasters, spectra and the MRI crosshair (browser-side
  inspection, which the rule allows).
- Large tables with sort and filter. TanStack Table is headless; row virtualization is not in the app today
  (unverified that it is needed at the current catalog size), so it would be a new dependency.

Needs changes:

- Rendering raw data far beyond 5 MiB per view: precompute binary tiles or pyramids into the derived bucket
  and serve them by presigned URL, as `overview.bin` already does (`lfp_build.py`; the same pyramid design as
  the [continuous-data notes](#14-design-notes-a-continuous-data-source-not-built)).
- Analysis in the browser (for example DuckDB-WASM over Parquet): conflicts with no-browser-credentials and
  analysis-only-in-Python, so it needs a maintainer decision.
- Live streaming: a WebSocket or streaming API.
- Long computations behind a button: an async job path.

**Bottom line:** drawing could handle almost anything a desktop neuroscience viewer shows. The limit is about
5 MB per API call within 30 s, plus "the browser only draws server results". Precomputed binary by presigned
URL is the existing workaround and the lever for anything bigger.

**Questions**

- *Why doesn't the web app compute X itself?* Python is the one source of results; the app only draws and
  inspects.
- *Why does the app say "too large"?* The 5 MiB JSON cap on an API response.
- *Could we add 3D brain or electrode views?* Yes; NiiVue supports them.
- *Why no live view?* There is no push channel.
- *Can I add a charting library?* Yes, as a lazy chunk with its own budget line.

---

### 10. Could the web app read tables and files directly like the SDK? Why doesn't it?

**Short version:** technically yes; nothing in hosting prevents it, and the identity pool that gives the SDK
credentials could give them to the browser. It was deliberately not given that path. The file-access decision,
recorded in [ADR-0017](adr/0017-identity-pool-data-access.md), chose "Presign route" and
rejected "Identity-pool creds in browser".

**What was decided:** the web app uses no AWS credentials. It signs in with the Cognito access token only;
every byte comes through an API route (JSON) or a presigned URL the API issued (MRI volumes, LFP overviews,
downloads). The presigned GET lasts 300 s and is pinned to the catalogued object version; the raw bucket has
one CORS rule for this (GET and HEAD with `Range`, for the hosted origin and `http://localhost:5173`; see the
[ADR-0007](adr/0007-s3-cloudfront-spa.md) update). Identity-pool credentials remain the SDK's path (`sql()`,
lazy reads).

Options considered: (1) identity-pool credentials in the browser (rejected); (2) no direct file bytes at all,
everything as API JSON (rejected: MRI volumes and LFP overviews are too big); (3) the presign route (chosen).

**Why** (inferred; the decision records only the option label):

1. **One analysis implementation.** [ADR-0020](adr/0020-research-frontend-analysis.md) makes the browser a
   drawing surface and `asteroidea.analysis` the only place numbers are computed, so web and SDK "cannot
   disagree about a rate or a spectrogram". Direct table reads invite JS or WASM arithmetic, which the ADR
   rejects ("a second implementation of every function").
2. **NWB in the browser is impractical.** Browser analysis would need NWB/HDF5 reads (ruled out earlier, in
   the dataset browser design). LFP files are 0.5 to 1 GB with awkward gzip chunks (unverified). Through the
   server index and byte-range route, a zoomed 60 s window
   costs 6 requests and 0.47 MB, instead of the 17 to 22 MiB file open that an earlier measurement found (ADR-0020).
3. **Credentials and attack surface.** AWS credentials in a tab are readable by any script running there. No
   WAF is attached ([ADR-0007](adr/0007-s3-cloudfront-spa.md) update); whether a CSP is set was not
   checked. A presigned URL is one object, one version, 300 s; identity-pool credentials grant
   `ResearcherRole` (every raw object, Glue, Athena) for as long as the credentials live (about an hour is a
   general AWS figure, unverified here). Sign-in tokens are in `sessionStorage` for the same reason.
4. **A smaller, controlled interface.** The browser's data access is a fixed list of routes behind the JWT
   authorizer, which enforce limits (5 MiB, window caps, refusals) and can see requests. Compute, labels and
   imports need no `ResearcherRole`.
5. **Practical friction.** Threaded DuckDB-WASM needs `SharedArrayBuffer`, which needs COOP/COEP headers on
   CloudFront (ADR-0020's "Next hosting tier" table; nothing forces it yet). PyIceberg has no browser
   equivalent. Iceberg metadata in the browser would need CORS on the warehouse bucket and the Glue API,
   neither of which is set up.

**What it would take to change:**

- Feasible: a JS/WASM reader (DuckDB-WASM over Parquet, an Iceberg client), identity-pool credentials in the
  browser, CORS on the warehouse bucket and Glue, and COOP/COEP for threaded WASM. A medium-sized piece of work
  (estimate).
- Reopens: the one-implementation rule (or a strict "browser reads, never computes" line); credentials in the
  browser together with the missing read-audit trail ([section 5](#5-the-sdk-install-data-access-and-access-logs));
  the no-compute-in-web policy.
- The middle path the design already favours: keep reads server-mediated, but ship bulk binary (precomputed
  tiles or pyramids, Arrow or Parquet extracts) by presigned URL, and add Lambda response streaming
  (ADR-0020's next tier, stated as up to 200 MB) for results over 5 MB. That gives most of the power of
  direct reads without credentials in the tab.

**Questions**

- *Why can't the web app query the catalog like the SDK?* No AWS credentials in the browser, and one analysis
  implementation.
- *Is a presigned URL safe?* It is one object, one pinned version, for 5 minutes.
- *What would direct browser reads take?* WASM readers, browser credentials, CORS and COOP/COEP; it reopens
  two recorded decisions.

---

### 11. If the browser read directly, would we still need the API layer? What is its value?

**Short version:** yes. Direct reads would replace only the read-only catalog routes that serve the browser.
Everything else stays: writes that must be controlled, compute that must run in the one Python implementation,
and rules that cannot live in browser code. The SDK already shows this: it reads directly and still calls the
API for uploads, results and summaries. Route groups below are by area, not an exhaustive list (see
`ingest/src/asteroidea_ingest/routes.py :: ROUTES`).

**What direct reads would replace:** read-only browsing, mostly Athena queries in `CatalogFunction`: dataset,
subject, session and asset listings, studies, links and header rows (`/catalog/*`); possibly summary reads
(`/catalog/session-summary`, JSON in the derived bucket); possibly the presign step for downloads, MRI volumes
and LFP overviews. The win: no 5 MiB cap on reads, no Athena latency or per-scan cost, no 30 s limit.

**What the API would still do:**

1. **Controlled writes (the main reason).** Researchers have no write access to the catalog or raw data; the
   API is the only writer path.
   - Uploads (`/sessions`, finalize, `/uploads`): the claim-once state machine, checksum verification,
     dataset-ownership checks, the 5 GiB cap and the queued, serialized commit (see
     [section 1](#1-data-ingestion-upload-to-catalog)).
   - Imports (`/imports`): curator-only, a server-side group check.
   - Results (`/artifacts`): create, finalize, rename, withdraw and delete; edits by the author only; "a result that
     another result used cannot be deleted"; the provenance record.
   - Labels and event periods (`/files/{id}/labels`) and dev notes (`/notes`).

   Browser write credentials would move these rules into bypassable browser code. IAM can say which objects,
   not "only the owner of this dataset name" or "not if it has dependents".
2. **Compute in the one implementation.** `/compute/rates`, `tuning`, `events`, `spectrogram` and `overview`
   run `asteroidea.analysis` next to the data. Unless the "browser only draws" rule changes too, compute
   stays a server call; only catalog browsing moves.
3. **Authorization richer than IAM.** Curators-group checks, ownership, edits by the author only, and "another
   caller's import run reads as 404" (that last behaviour is not rechecked).
   Identity-pool credentials give one role per group; per-record rules stay on the server. If per-subject
   access arrives (see the [continuous-data notes](#14-design-notes-a-continuous-data-source-not-built)),
   direct reads get much harder and the API becomes the filter.
4. **A stable contract and limits.** Fixed error codes (`too_large`, `slow`, `not_nwb`, ...) that the app and
   SDK branch on; versioned bodies (the publish-before-deploy rule in [section 8](#8-the-web-app-definition-and-deployment)
   exists because of them); limits and refusals in one place. Direct reads couple the browser to the physical
   layout (Iceberg schemas, bucket keys, derived formats): every schema change becomes a browser change and
   old tabs break.
5. **Visibility.** API requests produce structured CloudWatch timing lines. Direct S3 and Glue reads produce
   nothing today (no access logs, no data-event trail). Direct browsing would make the web app as unaudited
   as the SDK, widening the gap in [section 5](#5-the-sdk-install-data-access-and-access-logs).
6. **Hiding work and cost.** Server-side caches (asset-row, presign; unverified), Athena's parameterized fixed
   queries, and heavy dependencies (h5py, numpy) kept off the client. Direct reads download Parquet and
   metadata to every tab; fine for today's small catalog, worth watching.

**A direct-read world:**

- Reads: the browser gets identity-pool credentials and queries Iceberg, Parquet and S3 directly.
- The API shrinks to writes, compute, curation and authorization: sessions, imports, artifacts, labels, notes,
  compute.
- New costs: credentials in the tab (CSP and WAF matter more, and so does audit); CORS on the warehouse and
  Glue; DuckDB-WASM plus an Iceberg reader in the bundle (a large lazy chunk with its own budget); COOP/COEP
  for threaded WASM; a browser bound to the storage layout.

The API's value is not mainly delivering bytes (presigned URLs and direct reads both do that well). It is the
single place that enforces rules, runs the one analysis implementation, writes the catalog and leaves a
record. Direct reads trim the read half; they do not remove the layer.

**Questions**

- *If the browser could read directly, why keep the API?* Writes, compute, authorization, the contract and
  logging.
- *Why does the SDK still call the API?* For every write and every rule-checked action.
- *What would direct browser reads cost us?* Credentials in the tab, coupling to the layout, no audit trail
  and a bigger bundle.


---

## Part 6: What if

### 13. Why not Snowflake or dbt?

**Short version:** both were weighed and deferred at foundation ([ADR-0001](adr/0001-rust-tooling-clean-mvp.md),
[ADR-0003](adr/0003-iceberg-athena-catalog.md), and the platform survey).
The reason is the same for both: the SQL-shaped data is small metadata about large files. The docs record no
triggers for revisiting; the triggers below are opinion.


#### Why not Snowflake (or Databricks)

- **Wrong shape:** bulk data stays as files read by byte range; only metadata (plus spike times) is tabular.
  The survey called it "Overbuilt for metadata-sized tables over files stored in S3."
- **Cost:** the survey estimated about $100 to $300 a month for a small org, against single digits to low tens
  for Athena + Glue + S3, which cost $0 idle. Those prices were third-party; the official pages did not
  render.
- **Lock-in and duplicate storage:** Snowflake keeps a second copy in its format or reads external Iceberg;
  the platform already has Iceberg readable by DuckDB, Polars, PyIceberg and Athena, including from a laptop.
- **Mostly redundant:** Iceberg snapshots give time travel, Athena gives corpus SQL, and the SDK reads locally.

#### Why not dbt (or SQLMesh)

- **No transform pipeline to manage:** ingest writes the final tables directly, and SQL views cover the rest.
  The "transform" is Python parsing NWB and BIDS, which SQL cannot do.
- **Licensing:** dbt v2 ("Fusion") is proprietary; dbt Core 1.x stays Apache-2.0 with 1.13 as the last minor.
  SQLMesh (now Linux Foundation) was deferred with it.
- **Tooling stance:** the founding preference for modern or Rust tools and a clean MVP put dbt on the defer
  list.

#### What would make us reach for them (opinion)

**dbt or SQLMesh first** (the cheaper step), when the catalog grows SQL-shaped derived data built from other
tables:

- More derived tables across datasets. The dataset and session summaries (`dataset_summary.py`, JSON in
  `derived/summary/`) are already a hand-rolled model layer; more of them (per-subject rollups, cross-study
  participant views, QC scores) would earn lineage, tests and incremental builds.
- Tabular sources needing cleaning and joins: clinical or behavioral spreadsheets, REDCap exports,
  demographics, scored tasks, curator crosswalks.
- A real QC table: [ADR-0010](adr/0010-dynamodb-upload-sessions.md) left QC placement open (default: a
  separate append-only table), and "current QC state" from that log is a classic dbt model.
- Others writing SQL: a data team or analysts building shared models.

dbt-duckdb (or Athena adapters) runs over the existing Iceberg tables; no platform change.

**Snowflake or Databricks** only if several of these hold:

- Large tabular signals become the main workload (spike-sorted outputs across thousands of sessions,
  per-trial or per-bin features, imaging-derived measures), where TB-scale corpus SQL is routine and Athena's
  per-scan cost or latency hurts.
- Many concurrent analysts need a warehouse UX: a shared SQL workspace, BI dashboards, row-level security,
  audited cross-institution sharing.
- Compliance: identifiable or PHI-adjacent data would break the de-identified-only rule
  ([ADR-0005](adr/0005-deidentified-only-quarantine.md)) and bring audit, masking and access policies that a
  managed platform bundles (this ties to the read-audit gap in [section 5](#5-the-sdk-install-data-access-and-access-logs)).
- Partners already on one: joining a consortium's platform beats exporting.

Even then, point the warehouse at the existing Iceberg tables rather than migrate; the open format keeps that
option cheap.

**Questions**

- *Why not Snowflake or Databricks?* Wrong shape and cost for metadata-sized tables.
- *Why no dbt?* There is no SQL transform pipeline to manage yet.
- *Could we add one later without migrating?* Yes; both can sit over the existing Iceberg tables.

---

### 14. Design notes: a continuous data source (not built)

These are thinking-out-loud design notes for something that does not exist. Data rates are back-of-envelope
estimates. The scenario is always-on spikes and LFP from one implanted human. The platform assumes finished
files from de-identified public or uploaded datasets.

**Short version:** the platform is built around finished files, uploaded once and extracted once. A continuous
source breaks that in five places: unbounded per-unit spike lists, single whole-recording overviews, the
de-identified-only rule, short Lambda and API time limits, and storage cost. The ingest path mostly survives if
the stream is cut into segments.

**Scale (estimate):**

| Stream | Assumption | Per day | Per year |
|---|---|---|---|
| Broadband | 96 ch x 30 kHz x int16 | ~500 GB | ~180 TB |
| LFP | 96 ch x 1 kHz x int16 | ~17 GB | ~6 TB |
| Spikes | ~100 units x ~10 Hz | ~86M spikes, ~0.3 to 0.7 GB compressed Parquet | ~150 GB |
| Clinical device (e.g. RNS) | event-triggered snippets | MB to GB | small |

Whether broadband is kept decides most of the design.

**1. Ingestion: segments.** Use fixed-length segment files (10 to 60 min), each through session, finalize and
commit; that reuses claim-once, checksums, Object Lock and retries. 24 to 144 commits a day is trivial for the
single-writer FIFOs. The 5 GiB single-PUT cap bounds segment size (an hour of broadband is about 20 GB): build
multipart ([ADR-0016](adr/0016-presigned-uploads.md) leaves it open) or keep broadband segments short. Add a
`segments` table for a gap-free time base (subject, stream, start and end, samples, rate, gaps, clock-sync
offsets); today each file has its own clock. No Kinesis unless live views are needed; real-time monitoring is
a separate hot path.

**2. Spikes: new table layout.** `unit_spikes` (one row per unit, all spike times as a list) grows without
bound. Use a long table (`unit, t`) or per-(unit, segment) lists, partitioned by subject and day and
time-sorted. Units drift across days in chronic human recordings, so unit identity becomes a versioned result
of a sorting run (`sorting_run_id`); keep per-channel threshold crossings as a sorter-independent stream.
Precompute binned rates (per unit at 1 s and 1 min).

**3. LFP: zoom pyramid.** Today's `overview.bin` is one whole-recording spectrogram at 1 s (about 31M steps per
channel per year). Replace it with a multi-resolution pyramid (1 s, 1 min, 1 h, 1 day), built incrementally,
with coarse levels as Iceberg band-power tables and fine levels as per-segment binaries. The chunk-offset
index generalizes (one per segment, plus the segments table for time range to files). Decide between
HDF5/NWB per segment and an append-forever Zarr v3 array
([ADR-0001](adr/0001-rust-tooling-clean-mvp.md) deferred NWB to Zarr, TileDB and Lance; this is the use case
that reopens them).

**4. Catalog upkeep.** Continuous appends need compaction and snapshot expiry (possibly revisit S3 Tables for
managed compaction; that evaluation never ran). Expiry conflicts with reproducibility: results cite snapshot ids
(`SnapshotGoneError`), so tag and retain cited snapshots. Partition by subject and day (the current
partitioning was not checked).

**5. Compute and access.** The 30 s API and 15 min Lambda limits suit window queries, not "every night for six
months". Long analyses need batch (the `reextract` Step Functions map pattern, or Fargate/Batch) writing
derived tables. The SDK needs time-range access (for example `load_range(subject, start, end, streams=...)`,
lazy across segments); `load_session` assumes a bounded, in-memory file set. A month of spikes fits; a month of
LFP does not. The web app needs zoom-level navigation over the pyramid.

**6. Cost.** At full broadband, storage dominates (on the order of $4k a month after a year on S3 Standard;
estimate). Tier broadband to Glacier after spike extraction, keep spikes, LFP and pyramids hot, and set raw
Object Lock retention deliberately. Every derivation must be incremental per segment. A pyramid of derived
tables plus versioned sorting runs is exactly the SQL-shaped derived data where dbt or SQLMesh would start to
pay ([section 13](#13-why-not-snowflake-or-dbt)).

**7. Governance (likely the first blocker).** Continuous recordings from one identifiable implanted patient
almost certainly break the de-identified-only rule ([ADR-0005](adr/0005-deidentified-only-quarantine.md)):
timing, clinical context and device data make the person re-identifiable. That brings IRB and HIPAA
questions and would reopen a foundation decision. The read-audit gap
([section 5](#5-the-sdk-install-data-access-and-access-logs)) becomes a hard requirement, and access likely
needs per-subject restriction rather than every signed-in researcher reading everything.

**Decisions that shape everything else:**

1. Keep broadband, or only spikes + LFP?
2. Live viewing, or after the fact only?
3. Segment format: NWB/HDF5 or Zarr?
4. Can identifiable clinical data enter this platform at all, or does it need a separate locked-down instance?
   (Decide this first.)

