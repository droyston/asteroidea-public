# Solution strategy

The strategy in one paragraph. Store every file once in S3, uploaded by presigned URL and never
through the API. Track each upload as a session that ends in an explicit finalize step. Keep the raw
files immutable. Catalog finalized files as rows in Iceberg tables that Athena queries and that the SDK
reads directly with DuckDB on short-lived, scoped AWS credentials. Keep stateful resources in one
stack that services cannot roll back. Describe every Lambda function and every API route as one plain
data row, so adding one is a row and not new wiring.

## Quality goals and the approach for each

| Goal (rank) | Approach | Decision |
|---|---|---|
| Raw-data integrity (1) | **Immutable raw bucket.** The raw bucket is versioned and has Object Lock. A bucket policy denies writes to every role except three named ingest roles, and only the commit function may delete. The commit function sets a governance retention on each verified object (default 1 day, set by `make deploy RAW_RETENTION_DAYS=<n>`). S3 computes a CRC64NVME checksum for every new object by default, and the commit function checks it before it copies the file into `raw/`. | [ADR-0005](adr/0005-deidentified-only-quarantine.md) |
| Raw-data integrity (1) | **Upload sessions with an explicit finalize.** A DICOM series or a vendor ephys folder has no single "done" event, so an upload is a session: create, upload, finalize. Session state lives in DynamoDB. Only a finalized file reaches the catalog. | [ADR-0010](adr/0010-dynamodb-upload-sessions.md) |
| Raw-data integrity (1), operable by one developer (5) | **File bytes never pass through the API.** API Gateway caps a payload at 10 MB and times out an integration at 29 to 30 s. Clients send files to S3 with a presigned PUT (one object up to 5 GB) or a presigned multipart upload (parts of 5 MiB to 5 GiB, up to 10,000 parts), and fetch them with a presigned GET. | [ADR-0016](adr/0016-presigned-uploads.md) |
| Raw-data integrity (1) | **One writer at a time per catalog.** Finalize requests and extract results go through SQS FIFO queues with a single message group, each queue with a dead-letter queue. The catalog writer therefore never commits twice at once. A stopped session is re-sent by `make reclaim`. | [ADR-0010](adr/0010-dynamodb-upload-sessions.md) |
| Reproducible queries (2) | **Iceberg and Athena.** The catalog is fourteen Iceberg v2 tables in the Glue database `asteroidea`. Iceberg time travel allows reproducible queries against exactly the same table snapshot, so a query result cites the snapshot id it ran against. Athena runs SQL for the web app. The SDK reads the same tables with DuckDB. | [ADR-0003](adr/0003-iceberg-athena-catalog.md) |
| Reproducible queries (2), first hour (6) | **Direct reads with identity-pool credentials.** The SDK swaps the researcher's login for temporary AWS credentials scoped by group, then reads tables and files straight from S3. Presigned reads through the API cover any path the credentials cannot scope. | [ADR-0017](adr/0017-identity-pool-data-access.md) |
| Low idle cost (3) | **Serverless, no always-on compute.** There is no VPC and no NAT gateway; one NAT gateway costs about $33 a month before data. The Glue Data Catalog's first million objects are free. Lambda, SQS, DynamoDB, Athena, Step Functions and CloudFront cost nothing when idle. | [ADR-0001](adr/0001-rust-tooling-clean-mvp.md) |
| De-identified data only (4) | **Policy now, checks later.** The platform accepts only de-identified data. The SDK header check and the quarantine bucket are not built. | [ADR-0005](adr/0005-deidentified-only-quarantine.md) |
| Operable by one developer (5) | **Two stacks.** `DataStack` holds the state: buckets, tables, the catalog, the user pool and the web hosting. It is termination-protected. `ServicesStack` holds the code in two nested stacks (`Ingest` and `Api`). A failed services deploy cannot roll back data. Nested stacks cannot be deployed or listed alone. | [ADR-0002](adr/0002-stateful-stateless-stack-split.md) |
| Operable by one developer (5) | **Rows, not wiring.** The function registry (`infra/.../functions.py`) holds one row per Lambda function. The route table (`ingest/.../routes.py`) holds one row per API route. The stack code loops over the rows. All API handlers share one kit, `api_common`, which finds the route, checks the caller's group, times the call and turns a refusal into the standard error body. | [ADR-0002](adr/0002-stateful-stateless-stack-split.md) |
| Operable by one developer (5) | **Templates are checked, not trusted.** A refactor must leave the synthesized CloudFormation templates equal to `origin/main`, apart from asset hashes. `make gate-templates` compares them. A change that is meant to alter a template lists the expected differences in `infra/template-diff.expected`. | [ADR-0002](adr/0002-stateful-stateless-stack-split.md) |
| Operable by one developer (5) | **Standard formats.** BIDS for MRI and NWB for ephys, so researchers' own tools read the data. | [ADR-0006](adr/0006-bids-nwb-native.md) |
| Productive in the first hour (6) | **SDK principles first.** One call to sign in, defaults for region, catalog and credentials, and one exception hierarchy whose messages say what to do next. Headless sign-in uses a copy-paste login URL. | [ADR-0009](adr/0009-sdk-copy-paste-login.md) |
| Productive in the first hour (6) | **One error contract.** Every refusal from the API has the body `{"error": <code>, "detail": <sentence>}`. The code is short and stable. The SDK and the web app branch on the code and show the sentence. | [API reference](api.md) |
| Research without a second implementation (6) | **One analysis module.** Rates, z-scores, spectrograms and band power live in `sdk/src/asteroidea/analysis.py`. The compute function runs the same files, so the web app and the SDK give the same numbers. The browser draws what the API returns and does no analysis arithmetic. | [ADR-0020](adr/0020-research-frontend-analysis.md) |


