# Crosscutting concepts

Rules and conventions that cut across the building blocks. Each section says what holds today. What is
planned or not built is marked as such.

## SDK design principles

The SDK is the researcher's main way into the platform, and "productive within the first hour" is a
top quality goal. Four rules shape it.

| # | Rule | How it is tested |
|---|---|---|
| P1 | **One-call sign-in.** A researcher signs in with one call and no arguments. The SDK finds or obtains tokens and AWS credentials itself ([ADR-0009](adr/0009-sdk-copy-paste-login.md)). | A test calls sign-in with no arguments against a stub identity provider and checks that the next data call works. A second test with a valid token cache checks that no login URL is printed. |
| P2 | **Defaults for region, catalog and credentials.** Region, catalog name, Athena workgroup and credential source come from the platform's own configuration, not from the researcher's environment. | A test runs every public function with only its required arguments in an environment with no `AWS_*` variables and no `~/.aws` directory. |
| P3 | **One exception hierarchy whose messages say what to do next.** Every SDK error derives from `AsteroideaError`. Each message names what went wrong and the next step. | `sdk/tests/test_errors.py` walks every exception class and checks the base class and that the message names a next step. |
| P4 | **No AWS jargon in researcher-facing errors.** Messages never contain AWS service names, error codes or ARNs. The original exception stays attached as the cause. | The same test checks every message against a denylist (AWS, Cognito, STS, ARN, IAM, boto, `arn:aws`, `AccessDenied`, `ExpiredToken`). |

State: P3 and P4 are tested as written. `sign_in()` takes no required argument and prints no
login URL when a valid cache exists, but it still needs a `platform.json` (the `asteroidea-setup` command
writes it), so P2 is only partly met; a missing file raises `ConfigMissingError`. The P1 and P2 tests as
written above are not in the suite.


## Identity and authorization

- **Researchers** sign in to a Cognito user pool ([ADR-0004](adr/0004-cognito-researchers-identity-center-admins.md)).
  The web app uses authorization code with PKCE. The SDK prints a copy-paste login URL
  ([ADR-0009](adr/0009-sdk-copy-paste-login.md)). Self sign-up is off: an administrator runs
  `make researcher EMAIL=<address>`, and Cognito emails a temporary password (`RESEND=1` sends it again).
- **Cognito groups map to IAM roles.** The identity pool maps the group `researchers` to ResearcherRole
  with token mapping and denies ambiguous roles. The default authenticated role has no policies, so a user
  in no group gets no credentials. ResearcherRole may read the raw bucket and Iceberg storage and use the
  Athena workgroup, and nothing else: no write to raw, no access to a quarantine bucket (not built)
  ([ADR-0017](adr/0017-identity-pool-data-access.md)). ResearcherRole's writes to the raw and landing buckets are denied (PutObject, DeleteObject,
  DeleteObjectVersion, CopyObject into raw), and `tests/live/test_raw_bucket_deny.py` checks this against the deployed policy.
- **Curators** are a second group, `curators`, added with `make curator EMAIL=<address>`. A curator is also
  a researcher. The group gates the import routes ([ADR-0004](adr/0004-cognito-researchers-identity-center-admins.md)).
- **The API** checks a Cognito access token in a JWT authorizer whose audience is the `sdk`, `web` and (when
  deployed) `sandbox-test` clients. Then the handler checks the route's group (403 otherwise) and, for
  session and artifact routes, that the caller owns the item. The kit that does this is
  `ingest/src/asteroidea_ingest/api_common.py`. The route list is in the [API reference](api.md).
- **Identity Center is for administrators only.** Administrators reach AWS through permission sets that only
  Identity Center users can assume. Researchers never get an Identity Center account.


## Error handling

Every refusal that platform code produces has the same JSON body:

```json
{"error": "<code>", "detail": "<sentence>"}
```

