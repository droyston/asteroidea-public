# Building block view

This chapter is the map of the system. It shows the parts, what each part owns and how they connect.
It is written from the code. For commands, layout and how to add a route, function or table, read the
[developer guide](developers.md). For every route, read the [API reference](api.md).

## The system in one picture

Researchers reach the platform through two surfaces: the Python SDK and the web app. Both call one
HTTP API. The API runs on Lambda functions that read and write the catalog, the buckets and the tables.
Files enter through the upload path and are described in the catalog by a chain of queue-driven
functions.

```text
  Researcher
     |
     +--> Web app (React, served by CloudFront from a bucket) ---+
     |                                                           |
     +--> SDK (Python package `asteroidea`) ---------------------+
              |                                                  v
              |  direct reads with temporary          HTTP API (Cognito JWT authorizer)
              |  credentials (DuckDB, ranged reads)     6 API functions, 36 routes
              |                                                  |
              v                                                  v
        Catalog: 14 Iceberg tables on Glue <------ commit and extract functions <---- queues
        raw, derived and artifacts buckets                       ^
                                                                 |
                                       Curator: `make import`, `POST /imports`
```


## Two stacks

The CDK app builds two top-level stacks (`infra/src/asteroidea_infra/app.py`).

**DataStack** holds what must outlive a redeploy. It has termination protection on. It owns:

- six buckets: the site bucket (with the CloudFront distribution, in the `WebHosting` construct), the
  raw bucket, the artifacts bucket, the derived bucket, the warehouse bucket and the Athena results
  bucket;
- five DynamoDB tables: upload sessions, dataset owners, labels, import runs and app-dev notes;
- the Glue database `asteroidea` and the Athena workgroup;
- the `Identity` construct: the Cognito user pool (kept on delete), its app clients, the identity pool
  and the two user groups, `researchers` and `curators`.

DataStack exports the identity values under `asteroidea-<name>`.

**ServicesStack** holds the code that can be torn down and rebuilt. It imports those exports and builds
two nested stacks:

- **Ingest** creates the commit queue, the extract queue and the LFP build queue (each with a
  dead-letter queue), then the six ingest functions, then the `asteroidea-reextract` state machine.
- **Api** creates the HTTP API with its JWT authorizer, then the six API functions and one API Gateway
  route for each row of the route table.

