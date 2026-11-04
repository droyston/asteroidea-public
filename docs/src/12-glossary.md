# Glossary

This is the word list for the book. Each term has one meaning. Terms are in alphabetical order.

- **ADR (Architecture Decision Record)**: One decision with its context, the options and the
  consequences. The records are in `docs/src/adr/`. A record has a status: Accepted, Provisional (the
  direction is chosen but a measurement may change it) or Proposed.

- **`api_common`**: The Python module `ingest/src/asteroidea_ingest/api_common.py`, the shared kit of
  the API handlers. It reads the caller from the token claims, finds the route-table row (404 if none),
  checks the row's group (403), runs the handler method, writes one timing line and turns an
  `ApiError` into the standard error body. It imports only the standard library, so zip Lambdas can use it.

- **Artifact**: A result file a researcher saves with `save_artifact`, stored with a record of its
  inputs. Artifacts live in the artifacts bucket. Every researcher can read them; only the author edits or withdraws one.

- **Asset**: One finalized file in the catalog: a row of the `assets` table with its checksum, size,
  S3 key, version id and path-derived BIDS fields.

- **Athena**: Amazon's serverless SQL service. It reads the catalog tables without a cluster and has no
  idle cost. The catalog, compute and labels functions use it. It bills per terabyte scanned.

- **BIDS (Brain Imaging Data Structure)**: A community standard for organising neuroimaging data as
  files and folders with JSON sidecars. A T1-weighted scan sits at `sub-*/ses-*/anat/*_T1w.nii.gz`.
  The platform's native layout for MRI.

- **Catalog**: The fourteen Iceberg tables in the Glue database `asteroidea`: `assets`, `datasets`,
  `subjects`, `subject_links`, `sessions`, `asset_headers`, `series`, `electrodes`, `units`,
  `unit_spikes`, `intervals`, `events`, `studies` and `study_datasets`. `make catalog-init` creates
  and extends them.

- **CRC64NVME**: A 64-bit checksum and the S3 default for new objects. The commit function checks it
  before it copies a file into `raw/`. S3 computes it as a full-object checksum over a multipart upload.

- **Curator**: A researcher in the Cognito group `curators`. Only a curator can start a dataset import.

- **DANDI**: A public archive for neurophysiology data in NWB. Its separation of blob, asset and
  collection is the model for the catalog. The platform can import files from it.

- **De-identification**: Removing or replacing information that identifies a person, such as names,
  dates and text burned into image pixels. The platform accepts only de-identified data. The DICOM standard defines profiles for it in PS3.15 Annex E.

- **Derived store**: The bucket for data the platform computes: `lfp/` (LFP indexes and overviews) and
  `rows/` (re-extract bundles, which expire after 7 days). Only two named roles write to it, and
  researchers cannot.

- **Error code**: The short, stable word in the `error` field of every API refusal, for example
  `invalid_request`, `forbidden`, `not_found`, `conflict`, `gone`, `too_large`, `unprocessable`,
  `internal_error`, `upstream_failed` and `unavailable`. A client branches on the code. The `detail`
  field holds a sentence for a person, and its text can change. A handler may set a more specific code
  than the status default.

- **Expected template diff**: The file `infra/template-diff.expected`. When a change is meant to alter
  the synthesized CloudFormation templates, the file lists the exact differences `make gate-templates`
  prints, one per line. The gate passes only if its output equals the file.

- **Extract**: Reading a finalized file and writing facts about it to the catalog. The extract
  function reads NWB (through pynwb) and NIfTI headers and fills `asset_headers`, `series`,
  `electrodes`, `units`, `unit_spikes`, `intervals` and `events`.

- **Finalize**: The explicit last step of an upload session. It queues the commit, which verifies the
  checksum, copies the file into `raw/` and appends the catalog row.