## Technology choices

| Concern | Choice | Decision |
|---|---|---|
| Infrastructure as code | AWS CDK in Python | [ADR-0002](adr/0002-stateful-stateless-stack-split.md) |
| Catalog table format | Apache Iceberg v2 on the Glue Data Catalog | [ADR-0003](adr/0003-iceberg-athena-catalog.md) |
| Corpus SQL | Amazon Athena | [ADR-0003](adr/0003-iceberg-athena-catalog.md) |
| SDK-side reads | DuckDB (SQL) and Polars (analysis) | [ADR-0003](adr/0003-iceberg-athena-catalog.md), [ADR-0020](adr/0020-research-frontend-analysis.md) |
| Upload-session state | Amazon DynamoDB | [ADR-0010](adr/0010-dynamodb-upload-sessions.md) |
| Write ordering | SQS FIFO queues | [ADR-0010](adr/0010-dynamodb-upload-sessions.md) |
| Researcher identity | Cognito user pool and identity pool | [ADR-0004](adr/0004-cognito-researchers-identity-center-admins.md), [ADR-0017](adr/0017-identity-pool-data-access.md) |
| Administrator identity | IAM Identity Center | [ADR-0004](adr/0004-cognito-researchers-identity-center-admins.md) |
| Web hosting | S3 and CloudFront with Origin Access Control | [ADR-0007](adr/0007-s3-cloudfront-spa.md) |
| Repository and tooling | uv workspace, ruff, ty, pytest, prek, mdBook | [ADR-0012](adr/0012-uv-workspace-monorepo.md), [ADR-0013](adr/0013-ty-strict.md), [ADR-0001](adr/0001-rust-tooling-clean-mvp.md) |


## What was considered and not chosen

Each decision record gives the full reasoning. In brief:

- A Postgres or DuckLake catalog needs an always-on database inside a VPC to hold metadata-sized tables.
- Snowflake and Databricks cost more and lock in more than this scale needs.
- S3 Tables (managed Iceberg) was an alternative to Glue. The platform uses Glue.
- AWS Amplify's CDK construct is alpha.
- A transform layer such as dbt is not needed over ingestion metadata.