There is no VPC and no third stack. The rule for when a nested stack must be split is in the
[developer guide](developers.md#when-to-split-a-nested-stack).


## The function registry

`infra/src/asteroidea_infra/functions.py` lists every Lambda function as one `FunctionSpec` row. A row
names the construct id, the handler, the fixed role name, memory, timeout, the environment, the role's
IAM statements and, when needed, the container image and the queue that feeds it. One builder loops
over the rows, so a function is added by adding a row.

The registry holds 12 functions: 6 in `API_FUNCTIONS` and 6 in `INGEST_FUNCTIONS` (counted with
`len(functions.FUNCTIONS)`, `len(functions.API_FUNCTIONS)` and `len(functions.INGEST_FUNCTIONS)`).

| Function | Stack | Serves or does |
|---|---|---|
| `SessionFunction` | Api | Upload sessions: create, finalize, read, list (`uploads_api.py`) |
| `PlatformFunction` | Api | The get-started values, the shared results and the app-dev notes (`get_started_api.py`, `artifacts.py`, `notes_api.py`) |
| `CatalogFunction` | Api | Read-only catalog routes and the file download presign (`catalog_api.py`) |
| `ComputeFunction` | Api | Rates, LFP overview, spectrogram, alignment events, tuning (`compute_api.py`, own image) |
| `LabelsFunction` | Api | Shared labels on files (`labels_api.py`) |
| `ImportsFunction` | Api | Curator import runs (`imports_api.py`) |
| `CommitFunction` | Ingest | Verifies an upload, copies it to `raw/`, appends the `assets` row (`commit.py`; fed by the commit queue) |
| `ImportFunction` | Ingest | Fetches one public archive file and runs the commit path (`import_file.py`) |
| `ImportPlanFunction` | Ingest | Plans an import run and starts one `ImportFunction` per file (`import_plan.py`) |
| `ExtractFunction` | Ingest | Reads a committed file and writes the catalog's detail tables (`extract/handler.py`; fed by the extract queue) |
| `LfpBuildFunction` | Ingest | Builds the LFP index and overview, and the dataset and session summaries, in the derived bucket (`lfp_build.py`, `dataset_summary.py`; fed by the LFP build queue) |
| `DeriveFunction` | Ingest | Writes row bundles for a re-extract (`extract/derive.py`) |

`PlatformFunction` serves the shared results: save, list, read, edit, withdraw and delete. Each result is
a provenance record plus its file under `artifacts/`. Small objects under `index/` let a list by id, by
input or by time avoid scanning every record.

Each function has its own IAM role with a fixed name. Three container images exist: the commit image
(`ingest/Dockerfile`, which also serves the import functions), the extract image
(`ingest/Dockerfile.extract`, which also serves `DeriveFunction`) and the compute image
(`ingest/Dockerfile.compute`, which also serves `LfpBuildFunction`). The commit image carries no
pynwb, h5py or remfile; a test pins that. The five API zip functions run from `ingest/src`.


## The route table and the shared handler

`ingest/src/asteroidea_ingest/routes.py` lists every HTTP route as one `Route` row: method, path, the
function that serves it, the Cognito group, and a one-line summary. The table holds 36 routes: 33 need
the group `researchers` and 3 need `curators`. By function: Session 4, Platform 9, Catalog 10, Compute 6,
Labels 4, Imports 3. The infrastructure builds one API Gateway
route per row, and the [API reference](api.md) is generated from the same rows.

`api_common.py` is the one handler kit every API function shares. It uses only the standard library, so
a zip function can import it. Each handler module exports a `DISPATCH` map from a route key to a method
name. The module's `handle` calls `serve`, which does these steps in order:

1. find the route row (otherwise 404);
2. check the caller's group from the token claims (otherwise 403);
3. call the method with a `Request` and a `Timing`;
4. turn an `ApiError` into its response, and log one timing line as JSON.

Any other exception is logged as one `unhandled` line and answered with a 500 that names the request id.

Every refusal has the same body, `{"error": code, "detail": sentence}`. The `error` code is short and
stable, and a client branches on it. The `detail` is a sentence for a person. `api_common.CODES` maps
ten statuses to codes: 400 `invalid_request`, 403 `forbidden`, 404 `not_found`, 409 `conflict`, 410
`gone`, 413 `too_large`, 422 `unprocessable`, 500 `internal_error`, 502 `upstream_failed` and 503
`unavailable`. A refusal can carry a more specific code. The full list per route is in the
[API reference](api.md#error-body).


## The catalog

The catalog is 14 Iceberg v2 tables in the Glue database `asteroidea`. They are defined in
`infra/src/asteroidea_infra/schema.py` (`len(schema.TABLE_NAMES)`). `make catalog-init`
creates the missing tables and adds missing optional columns. It never drops or retypes a column and
refuses a type conflict.

| Table | Holds |
|---|---|
| `assets` | One row per committed file: checksum, size, S3 key and version, dataset, BIDS entities, provenance |
| `datasets` | One row per dataset: title, description, licence, source archive and version, tier (curated, upload or sandbox) |
| `subjects`, `subject_links` | Subjects per dataset; the curated crosswalk to a shared subject key, with its status, who linked it, when, and on what basis |
| `studies`, `study_datasets` | Curated studies and the datasets each one groups (a dataset belongs to at most one study) |
| `sessions` | Recording sessions inside a dataset (not the DynamoDB upload sessions) |
| `asset_headers` | Extracted header fields per file |
| `series`, `electrodes`, `units` | Small NWB tables: signal series, electrode sites, unit summaries |
| `unit_spikes`, `events`, `intervals` | Spike counts and times per unit; event rows (onset, duration, trial type); time intervals |

Large time series stay in the files and are read lazily. Writers go through `CatalogWriter` (PyIceberg
through Glue). `CatalogWriter.replace` swaps one file's rows in a table in one commit per table. Readers
use Athena (the API functions), DuckDB or Polars (the SDK, with temporary credentials).


## From upload to catalog

Three queues carry the work. The commit and extract queues are FIFO queues with one message group each,
so one writer touches the catalog at a time.

```text
 SDK --POST /sessions--> SessionFunction --> sessions table (OPEN), presigned PUT
 SDK --PUT-------------> raw bucket  landing/<session>/<path>
 SDK --finalize--------> SessionFunction --> commit queue
                                                 |
 CommitFunction: verify checksum, copy to raw/<dataset>/<path>, set Object Lock
                 retention, append the `assets` row --> extract queue
                                                 |
 ExtractFunction: read the file (pynwb or NIfTI header, ranged reads),
                  write asset_headers, series, electrodes, units, unit_spikes,
                  events, intervals --> LFP build queue (NWB files, and one
                  summary message for the file's dataset)
                                                 |
 LfpBuildFunction: write lfp/<asset>/<version>/ index and overview in the derived bucket;
                   for a summary message, read the catalog through Athena, write
                   summary/<dataset>/_dataset.json, and send one session message per
                   session to its own queue, which writes that session's summary
```

`ImportFunction` joins the same path. A curator starts it (`make import` or `POST /imports`, which runs
`ImportPlanFunction` first). It copies or streams one public file into `landing/`, checks the published
checksum and sends a commit message. The raw bucket denies writes from every role but the session,
commit and import roles.

### Re-extract

A re-extract re-runs the extractors over files already in the catalog. `make reextract` writes one
tracking item per asset and starts one run of the `asteroidea-reextract` Step Functions state machine.
Its Map state runs `DeriveFunction` for each asset, at most 4 at a time. `DeriveFunction` writes row
bundles to `rows/<asset>/<run>/` in the derived bucket and sends one pointer message per asset to the
extract queue. `ExtractFunction` then replaces that asset's tables. A failure on one asset marks that
item failed and the Map goes on. The command line never invokes a function or sends to a queue.


## Buckets

| Bucket | Holds | Writers |
|---|---|---|
| raw | `landing/` (uploads in flight) and `raw/` (verified files, Object Lock) | Session, Import and Commit roles |
| artifacts | `artifacts/` (a record and its file per result) and `index/` (lists by id, input and time) | `PlatformFunction`, the presigned PUT of the file, `make artifacts-backfill` |
| derived | `lfp/` (index and overview), `summary/` (dataset and session summaries) and `rows/` (re-extract bundles, expire after 7 days) | `LfpBuildFunction` for `lfp/` and `summary/`, `DeriveFunction` for `rows/`; no researcher write |
| warehouse | Iceberg data and metadata | Commit and extract writers |
| Athena results | Query output | Athena |
| site | The built web app, the SDK wheel and the handout notebook | `make web-publish` |

The derived bucket keeps two writers. Its policy denies every write outside `lfp/`, `summary/` and
`rows/`, and inside each prefix every role but its writer. A summary write is conditional on the
object read just before it, so a newer summary is never replaced by an older one. The summaries are
rewritten after every import and re-extract. `make summaries` queues a rebuild of every dataset's summary
(`DATASET=<name>` for one) and is the one-time backfill for datasets imported before the summaries
existed.


## The SDK

`sdk/` is the Python package `asteroidea`. Its public names are listed in `asteroidea.__all__`: sign-in
(`sign_in`), upload (`upload`, `upload_dataset`), catalog reads (`datasets`, `subjects`, `sessions`,
`assets`, `query`, `sql`), file reads (`open_file`, `open_image`, `open_nwb`), sessions (`load_session`,
`sessions_index`, and the `Session` class with `rates`, `tuning`, `band_power` and `lfp`), runs and results
(`run`, `save_artifact`, `artifacts`, `open_artifact`, `download_artifact`, `withdraw_artifact`),
`labels` and a family of exceptions that all derive from `AsteroideaError`. Private modules begin with an
underscore. `asteroidea.analysis` (extra `analysis`) is the one implementation of rates, z-score,
spectrogram, band power, event recipes and tuning; the compute image copies the same files, so the
browser and the SDK show the same numbers. Two data files sit beside it: `analysis_defaults.json` (the
analysis settings per dataset) and `recipes/<dataset>.json` (which events a dataset aligns to).


## The web app

`web/` is a React, Vite and TypeScript single-page app outside the uv workspace. It has one folder per
feature under `web/src`: `router`, `kit` (shared components and tokens), `research` (dataset, subject and
file pages), `units`, `lfp`, `events`, `mri`, `plots`, `imports`, `auth`, `catalog`, `start`,
`discover` (the public-archive browser, with `index`, `sources`, `table`, `targets` and `detail` as its
parts) and `results` (the list of shared results and the result page). The navigation has four
entries: Data (datasets, subjects and files from the catalog), Discover (public archives, and the
curator's Imports), My results and Get started. The browser draws what the API returns and does no analysis
arithmetic. The image viewer (NiiVue) loads in
its own lazy chunk. Sign-in uses the Cognito hosted login with PKCE; tokens stay in `sessionStorage`.


## Gates

`make check` runs the offline gates: lock file, format, lint, types, tests, the docs build, help text,
the web gates and the suppression gate. `make check-full` adds the synthesis and the `synth`-marked
tests (cdk-nag and stack assertions). The gate list and the commands are in the
[developer guide](developers.md#the-gates).



## History

The first design planned a platform base stack (VPC and endpoints), a validator, a quarantine mover and a
separate DICOM path; none of these is built. The design started with one table and one commit function
and grew to 12 tables and 12 functions over several stages. The decision records are in the
[decisions chapter](09-decisions.md).