- **Function registry**: The list of Lambda functions in `infra/src/asteroidea_infra/functions.py`.
  Each function is one `FunctionSpec` row: construct id, handler, role name, memory, timeout,
  environment, IAM statements, and its image or zip. The stack code loops over the rows. A deployed
  function never changes its construct id.

- **Glue (AWS Glue Data Catalog)**: AWS's metadata catalog. It holds the Iceberg tables that Athena
  and the SDK read. It can manage Iceberg tables on ordinary S3 buckets.

- **Iceberg (Apache Iceberg)**: An open table format. A table is data files plus metadata that records
  each change as a snapshot, so a query can be re-run against an earlier snapshot.

- **Identity pool (Amazon Cognito identity pool)**: A Cognito feature that swaps a login for
  short-lived AWS credentials of an IAM role. Here it turns a researcher's user-pool login into
  credentials scoped by group.

- **Landing**: The `landing/` prefix of the raw bucket, where a presigned upload arrives. Objects there
  expire after 3 days. The commit function copies a verified file to `raw/`.

- **Lease**: A time limit on a claimed session. When a lease has passed, `make reclaim` may re-send
  the session to its queue.


- **NAT (NAT gateway)**: A managed gateway that lets resources in private subnets reach the internet. It bills by the
  hour and by the gigabyte, about $33 a month each before data. The platform has no VPC, so it has none.

- **NestedStack**: A CloudFormation stack deployed as a resource of a parent stack. It cannot be listed
  or deployed alone. `ServicesStack` has two: `Ingest` and `Api`. CDK synthesizes it to its own template.

- **NWB (Neurodata Without Borders)**: A standard format for neurophysiology data, usually one HDF5
  file per recording session. The platform's native format for electrophysiology. NWB files range from 0.1 to more than 100 GB.

- **OAC (origin access control)**: The CloudFront setting that lets only one distribution read a
  private S3 bucket. The web app is served this way. AWS recommends it over the legacy origin access identity.

- **Object Lock**: An S3 feature that stops an object version from being deleted or overwritten for a
  retention period. The raw bucket has it on, and the commit function sets a governance retention on
  each verified object. No role may bypass it.

- **PKCE (Proof Key for Code Exchange)**: An extension of the OAuth authorization-code flow that lets
  a client with no secret sign in safely. The web app and the SDK both use it. Cognito supports it for public app clients and has no device-code grant.

- **Presigned URL**: An S3 URL that carries a signature, so its holder can do one operation (put or
  get one object) for a short time without AWS credentials.

- **Quarantine**: A planned administrator-only bucket for uploads that fail a server-side check. It is
  not built. Objects there would expire after 7 days, and researchers could not read them.

- **Recording session and upload session**: A recording session is a row of the Iceberg `sessions`
  table (key `bids_session`): one scanning or recording session of a subject. An upload session is a
  row of the DynamoDB `sessions` table that tracks one file from create to COMMITTED. They are
  different things.

- **Route table**: The list of HTTP API routes in `ingest/src/asteroidea_ingest/routes.py`. Each
  `Route` row has a method, a path, the function that serves it, the Cognito group the caller needs,
  a one-line summary and an optional timing name. There are 36 rows. The stack code adds one API
  Gateway route per row, and `api_common` looks a request up in the same rows. The test fixture
  `infra/tests/fixtures/routes.txt` lists the keys.

- **S3 Tables**: Amazon S3's managed Iceberg tables. Table and column names must be lowercase for Athena to
  see them. The platform uses Glue instead.

- **Snapshot**: One committed state of an Iceberg table. A query that names a snapshot id returns the
  same rows every time.

- **Stack budget**: The size limit for one CloudFormation template that the repository enforces: 300
  resources, 150 outputs, 150 parameters and 800,000 bytes. A stack near the limit is split by adding
  a new nested stack, never by moving an existing function.

- **`subject_key`**: A curated identifier that joins one person across datasets. It comes from the
  `subject_links` table, loaded by `make crosswalk`. No code derives it from a name. An unlinked
  subject is `<dataset>/<subject>`.