`error` is a short stable code that the SDK and the web app branch on. `detail` is one sentence for a
person. A handler raises `ApiError(status, detail, code=...)`. The code defaults from the status
(400 `invalid_request`, 403 `forbidden`, 404 `not_found`, 409 `conflict`, 410 `gone`, 413 `too_large`,
422 `unprocessable`, 500 `internal_error`, 502 `upstream_failed`, 503 `unavailable`). A refusal that a client
must tell apart from its status default takes an explicit code. Old SDK wheels are not kept compatible.

An exception that is not an `ApiError` is a crash. The shared `serve` function logs one JSON line with
`unhandled` (the exception class), the request id, the route and the traceback. It then answers 500 with
code `internal_error` and the detail "The platform failed on this request. Report request id `<id>`." so a
researcher can quote the id and an administrator can find the line.

API Gateway answers a few cases itself, with the body `{"message": "<text>"}`: 401 from the authorizer, 404
for an unrouted path, 500 when a function fails to start, and 503 after the 30-second integration limit.
Clients branch on the status for these.

**The `Errors` metric no longer counts handler crashes.** The function now returns the 500 instead of
raising, so Lambda sees a success. The `unhandled` log line is where crashes are counted. No alarm reads
either today.

The SDK wraps each platform error in an exception of its own hierarchy (rules P3 and P4 above). Details of
each route's codes are in the [API reference](api.md).


## Data model

The model copies DANDI's separation of blob, asset and version.

| Concept | What it is | Mutable? |
|---|---|---|
| **Blob** | The stored bytes of one file: S3 key, S3 version id, size, CRC64NVME checksum, upload session id | Never |
| **Asset** | A blob in context: BIDS path and entities (subject, session, datatype, suffix), modality, extracted header fields, extractor and library versions | Never after finalize |
| **Version** | A named, immutable state of a dataset: a set of assets fixed by an Iceberg snapshot id | Never once published |
| **Platform-owned columns** | Facts the files do not carry, such as age at scan and QC state. A NIfTI header has no subject or age field, and BIDS has no standard QC field | QC changes over time |

Because finalized asset rows never change, QC would be written as rows of a separate append-only table (the
latest row wins) rather than as updates. That is the direction; the table is not built.

Datasets are named `<archive>-<id>`, for example `dandi-000623` and `openneuro-ds004798`. One version per
dataset name is a deliberate limit.


## Reproducibility

- **Query results cite an Iceberg snapshot id.** Every SDK and Athena query is pinned to a snapshot id, and
  the result carries it. Re-running the query with that id returns the same rows, because Iceberg keeps old
  snapshots. In a check, a query pinned to the first snapshot returned the same 3 rows after a
  second upload made the current table 4 rows.
