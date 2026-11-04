# ADR-0018: Analysis-artifact lake: boundary and provenance contract only

Status: Accepted
Date: 2026-10-22

## Context and Problem Statement

Researchers do not only read data; they produce analysis artifacts (derived images, spike sorts,
statistics, figures) and need somewhere to put them. The platform requires that researchers "save
analysis artifacts into a separate lake", with the design of that side left for later. Writing derived results
back into the main lake as new versions would contradict that. This record fixes the boundary between the two
lakes and the provenance an artifact must carry, and leaves the artifact lake's own design open.

## Considered Options

- Fix the boundary and the provenance contract now; design the artifact lake as its own piece of work.
- Write derived results back into the main lake as new versions, leaving raw objects untouched.
- Design the artifact lake fully now.

## Decision Outcome

Decided now: "Fix the boundary and the provenance contract", because the main lake's integrity and
the rest of the platform depend on the boundary, while the artifact lake's design depends on
requirements that are not yet known.

Not decided: the artifact lake's design. That is why this record is Open.

### The boundary (decided)

- The main lake holds primary research data and its catalog only. Researchers never write to it;
  only the ingest service does.
- Analysis artifacts go to a separate artifact lake, reached through an interface the SDK calls. To
  the rest of this architecture the artifact lake is an external neighbour system: the
  [context view](../03-context-and-scope.md) shows it as such, and the
  [building block view](../05-building-blocks.md) shows its interface as an external port.

### The provenance contract (decided)

Every artifact the SDK writes names the exact inputs it came from, so that it can be reproduced:

- the source asset id in the main catalog,
- the S3 version id of each source object,
- the Iceberg snapshot id of the catalog state the inputs were selected from.

This is the same snapshot id a reproducible query cites ([Crosscutting concepts](../08-crosscutting.md)).
It requires S3 versioning on the main lake's buckets, which the raw bucket already has; without
pinned versions, provenance links rot.

### Open questions for the artifact lake

- Where artifacts are stored and catalogued, and whether the artifact lake reuses the main lake's
  catalog engine.
- The write flow. A researcher-journey sketch has a two-phase register → presigned upload → finalize
  flow, so that no artifact lands without its provenance record, mirroring [ADR-0016](0016-presigned-uploads.md).
- Who may read whose artifacts, and how long artifacts are kept.
- Whether to follow BIDS-derivatives and NWB provenance fields; those were not fetched
  specifications.

### Consequences

- Good: the main lake's immutability and de-identified-only posture do not depend on a design that
  does not exist yet.
- Good: the provenance contract can be tested before the artifact lake exists: the SDK can produce a
  provenance record for any read it does.
- Bad: the first-hour script in [Quality requirements](../10-quality.md) has an artifact step that
  cannot run until the artifact lake exists, so it is measured without that step.
- No measurement refines this record. The artifact-lake design settles it, and its outcome is recorded here
  by a dated update that changes the status.

## Pros and Cons of the Options

### Boundary and provenance now, design later (chosen)

- Good: protects the main lake now; defers only what is genuinely unknown.
- Bad: the artifact step of the researcher journey stays unbuilt until the artifact lake is designed.

### Write-back into the main lake

- Rejected: it contradicts the requirement, which names a separate lake, and it would make
  researchers writers of the main lake, which the immutability goal forbids.

### Full design now

- Rejected: the artifact side was deferred (the requirement leaves that side for later) in favour of
  the boundary only.

## More Information

- Related: [ADR-0003](0003-iceberg-athena-catalog.md) (snapshot ids),
  [ADR-0016](0016-presigned-uploads.md) (upload mechanism the artifact lake may reuse),
  [ADR-0017](0017-identity-pool-data-access.md) (researcher credentials, which never write the main
  lake).

## Update (2026-10): the minimal write path

