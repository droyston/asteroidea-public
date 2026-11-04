# Decisions

Each significant architecture decision is an Architecture Decision Record (ADR) in the MADR 4 format. An
ADR states the decision it records, lists the options considered with the reason each rejected one
lost, and names any measurement that may still refine it. Each ADR also ends with a short plain "Current state" section
saying what it decided and whether that holds today.

## Status meanings

- **Accepted**: decided, and no planned measurement can change it.
- **Provisional**: decided, but one named measurement may still refine it. The ADR says which and what
  happens if it fails. Provisional is a dated promise to measure, not indecision.
- **Proposed**: derived from the researcher journeys, or decided but not yet built, rather than settled as its own
  question.
- **Open**: deliberately deferred. The ADR fixes only what later work must not contradict.

A status changes only by a dated update to its ADR.


## ADR index

The last column says what holds today.

| ADR | Title | Status | Decision today |
|---|---|---|---|
| 0001 | [Rust-based tooling where mature; clean-MVP scope](adr/0001-rust-tooling-clean-mvp.md) | Accepted | Use Rust-based tools where they are mature (uv, ruff, ty, prek) and keep the first version to a clean minimum. |
| 0002 | [Stateful/stateless stack split (DataStack, ServicesStack with nested stacks)](adr/0002-stateful-stateless-stack-split.md) | Provisional | Stateful resources sit in a termination-protected DataStack and code in ServicesStack, which holds nested stacks. The planned BaseStack was never built, so the two-stack shape is the design. Provisional because the nested-stack rollback test has not run. |
| 0003 | [Iceberg tables queried with Athena; SDK reads with DuckDB/Polars](adr/0003-iceberg-athena-catalog.md) | Provisional | The catalog is Iceberg tables on Glue. Athena runs corpus SQL and the SDK reads with DuckDB and Polars. Provisional because S3 Tables has not been evaluated. |
| 0004 | [Cognito user pool for researchers; Identity Center for admins only](adr/0004-cognito-researchers-identity-center-admins.md) | Accepted | Researchers sign in through Cognito. Administrators reach AWS only through IAM Identity Center. A `curators` group gates the import routes. |
| 0005 | [De-identified data only; SDK pre-check (the admin quarantine is not built)](adr/0005-deidentified-only-quarantine.md) | Accepted | Only de-identified data is accepted. This is a policy, not a built control: the SDK header check and the quarantine bucket are designed but not built. |
| 0006 | [BIDS and NWB native; vendor raw ephys ingest-only](adr/0006-bids-nwb-native.md) | Accepted | MRI is organised as BIDS and electrophysiology as NWB. Vendor raw formats are accepted at ingest only. |
| 0007 | [Frontend as S3 + CloudFront SPA](adr/0007-s3-cloudfront-spa.md) | Accepted | The web app is a single-page app served from S3 through CloudFront with Origin Access Control. Built. |
| 0008 | [arc42 + ADRs in mdBook](adr/0008-arc42-adr-mdbook.md) | Accepted | This document is arc42 chapters plus MADR records, built with mdBook. |
| 0009 | [SDK headless sign-in by copy-paste login URL (PKCE)](adr/0009-sdk-copy-paste-login.md) | Accepted | The SDK signs in on a machine without a browser by printing a login URL and taking back the pasted result (authorization code with PKCE). |
| 0010 | [Upload sessions in DynamoDB; Iceberg holds finalized records only](adr/0010-dynamodb-upload-sessions.md) | Provisional | In-flight upload state lives in DynamoDB. The catalog gets a row only when an upload is finalized. Provisional because the concurrent-finalize test as designed did not run; a queue now serialises catalog writes. |
| 0011 | [Classic public/private VPC with NAT on demand (not built)](adr/0011-vpc-nat-on-demand.md) | Accepted | The design is a VPC with no NAT gateway until a workload needs one. It is not built: the CDK app defines no VPC and no function runs in one. The record is a design for a future in-VPC workload. |
| 0012 | [uv workspace monorepo (infra, services, ingest, sdk; web and docs outside uv)](adr/0012-uv-workspace-monorepo.md) | Accepted | One uv workspace and one lockfile hold infra, services, ingest and the SDK. The web app and the docs sit outside uv. |
| 0013 | [ty strict, version pinned](adr/0013-ty-strict.md) | Accepted | ty checks types with every rule an error, at an exact pinned version, with a written escape-hatch policy. |
| 0014 | [Apache-2.0](adr/0014-apache-2-license.md) | Accepted | The repository is licensed under Apache-2.0. |
| 0015 | [Python 3.14 as the single workspace Python](adr/0015-python-3-14.md) | Accepted | Every package targets Python 3.14, the newest Python with a generally available Lambda runtime. |
| 0016 | [File bytes never transit the API; presigned PUT (multipart is not built)](adr/0016-presigned-uploads.md) | Proposed | Clients upload straight to S3 with presigned URLs, and the API carries only manifests and URLs. A single PUT is built. Multipart upload is not, so large files are not supported. |
| 0017 | [SDK data access with scoped temporary AWS credentials from a Cognito identity pool](adr/0017-identity-pool-data-access.md) | Provisional | The SDK exchanges its Cognito login for short-lived, group-scoped AWS credentials and reads the lake directly. Provisional because the quarantine read denial cannot be tested without a quarantine bucket. |
| 0018 | [Analysis-artifact lake: boundary and provenance contract only](adr/0018-artifact-lake-boundary.md) | Accepted | Analysis artifacts go to a separate store with provenance records and are readable only by their author. Sharing and indexing are open. |
| 0019 | [Dataset browser: a local survey tool for DANDI and OpenNeuro](adr/0019-dataset-browser.md) | Accepted | A local React and Vite page filters a harvested index of both archives, and a terminal command peeks inside one remote NWB file. |
| 0020 | [Research frontend: one analysis implementation, server-side compute, Lambda now](adr/0020-research-frontend-analysis.md) | Accepted | The SDK and the web app share one analysis module, and the platform computes in Lambda. |
| 0021 | [Research synthesis: sessions, recipe-driven events and readable result records](adr/0021-research-synthesis.md) | Accepted | The session is derived from the catalog, per-dataset recipe files and one shared resolver align events, and result records are readable by all researchers. Built and validated live. |



## Writing a new ADR

Copy the template at `docs/src/adr/template.md`. Add the ADR to `SUMMARY.md` and to the index above.