- **Artifacts cite their sources.** An artifact's provenance record lists source asset ids, S3 version ids
  and the snapshot id ([Runtime view](06-runtime.md#f-the-sdk-writes-an-analysis-artifact-with-provenance)).
- **Retention.** Snapshots that a published version or an artifact record refers to must survive when old
  snapshots are expired. Raw object versions are never deleted by the platform. No snapshot expiry runs
  today, so nothing is lost yet.
- **Re-extraction is not atomic across tables.** `make reextract` replaces an asset's tables one commit each.
  A reader can see new units beside old intervals until the run ends. A failure names the tables already
  replaced ([ADR-0003](adr/0003-iceberg-athena-catalog.md)).


## Integrity and immutability

- S3 computes a CRC64NVME full-object checksum at upload. The platform stores it with the S3 version id in
  the blob record, and the commit function compares it with the client's value from the manifest.
- A multipart SHA-256 in S3 is a checksum of part checksums, not of the file. If a whole-file SHA-256 is ever
  needed, the platform computes it separately. Multipart upload is not built.
- **Verify before visibility.** A presigned PUT lands in `landing/`, which researchers cannot read. The
  commit function compares the stored checksum with the client's value there and copies to `raw/` only a
  verified version. A mismatch leaves the session INCOMPLETE with nothing in `raw/` and no catalog row,
  as the first-hour run showed.
- **The raw bucket is locked down in three layers**: versioning, a bucket policy that allows writes and
  deletes only to the ingest roles, and Object Lock in governance mode after the verified copy. See the
  [Deployment view](07-deployment.md#datastack-termination-protected).
- **Public static files.** The app shell and the public index are reachable without sign-in. They hold
  public identifiers and public archive metadata only ([ADR-0007](adr/0007-s3-cloudfront-spa.md)).


## De-identification and quarantine

The platform accepts **de-identified data only** ([ADR-0005](adr/0005-deidentified-only-quarantine.md)).
The checks below are the design. None is built: the platform imports only public, de-identified archive
data, and there is no validator and no quarantine bucket.

1. **SDK pre-check.** Before upload, the SDK would read headers and refuse files with obvious identifiers,
   for example DICOM files without the Patient Identity Removed flag (0012,0062).
2. **Server-side validation.** A validator would repeat and extend the checks after upload.
3. **Quarantine.** A file that fails validation would move to a bucket only administrators can read, with a
   7-day expiry.

**The exposure, stated plainly.** Server-side checks run after the bytes are in AWS. A file with identifiable
data that passes the pre-check, or is uploaded without the SDK, would sit in the landing area, and in
quarantine if caught, for up to 7 days, readable by administrators. Burned-in identifiers in pixel data need
DICOM's "Clean Pixel Data" option, and header checks do not catch them. If such data passed every check and
reached raw, Object Lock governance mode would still let a privileged administrator remove it; compliance
mode would not. A removal needs a temporary role and a runbook.


## Format conversion (open)

BIDS and NWB are the stored formats. Vendor raw ephys formats are ingest-only
([ADR-0006](adr/0006-bids-nwb-native.md)). **Who converts** vendor raw data to NWB, and DICOM to BIDS
NIfTI, is open: the researcher before upload, or the platform after it. Multi-gigabyte conversions are
unlikely to fit a Lambda's 900-second timeout, which bears on the choice.

## Observability

- **One timing line per request.** Every API request writes one JSON line on stdout, built in
  `ingest/src/asteroidea_ingest/timing_log.py`: `timing` (the route), `phases` (whole milliseconds, such as
  `athena_ms`, `glue_ms`, `s3_ms`), `cold` (true for the first request of an execution environment), and
  `status` and `request_id`, plus `asset_id` and `cache` when the route has them. A line never carries a
  token, a user id or a URL, because a presigned URL is a credential. In CloudWatch Logs Insights, select the
  lines with `filter ispresent(timing)`. `make live-route-timing` reads them back.
- **One line per crash.** The `unhandled` line described under Error handling.
- **Session state.** The session id is in every session item and catalog row. The states in the sessions
  table are OPEN, FINALIZING, ACCEPTED, COMMITTED and INCOMPLETE. There is no QUARANTINED state and no
  administrator view. `GET /uploads` shows a researcher their own sessions.
- **Not built:** alarms, dashboards and a backup and restore drill.


## Tagging and cost

- The CDK app applies the tags `project=asteroidea` and `managed-by=cdk` to every resource
  (`infra/src/asteroidea_infra/config.py`). A `stack` tag and an `environment` tag are not
  applied.
- A budget alarm sits in the management account: `asteroidea-sandbox-monthly`, alerts at $3, $8 and $10
  actual and $10 forecast, and an automatic stop at $10. No budget is defined in a stack.
- Idle cost is in the [Deployment view](07-deployment.md#idle-cost).


## Naming

- Stacks: `DataStack` and `ServicesStack`, with nested stacks `Ingest` and `Api`. A `BaseStack` for a VPC is
  not built.
- Python distributions: `asteroidea` (the SDK), `asteroidea-infra`, `asteroidea-services` and
  `asteroidea-ingest`.
- Table and column names are lowercase `snake_case`, because S3 Tables requires lowercase names for Athena
  to see them.
- S3 keys under `raw/` follow the BIDS layout, for example `sub-*/ses-*/anat/*_T1w.nii.gz`.

## Policy as code (cdk-nag)

cdk-nag 3.0.2 registers rule packs with `Validations.of(app).add_plugins(...)`. Its README shows TypeScript
only, so a test proves that Python works. `infra/src/asteroidea_infra/nag.py` calls
`Validations.of(app).add_plugins(AwsSolutionsChecks(app))`. A test bucket with all four
BlockPublicAccess flags false fails with AwsSolutions-S2. An acknowledgement takes
`aws_cdk.Acknowledgment(id="AwsSolutions::AwsSolutions-S1", reason=...)` and always carries a reason. The
check runs in `make check-full`, not in `make check`.


## Lambda packaging

The API functions are zips of `ingest/src`. The commit, extract and compute functions are arm64 container
images, because PyIceberg, pynwb and the analysis libraries do not fit a zip. A test pins which standard
library and `asteroidea_ingest` modules a zip function may import
(`infra/tests/test_api_bundle.py`). The commit image must carry no harvester library, and a test pins that.
The sizes are in the [Deployment view](07-deployment.md#servicesstack-nested-stacks).

PyIceberg is imported at module level in the commit function. The median time from finalize to a queryable
row was 8.5 s with a module-level import and 9.1 s with a lazy import. The first
start after an image push can hit Lambda's 10-second init limit; that happened once on the commit image
(9,999.77 ms) and twice in three first starts on the extract image. It is an accepted cost.


## Tooling escape-hatch policy

The strict settings (ruff `ALL`, ty with every rule an error, strict pytest) relax only in these ways:

- Test directories may ignore `S101`, `S105`, `S106`, `ANN`, `D1`, `PLR2004` and `INP001`.
- `infra/src/asteroidea_infra/runner.py` may ignore `S603` and `S607`, because it runs argument lists with
  `shell=False` and a timeout. `sdk/src/asteroidea/_setup.py` may ignore `S603` only, for the same reason.
- Third-party warnings are filtered per module in pytest's `filterwarnings`, each with its reason.
- ty overrides are scoped to a path, each with its reason. Three paths have one: `catalog_writer.py`
  (PyIceberg and pyarrow types), and `api.py` and `identity.py` (an aws-cdk-lib protocol mismatch).
- Anything wider needs a maintainer decision.

**The suppression gate.** `make gate-suppressions`, part of `make check`, fails on any `noqa`,
`ty: ignore`, `type: ignore`, `biome-ignore`, `@ts-ignore` or `@ts-expect-error` that
`scripts/suppression-allowlist.txt` does not list. The allowlist is empty (it has no
non-comment line). A stale allowlist line fails the gate too.

The reason for a written policy: scientific libraries (pynwb, h5py, neo) arrive with warnings and missing
type information, and without a policy each would be a separate decision.


## Provenance, subject identity and licence

- **Provenance.** `assets` carries optional provenance columns, including `upload_id`, `ingest_method`,
  `format`, `checksum_sha256`, `source_archive`, `source_dataset`, `source_version`, `source_path`,
  `source_url`, `source_digest`, `license` and `ingested_by` (`infra/src/asteroidea_infra/schema.py`).
- **Subject identity.** No code derives a subject key from a naming rule. The curated table `subject_links`
  maps (dataset, subject) to a `subject_key`. `make crosswalk` loads it from a reviewed CSV in
  `curation/subject-links/`. An unlinked subject appears in the SDK as `<dataset>/<subject>`. The CSV for the
  two exemplar datasets is not yet reviewed, and the `participants.tsv` of ds004798 labels every
  participant "control".
- **Licence.** `datasets.license` and `assets.license` carry the licence (CC-BY-4.0 for 000623, CC0 for
  ds004798). The SDK `datasets()` view shows it, which meets the attribution term.
- **Placeholder session time.** NWB files in the exemplar set carry a placeholder `session_start_time`
  (`2019-04-01`). Do not read it as a recording date.


## Ownership and author access

- **Dataset ownership.** The first researcher to commit a file into a dataset owns it, recorded in the
  dataset-owners table. `POST /sessions` compares the caller with the owner and refuses another researcher
  with a 403 whose code lets the SDK tell an ownership refusal from a missing group. Imports use the owner
  `import:<caller>`.
- **Artifacts are readable by every researcher; only the author changes them.** Every signed-in researcher can list, read and download every finished
  artifact and its provenance record. Only the author edits the name or note, withdraws it, or deletes it; a
  non-author gets 403 `not_author`. A result that another result used cannot be deleted (409
  `has_dependents`), so a researcher withdraws it instead, and a withdrawn result stays readable. An
  unfinished record reads as absent (404) to everyone but its author. The record names its sources as
  `FileRef`s ([ADR-0018](adr/0018-artifact-lake-boundary.md), research-synthesis update).
- **Author email is shown to every researcher.** A result's record and its listings carry the author's email
  address, read from the `email` claim of the ID token that the SDK sends on create. Records
  made before the email was recorded carry `null` until `make artifacts-backfill` fills them from Cognito. The consequence is
  in [Risks](11-risks.md#data-and-access).
- **Shared labels.** Researcher labels on a file are the one artifact-side data every researcher can read.
  Only the author edits a label, and an asset takes at most 2,000. See the
  [ADR-0018](adr/0018-artifact-lake-boundary.md), labels update.
- **Shared code.** A pattern with two or more copies is extracted to a shared module, or kept with a stated
  reason.


## Research frontend conventions

- **One implementation of an analysis.** A function that the SDK and the web app both need lives in
  `asteroidea.analysis`. The compute image copies its files, and a test compares their sha256 with the
  source ([ADR-0020](adr/0020-research-frontend-analysis.md)).
- **The browser draws; it does not compute.** The Units and LFP plots draw the arrays the API returns. A test
  fails if the web rebins.
- **Athena at the boundary.** Every string parameter is a quoted varchar literal
  (`athena_query.render_param`). Array cells are parsed into lists. Doubles stay doubles. One `AthenaRunner`
  serves every Lambda.
- **A fake enforces the service's rule.** `ingest/tests/ddb_rules.py` raises DynamoDB's own
  ValidationException for an unused expression name or value. `infra/tests/test_api_bundle.py` imports each
  zip handler with only the standard library, the runtime's boto3 and `asteroidea_ingest`. Both exist
  because a live deploy found what the earlier fakes allowed. Athena fakes return every cell as text.
- **Live checks report every row.** `make live-research-app` and `make live-web-smoke` continue past a FAIL
  and mark a dependent step "not reached". `make live-first-hour`, `make live-hosted-setup` and
  `make live-analysis` still stop at the first FAIL.
- **Publish guards.** `web-publish` refuses a stale `index.json` schema. A browser tab opened before a
  publish shows a reload notice when a chunk is gone.


## Research synthesis conventions

- **Events come from one recipe and one resolver.** A recipe file per dataset
  (`sdk/src/asteroidea/recipes/<dataset>.json`) names the event types and where their rows live;
  `analysis.resolve_events` turns it into typed events for the web, the SDK and the compute Lambda. The compute
  image and the SDK wheel carry the same files, and a test compares them by sha256 ([ADR-0021](adr/0021-research-synthesis.md)).
- **Analysis defaults are versioned and named.** `analysis_defaults.json` holds the bins, windows, bands,
  wavelet cycles, baseline and test settings, with a block per dataset where a paper gives values and the
  convention otherwise. Every result row and every page names the defaults version, the recipe version and the
  baseline used. Pre-window baselines are the convention; 000623 uses raw rates.
- **Every statistic is an exploratory screen.** The label "exploratory screen" travels with every p, q and
  area result: in the route bodies (`label`), in the SDK frames, in the pages and in the notebook. A
  q at most alpha means "worth a look", not a finding. The movie onset is one trial, so it is a labelled
  single-trial trace and is never averaged.
- **A run records what it read.** Inside `asteroidea.run(...)` the SDK records every table snapshot, file
  version, query, result opened and the defaults and recipe versions used, and saves them in version 2 of the
  record. The index objects `index/by-id`, `by-input` and `by-time` in the artifacts bucket are written by the
  PlatformFunction only.
- **The browser still draws.** The session page shows the server's `rate_hz`, `p`, `q`, block and baseline and
  recomputes nothing. The bundle gate (`web/bundle-budget.json`) fails when the main bundle or a lazy chunk grows past its
  cap.

