# Context and scope

## Business context

```text
                  +---------------------------+
                  |        Researcher         |
                  |  (web app, Python SDK)    |
                  +-------------+-------------+
                                |
      upload, browse, query,    |    save analysis results
      read lazily               |    (with their inputs)
                                v
 +------------------------------+-------------------+        +----------------------+
 |                    asteroidea                     | writes |   Artifacts bucket   |
 |  main data: de-identified primary data + catalog  |------->|  (saved results,     |
 |  (raw files are read-only once finalized)         |        |   one author each)   |
 +------------------------------+-------------------+        +----------------------+
                                ^
      deploy, operate           |             public archives (read only): DANDI, OpenNeuro
                  +-------------+-------------+
                  |      Administrator        |
                  |  (Identity Center SSO,    |
                  |   AWS CLI, console, CDK)  |
                  +---------------------------+
```

| Partner | What it gives asteroidea | What it gets from asteroidea |
|---|---|---|
| Researcher | MRI sessions, ephys recordings, queries, labels, analysis requests | Upload status, catalog listings, query results as file references, byte ranges of files, plots, saved results |
| Curator (a researcher in the `curators` group) | Requests to import a public dataset | The import run and its outcome per file |
| Administrator | Stack deployments, account management | Stack status, logs |
| Artifacts bucket | Nothing | Each result file with a record of its inputs |
| DANDI Archive and OpenNeuro | Dataset summaries and file listings from their public APIs; file bytes for a curator's import | Nothing |

### The artifacts boundary

Analysis results are not stored with the main data. They go to a separate S3 bucket, the artifacts
bucket. The design of a full artifact store is still open ([ADR-0018](adr/0018-artifact-lake-boundary.md)).
Only the boundary is fixed:

- **The main data does not accept** analysis outputs, derived files or any researcher write after
  finalize. Only the ingest path writes there. The platform itself writes derived data (the LFP
  summaries and re-extract bundles) to a third bucket, the derived store, which researchers cannot write.
- **The artifacts bucket receives** each result with a record naming its sources: the source asset id,
  the S3 version id of each source file and the Iceberg snapshot id of the catalog state the inputs came
  from. Only the author can list or download a result.
- **The main data guarantees** that every id in such a record stays resolvable. Source file versions
  are not deleted, and the catalog keeps the snapshots that records cite.


### Public archives

DANDI is a public archive for neurophysiology data. It separates an immutable blob (a checksum and a
size), an asset (a path plus JSON metadata) and a versioned collection. The platform's catalog copies
that separation. OpenNeuro describes itself as "a free and open platform for analyzing and sharing neuroimaging data".

The platform reads both through their public APIs. The web app lists their datasets so a researcher can
browse them. A curator can import chosen files: the import function copies the file from OpenNeuro's
public bucket, or streams it from DANDI, checks the published checksum and then runs the normal commit
path. Imports are limited to 50 files per run.


## Technical context

```text
  Researcher's browser                          Researcher's SDK (Python)
        |                                              |
        | (1) sign in: authorization code + PKCE        | (1) sign in: copy-paste login URL (PKCE)
        v                                              v
  +--------------------------- Cognito user pool -----------------------------+
  |  issues ID, access and refresh tokens (JWTs) to both clients              |
  +---------------------------------------------------------------------------+
        |                                              |
        | (2) static app                               | (2) exchange the ID token
        v                                              v
  +-------------+                              +---------------------------+
  | CloudFront  |---> S3 (site bucket, OAC)    | Cognito identity pool     |
  +-------------+                              | -> temporary, scoped AWS  |
        |                                      |    credentials per group  |
        | (3) JSON API calls with the JWT      +---------------------------+
        v                                              |
  +-------------+   (4) presigned URLs          (3) direct reads with temporary credentials
  | API Gateway |------------------------+             |
  | + Lambda    |                        |             v
  +-------------+                        |     +-----------------------------+
        |                                +---->| S3 (raw, warehouse, derived)|
        +--> DynamoDB, SQS, Athena               | Iceberg tables on Glue      |
             Step Functions                      +-----------------------------+

  Administrator --> IAM Identity Center (SSO) --> permission-set role --> CDK / AWS CLI / console
```

| Channel | Protocol and credential | Carries | Decision |
|---|---|---|---|
| Browser to Cognito user pool | OAuth 2.0 authorization code with PKCE | Sign-in, tokens | [ADR-0004](adr/0004-cognito-researchers-identity-center-admins.md) |
| SDK to Cognito user pool | Authorization code with PKCE. The user copies the login URL into any browser and pastes the result back. Cognito documents no device-code grant, so this flow is the chosen mechanism. | Sign-in, tokens | [ADR-0009](adr/0009-sdk-copy-paste-login.md) |
| Browser to CloudFront to S3 | HTTPS. CloudFront reads the site bucket through Origin Access Control. | The single-page app, the public dataset index | [ADR-0007](adr/0007-s3-cloudfront-spa.md) |
| Browser and SDK to API Gateway | HTTPS with the user-pool JWT. One HTTP API with 36 routes served by six Lambda functions (see the [API reference](api.md)). | Upload sessions, catalog browsing, compute requests, labels, imports, artifact sessions, get-started values | [ADR-0016](adr/0016-presigned-uploads.md) |
| Browser and SDK to S3 (presigned) | HTTPS PUT or GET with a presigned URL | File bytes for upload and for download | [ADR-0016](adr/0016-presigned-uploads.md) |
| SDK to Cognito identity pool | The user-pool ID token is exchanged for temporary AWS credentials scoped by the researcher's group | Credentials | [ADR-0017](adr/0017-identity-pool-data-access.md) |
| SDK to S3 (direct) | AWS SigV4 with the temporary credentials | DuckDB reads of Iceberg tables and ranged reads of files | [ADR-0003](adr/0003-iceberg-athena-catalog.md), [ADR-0017](adr/0017-identity-pool-data-access.md) |
| Lambda functions to Athena | The function's own role | Catalog and compute queries for the web app | [ADR-0003](adr/0003-iceberg-athena-catalog.md) |
| Administrator to AWS | IAM Identity Center sign-in (`aws configure sso` uses the OIDC device-code flow); a permission set becomes an IAM role that only Identity Center users can assume | Deployments, operations | [ADR-0004](adr/0004-cognito-researchers-identity-center-admins.md) |


File bytes never pass through the API. API Gateway caps a payload at 10 MB and an integration at about
30 seconds, so clients send and fetch files straight to and from S3. The API timeouts are 10 s for the
session, get-started and imports functions, 20 s for the catalog and labels functions and 28 s for the
compute function.


## Scope

In scope: upload, cataloguing, corpus query, lazy file reads, the web app, the SDK, the record of a
result's inputs, import of public datasets by curators, and plots and labels for NWB and NIfTI files.

Out of scope: a full design of the artifact store ([ADR-0018](adr/0018-artifact-lake-boundary.md));
identifiable data of any kind ([ADR-0005](adr/0005-deidentified-only-quarantine.md)); a transform or
warehouse layer ([ADR-0001](adr/0001-rust-tooling-clean-mvp.md)); multi-account and multi-region
deployment.

Not built yet: a VPC, validation of uploads, the quarantine bucket, multipart upload and continuous
integration.
