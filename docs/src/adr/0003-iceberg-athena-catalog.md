# ADR-0003: Iceberg tables queried with Athena; SDK reads with DuckDB/Polars

Status: Provisional
Date: 2026-10-22

## Context and Problem Statement

Research files (MRI images, electrophysiology recordings) are stored in S3 as they arrive. The
platform also needs a catalog: one row per file or scan, with its subject, session, modality, checksum
and S3 version, so that researchers can find data by query ("T1-weighted scans, age 20 to 30, passed
QC") and then read the matching files directly. The
platform asks for this catalog in "AWS duckdb if possible, or rds, and glue".

The catalog tables are small: metadata about files, not the files themselves. What matters is no idle
cost, reproducible queries and direct reads from the researcher's own Python session.

Apache Iceberg is an open table format: a table is a set of data files plus metadata files in object
storage, and every commit produces a new snapshot that can be read again later. Iceberg tables on AWS
can live in two places. S3 Tables is a managed table-bucket service with automatic compaction. A Glue
Iceberg table is a table registered in the Glue Data Catalog over files in an ordinary S3 bucket.

## Considered Options

- Iceberg tables (S3 Tables or Glue-managed), Athena for corpus SQL, DuckDB and Polars in the SDK
- A Postgres-backed catalog (Aurora Serverless v2, or DuckLake or a Rust catalog that needs Postgres)
- A managed data platform (Snowflake or Databricks)
- A hosted DuckDB catalog service on AWS

## Decision Outcome

Chosen option: "Iceberg tables, Athena for corpus SQL, DuckDB and Polars in the SDK", because it has no
idle compute cost, stores the catalog in an open format next to the data, and gives every query a
snapshot to cite.

- Athena runs SQL across the whole corpus and costs $5 per TB scanned with nothing to run when idle.
- The SDK reads tables and files directly with DuckDB or Polars, using short-lived AWS credentials
  (see [ADR-0017](0017-identity-pool-data-access.md)).
- PyIceberg 0.12 supports append, overwrite and upsert, so the platform's ingest code has a Python
  write path.
- Whether the tables live in S3 Tables or in Glue is not yet decided; a comparison of the two decides it.

### Consequences

- Good: about $0 at idle. The Glue Data Catalog's first million objects and first million requests are
  free.
- Good: a query result can cite the Iceberg snapshot it read, so it can be run again on the same data.
- Bad: the write libraries are pre-1.0. DuckDB 2.0 writes Iceberg only through an attached REST
  catalog, and its documentation contradicts itself on whether S3 Tables writes work: one page says
  S3 Tables "supports reading", another says attached catalogs have the full feature set, including
  writing.
- Bad: Athena's Iceberg page says it supports Iceberg format v2 tables and the Glue catalog only,
  while the S3 Tables page says v3 is supported and that Athena queries S3 Tables. The contradiction
  is unresolved.
- Bad: S3 Tables has only low-level (L1) CDK constructs in `aws-cdk-lib`; the higher-level (L2)
  constructs are in a separate experimental package. Table and column names must be lowercase, or
  Athena cannot see the table.
- Bad: several uploads may finish at the same moment, and each commits a row to the same Iceberg
  table. Whether concurrent commits lose rows is unmeasured; a concurrency test measures it.
- Neutral: Polars 2.0 reads Iceberg through PyIceberg, so PyIceberg is on the read path too.

### Provisional: two open measurements

Both measurements were planned for the initial implementation; the update below records what ran.

**S3 Tables versus Glue Iceberg on S3.**

- **Deciding metric:** pass or fail per engine: append from PyIceberg and from DuckDB 2.0; read back
  from Athena; Athena DDL; idle cost. S3 Tables is picked only if every write path passes.
- **If it fails:** if neither option has a write path from Lambda, only the sub-choice (S3 Tables or
  Glue) reopens. Iceberg and Athena stand.

**Iceberg write path under concurrent finalizes.**

- **Deciding metric:** ten concurrent upload finalizes, no lost rows, and a bounded number of retries.
- **If it fails:** if rows are lost, writes go through a single-writer SQS FIFO queue.

## Pros and Cons of the Options

### Iceberg tables, Athena, DuckDB and Polars

- Good: no idle cost, an open format, snapshots for reproducibility, and both a managed SQL engine
  and local engines over the same tables.
- Bad: pre-1.0 write libraries and contradictory documentation, to be settled by the two measurements above.

### A Postgres-backed catalog

- Good: transactional upserts; Aurora Serverless v2 can scale to 0 capacity units and pause.
- Bad: rejected because it needs an always-on database inside a VPC for metadata-sized tables. Aurora needs a VPC, bills storage while paused and
  takes about 15 seconds to resume.
- Bad: DuckLake, Lakekeeper (Rust) and Apache Polaris also need a separate catalog database such as
  Postgres.

### A managed data platform (Snowflake or Databricks)

- Good: mature SQL engines.
- Bad: rejected for cost and lock-in at this scale, judged on third-party prices.

### A hosted DuckDB catalog service on AWS

- Good: the first choice named in the platform's requirement for this catalog.
- Bad: no AWS-managed DuckDB offering was found. That is an absence claim from the AWS pages
  read, not a command result. DuckDB remains in the
  design as a reader in the SDK.

## More Information

- Related: [ADR-0001](0001-rust-tooling-clean-mvp.md), [ADR-0010](0010-dynamodb-upload-sessions.md)
  (only finalized records reach Iceberg), [ADR-0017](0017-identity-pool-data-access.md) (direct reads).

## Update (2026-10): the Glue check

The catalog comparison was reduced to a check on one catalog, Glue-managed Iceberg on S3, and the
S3 Tables evaluation is deferred. The check ran against a sandbox account.
The status is unchanged.

- **CloudFormation cannot create the table.** `AWS::Glue::Table` with an Iceberg `OpenTableFormatInput`
  failed with "Table metadata is expected only via TableInput or via IcebergTableInput inside
  OpenTableFormatInput". CloudFormation creation of a version 2 Iceberg table therefore does not work
  in the form tried. The table is not part of DataStack. `make catalog-init` creates `asteroidea.assets`
  through PyIceberg's Glue catalog as format-version 2. DataStack holds the buckets, the Glue database and
  the Athena workgroup.
- **Criteria.** PyIceberg append through the Glue catalog: pass, 3 rows into `s1_probe`. Athena read-back at
  the snapshot: count 3. Athena `ALTER TABLE ... ADD COLUMNS`: pass. The idle cost of $0 comes from Glue
  pricing and was not measured. The write path from the commit Lambda was then exercised by the
  first-hour run.
- **Not run.** DuckDB 2.0 was not used as a writer; the criterion "append from DuckDB 2.0" was dropped.
  DuckDB and Polars were used as readers of the Glue table (see
  [ADR-0017](0017-identity-pool-data-access.md)).
- **The Athena version contradiction** (Consequences above) is half answered: Athena read a Glue-registered
  version 2 table. Whether Athena reads an S3 Tables version 3 table is still unmeasured.

**Why the Status line stays "Provisional".** The check showed that Glue works for the write and read
paths used so far. It did not compare Glue with S3 Tables, so the S3 Tables sub-question is
deferred, not decided on evidence. The concurrent-commit test is postponed; its
fallback (a single-writer SQS FIFO queue) is still available. Iceberg and Athena stand.

## Update (2026-10): ten tables and a single writer

At this update the catalog held ten Iceberg v2 tables on Glue (`assets` plus `datasets`, `subjects`, `subject_links`, `sessions`, `asset_headers`, `series`, `electrodes`, `units`, `intervals`), created and evolved by an idempotent `make catalog-init` that adds optional columns and refuses a type conflict. Each table has one writer, fed by an SQS FIFO queue with one message group; this adopts the single-writer fallback up front, and the stand-alone concurrency test did not run. The acceptance check applied the same criterion: 10 concurrent finalizes gave 10 rows, 0 overlaps, in 49 s. The SDK reads with DuckDB `iceberg_scan` on each table's metadata location (5.27 s on the first call, 0.26 s warm). One snapshot per file per table grows metadata; table maintenance is deferred. Status stays Provisional.

## Update (2026-10): twelve tables

The catalog grows to twelve tables: `unit_spikes` (one row per unit, `spike_times`, `list<double>`) and `events` (BIDS `events.tsv` rows, keyed by `events_for`, the entity key of the scan they describe) join the earlier ten.

- **Per-table replace.** `catalog_writer.py:: CatalogWriter.replace` deletes a file's rows and appends the new rows in one PyIceberg transaction, so each table gets one commit and each commit is atomic.
  There is no atomicity across tables: a re-extract writes the asset's tables one commit each and `asset_headers` last, and a failure names the tables already replaced. A reader can see new units beside old intervals until the re-run finishes.
- **Two snapshots per commit.** Delete-then-append inside one transaction leaves two snapshots per commit in the Iceberg log, which is why label provenance ([ADR-0018](0018-artifact-lake-boundary.md)) reads the table's `current-snapshot-id`, not the newest row of `$snapshots`.
- **Re-extraction.** `make reextract` is the repeatable backfill. It fans the read-and-derive phase out with Step Functions and sends one pointer message per asset to the extract FIFO, which still commits serially.
- **Athena precision.** Only metadata columns are timestamps; every analysis time is a double in seconds. Athena returns doubles at full precision as text and `array` cells as text (`"[732343, 56]"`); `athena_query.py:: _cell` parses them. Parameters are rendered as quoted varchar literals (`render_param`), because Athena types an `ExecutionParameters` literal by its text.

This decision is otherwise unchanged. Status stays Provisional.

## Current state

The catalog is Iceberg tables queried with Athena, and the SDK reads with DuckDB and Polars. The reason is no idle cost and a snapshot to cite for each query. It holds. The tables are Glue-managed Iceberg, not S3 Tables, and S3 Tables was never adopted. CloudFormation rejected the Iceberg table resource, so `make catalog-init` creates and evolves the tables through PyIceberg. The open question about the Glue-or-S3-Tables choice is answered: Glue.