The Status moves from Open to Accepted for the minimal write path. That path is built: a separate artifacts bucket (outside the raw bucket, whose policy denies researcher writes), API routes that hand out presigned PUT URLs bound to the content checksum, a provenance record (sources as `FileRef`s, shape only), and a read limited to the author (relaxed below). A PUT of other bytes is refused by S3 and never commits.
 Sharing results and an artifact catalog or index stay open and need their own decision. The Status applies to the minimal write path only.

## Update (2026-10): labels and the derived store

Two things join the artifact side of the boundary, and the provenance contract applies to both.

- **Derived labels are shared, and only their author edits them.** Provided epochs and events are primary data in the main catalog (`intervals`, `events`). A researcher's derived label (name, optional note, start and stop in seconds) lives outside the catalog in a DynamoDB `LabelsTable` behind `labels_api.py`. Every researcher reads every label; only the author may PATCH or DELETE it (403 otherwise, 409 on a stale `version`); an asset holds at most 2,000 labels. This is the first artifact-side data that is not limited to its author, so the earlier "readable by its author only" rule is relaxed for this kind and no other. Artifacts saved by `save_artifact` stayed limited to their author until the later update below.
- **Provenance.** The server stamps each label with the catalog version it was drawn against: the asset's S3 version id and, per table, the Iceberg `current-snapshot-id` (read from the Glue table's `metadata_location` and one S3 `GetObject`). If that read is refused the label is stored with `snapshots: null` (the fallback).
- **SDK read.** `asteroidea.labels(<asset id>)` returns the records through the API. SDK label writes are not built.
- **Platform-written derived store.** A third kind, written by the platform, never by a researcher: the derived bucket holds `lfp/<asset>/<version>/` (chunk-offset index and overview, written by LfpBuildRole only) and `rows/<asset>/<run>/` (re-extract row bundles, written by the derive role only, expiring after 7 days). Researchers read an overview only through a presigned GET from `/compute/overview`. The decision on direct reads is unchanged.

## Update (2026-10): research synthesis

Decisions in [ADR-0021](0021-research-synthesis.md). The first synthesis stage widens the artifact side of the boundary again. The provenance contract still applies to everything below.

- **The record is version 2.** `schema` is `asteroidea.provenance/2`. It adds the run (function, parameters, defaults version, SDK version), the inputs of kind `asset`, `artifact`, `table_snapshot`, `defaults` and `recipe`, the author's email (`author_email`, read from the `email` claim of the ID token the SDK sends on create) and a `data_key` fixed at create. A body without `schema` keeps the version 1 rules and stores `author_email` null; `make artifacts-backfill` fills the email, `data_key` and the index objects of older records.
- **Every researcher reads every finished artifact.** The record and the bytes of a `COMMITTED` or `WITHDRAWN` result are readable and downloadable by every researcher, with the author's email. This supersedes the earlier "readable by its author only" rule for artifacts saved by `save_artifact` (the labels update above already relaxed it for labels). Only the author edits the name or note, withdraws or deletes; a result that another result used cannot be deleted (409 `has_dependents`).
- **Index objects, written by the PlatformFunction only.** In the artifacts bucket: `index/by-id/<artifact_id>` (author and state), `index/by-input/<input id>/<artifact_id>` (the "results that used this file or result" lookup) and `index/by-time/<inverted ms>-<artifact_id>` (the newest-first listing). The PlatformFunction role gains `index/*`; researchers still never write the bucket.
- **Catalog tables for lineage are still open.** Lineage in catalog tables, staleness and precomputed summaries move to a later release, which loads the same version 2 records with no format change. Sharing is by all-read, not by a share list; there is no private artifact. The privacy consequence of the email is in [Risks](../11-risks.md#data-and-access).

## Current state

Researchers write analysis artifacts, with provenance links, to a separate bucket, and the main lake keeps primary data only. The reason is to settle the boundary now and leave the artifact design for later. It holds: `storage.py` defines a separate artifacts bucket that only the platform role may read or write, and `ingest/src/asteroidea_ingest/artifacts.py` and the SDK provide a minimal write path. Sharing and indexing of artifacts are still open.
