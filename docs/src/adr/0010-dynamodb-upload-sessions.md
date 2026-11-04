# ADR-0010: Upload sessions in DynamoDB; Iceberg holds finalized records only

Status: Provisional
Date: 2026-10-22

## Context and Problem Statement

An upload is not one event. A DICOM series is thousands of small files, and a vendor
electrophysiology recording is a directory whose files are useless apart (a SpikeGLX `.meta` without
its `.bin`). Neither has a single "done" signal, so the platform needs an upload session with an
explicit finalize step. Something has to
hold the in-flight state of each session: the manifest, which objects have landed, URL issue times,
checksums and the finalize outcome. The question is where that state lives, and whether the catalog
ever sees a record that is not yet complete.

## Considered Options

- A DynamoDB table of upload sessions; the Iceberg catalog receives one row per asset only at finalize.
- In-flight session rows written to the Iceberg catalog and updated as objects land.
- A Postgres (Aurora Serverless v2) database holding both sessions and the catalog.
- No session: infer completion from S3 "Object Created" events alone.

## Decision Outcome

Chosen option: "A DynamoDB table of upload sessions", because it keeps mutable, per-object state out
of the catalog, costs nothing while idle, and needs no VPC. The catalog then holds only finalized,
checksum-verified records, which is what lets a query be reproduced against a catalog snapshot.

The session lifecycle that the [runtime view](../06-runtime.md) walks is: create the session →
presigned single or multipart PUTs → finalize → verify CRC64NVME checksums → validate headers →
quarantine or accept → extract metadata → commit one Iceberg row per asset. The session item records
the outcome of each step.

### Consequences

- Good: DynamoDB on-demand bills per request and per GB stored ($0.625 per million writes, $0.125
  per million reads, $0.25 per GB-month), so an idle platform pays nothing for sessions.
- Good: abandoned sessions can expire in DynamoDB without ever touching the catalog.
- Good: finalize is the only writer to the catalog, which bounds where concurrency problems can
  appear.
- Bad: two stores to keep consistent. A finalize that commits the Iceberg row but fails to update the
  session must be safe to retry, so the commit has to be idempotent on asset id and S3 version id.
- Bad: DynamoDB is not queryable from Athena or DuckDB without extra work, so in-flight uploads are
  invisible to corpus queries by design.
- Open tension: the catalog also needs columns that change after finalize, such as quality-control
  state. "Iceberg holds only finalized records" holds only if those columns live elsewhere. The
  ingest design decides; the planned default is a separate append-only QC table. See [Risks and technical debt](../11-risks.md).

### What would change this decision

- Question: does the Iceberg write path hold under concurrent finalizes?
- Deciding metric: 10 finalizes run concurrently commit 10 rows, with no lost rows and a bounded
  number of commit retries.
- If it fails: if rows are lost, finalize stops committing directly. Finalizes go through a
  single-writer SQS FIFO queue instead. Sessions stay in DynamoDB either way; only the commit mechanism
  changes.
- The test belongs to the initial implementation. It measures appends only; row-level updates belong to
  the QC tension above.

## Pros and Cons of the Options

### DynamoDB sessions, catalog row at finalize (chosen)

- Good: zero idle cost; no VPC; per-item updates are what DynamoDB is built for.
- Good: the catalog is append-mostly and holds only complete records.
- Bad: a second store, and a cross-store commit to make idempotent.

### In-flight rows in the Iceberg catalog

- Rejected: every landed object becomes a table update. DuckDB's Iceberg updates are merge-on-read
  only, and per-row upsert needs `MERGE INTO`, so frequent small updates pile up delete files and snapshots.
- Bad: corpus queries would see half-uploaded assets unless every query filters them out.

### Postgres (Aurora Serverless v2)

- Rejected: it needs an always-on VPC database for metadata-sized tables. Aurora Serverless v2 can pause at 0 ACU, but it still
  bills storage while paused and takes about 15 s to resume.

### S3 events only, no session

- Rejected: a DICOM series or a vendor ephys directory has no single completion event, and duplicate
  or out-of-order events must be de-duplicated, so completion cannot be inferred safely.

## More Information

- Related: [ADR-0003](0003-iceberg-athena-catalog.md) (catalog),
  [ADR-0005](0005-deidentified-only-quarantine.md) (the quarantine branch of finalize),
  [ADR-0016](0016-presigned-uploads.md) (how bytes reach S3).

## Update (2026-10): the claim-once flow

The first version trimmed ingest to what the concurrency test needs, and an upload was reduced to one file. The Status is unchanged. As shipped:

- **Table.** `sessions`, on-demand, partition key `session_id`, no sort key (one session holds one file),
  time-to-live attribute `expires_at`, point-in-time recovery on. A global secondary index `by_owner`
  (`owner_sub`, `created_at`) serves `GET /uploads`. Session states at this point are OPEN, FINALIZING,
  ACCEPTED, COMMITTED and INCOMPLETE. QUARANTINED does not exist yet.
- **Claim-once.** `POST /sessions/{id}/finalize` makes one conditional update OPEN to FINALIZING with a
  random `finalize_token`, then starts the commit Lambda asynchronously and answers 202. A second finalize
  never starts a second run.
- **Second claim.** The commit Lambda verifies the landing object's CRC64NVME checksum (read-only), then
  makes a conditional update FINALIZING to ACCEPTED, and only then copies to `raw/` and appends. A duplicate
  delivery or retry fails that condition, so the copy and the append happen at most once per session.
- **Idempotence.** The asset id is `uuid5(raw_key@raw_version_id)`. The committer scans `assets` for the
  session id before appending (check-before-append).
- **Measured.** A wrong checksum leaves the session INCOMPLETE with nothing readable in `raw/` and no
  catalog row; the finalize 202 to a visible row takes 5.3 s to 8.6 s once warm.
- **Still open.** The concurrent-finalize test (with its SQS fallback) did not run, so the Status
  stays Provisional. These items are also open: reclaim of a session stuck in
  FINALIZING or ACCEPTED (one real instance exists, left in place), adoption of an orphaned `raw/` copy,
  re-finalize after INCOMPLETE, URL refresh, and the first commit after an image push, which can hit the
  10 s init timeout.

## Update (2026-10): leases and reclaim

New attributes carry a lease, an attempt count, an `extract_state` and an `extract_error`; a session stopped in FINALIZING, ACCEPTED or COMMITTED is reclaimed by `make reclaim` (a dry run with `DRY=1`; `FORCE=1 SESSION=<id>` expires the lease of one resumable session). Import sessions use the same table. Timeouts are 120 s, queue visibilities 720 s and claim leases 150 s. Extraction retries transient errors twice inside the invocation; that retry is covered by unit tests only. Status stays Provisional.

## Current state

In-flight upload state lives in a DynamoDB table of upload sessions, and the Iceberg catalog receives a row only after an upload is finalized and verified. The reason is that mutable state stays out of the catalog, the table costs nothing idle, and it needs no VPC. It holds: `storage.py` defines the on-demand sessions table with a time-to-live attribute, and the commit code writes the catalog row last. Nothing replaced it.
