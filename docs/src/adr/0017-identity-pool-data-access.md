# ADR-0017: SDK data access with scoped temporary AWS credentials from a Cognito identity pool

Status: Provisional
Date: 2026-10-22

## Context and Problem Statement

Researchers sign in through a Cognito user pool ([ADR-0004](0004-cognito-researchers-identity-center-admins.md)),
and the SDK reads the catalog with DuckDB and Polars ([ADR-0003](0003-iceberg-athena-catalog.md)).
Those two decisions do not compose on their own. A user-pool login yields JSON Web Tokens, but
DuckDB or Polars scanning Parquet and Iceberg files on S3, and Athena running a query, all need AWS
credentials that sign requests with SigV4. A token is not such a credential. The question is how the SDK turns a researcher's login into access to the data, scoped
so that a researcher can read what their group may read and can write nothing in the main lake.

## Considered Options

- A Cognito identity pool exchanges the user-pool login for short-lived AWS credentials; each Cognito
  group maps to an IAM role; the SDK reads S3 and the Iceberg tables directly.
- An API that brokers every read: the SDK asks for each object and receives a presigned GET URL, and
  queries run server-side.
- IAM Identity Center for researchers as well as admins, giving them SSO credentials.
- Long-lived IAM user access keys per researcher.

## Decision Outcome

Chosen option: "A Cognito identity pool", because it is the only option that lets the SDK run
DuckDB and Polars locally over the lake, which is the direct-read half of the platform's
"direct file read and corpus query" goal, while keeping researchers on the Cognito login.

Flow: the SDK signs in ([ADR-0009](0009-sdk-copy-paste-login.md)), then exchanges the user-pool
token with the identity pool for temporary credentials of the role mapped to the researcher's group.
DuckDB and Polars use those credentials for S3 and Iceberg reads; Athena queries use them too. The
credentials refresh within a session.

### Consequences

- Good: local, engine-native reads: DuckDB can attach S3 Tables or Glue Iceberg catalogs, and Polars
  `scan_iceberg` reads through PyIceberg.
- Good: no server in the read path, so no API timeout or payload cap on large reads.
- Good: the scope of each group is an IAM policy that can be reviewed and tested.
- Bad: the SDK now holds AWS credentials. Their scope is the whole security boundary for direct reads,
  so a policy that is too broad over-privileges every researcher in that group.
- Bad: researchers see AWS errors (access denied, expired token) unless the SDK translates them. The
  SDK principles in [Crosscutting concepts](../08-crosscutting.md) require that translation.

### What would change this decision

- Question: do identity-pool credentials give the SDK direct reads with the right scope?
- Deciding metric, all four must pass:
  1. The SDK gets scoped credentials from a Cognito login.
  2. DuckDB and Polars read S3 objects and the Iceberg table with those credentials.
  3. A researcher role cannot write the raw bucket or the quarantine bucket.
  4. The credentials refresh within a session.
- If it fails: if identity-pool roles cannot express the scoping, the SDK
  falls back to API-brokered presigned reads for the affected paths only.
- The test belongs to the initial implementation.

## Pros and Cons of the Options

### Identity-pool credentials (chosen)

- Good: local DuckDB and Polars over the lake; no read server; one login.
- Bad: credential scope is the security boundary and must be tested.

### API-brokered reads only

- Good: the server sees and logs every access; researchers never hold AWS credentials.
- Rejected: no local DuckDB over the lake.
  It remains the named fallback for any path the scoping test cannot cover.

### Identity Center for researchers

- Good: SSO credentials that refresh automatically through the device-code flow.
- Rejected: researchers use Cognito and Identity Center is for admins only ([ADR-0004](0004-cognito-researchers-identity-center-admins.md)).

### Long-lived IAM access keys

- Bad: long-lived secrets on researchers' machines, managed by hand per person, outside the Cognito
  login.

## More Information

- Related: [ADR-0003](0003-iceberg-athena-catalog.md), [ADR-0004](0004-cognito-researchers-identity-center-admins.md),
  [ADR-0009](0009-sdk-copy-paste-login.md), [ADR-0016](0016-presigned-uploads.md) (writes go through
  presigned URLs, not these credentials).

## Update (2026-10): scoping test, criteria 1 to 4 passed

The test ran against a sandbox account. The Status is unchanged.

1. **Scoped credentials from a login.** A Cognito login in the `researchers` group yields credentials of
   ResearcherRole. A user in no group is refused.
2. **Direct reads.** DuckDB 2.0.0 (`iceberg_scan` on the table's metadata file) and Polars 2.0.0
   (`scan_iceberg`) each read the 3 rows of `s1_probe`; DuckDB read a `raw/` object. Athena ran as
   ResearcherRole (count 3).
3. **Writes denied.** All 5 attempted writes returned AccessDenied: PutObject, DeleteObject and
   DeleteObjectVersion on raw, PutObject on landing, CopyObject into raw.
4. **Refresh.** A forced expiry of the cached credentials produced a new AccessKeyId and the next Glue read
   succeeded (no 70-minute soak).

Criteria 1 to 4 passed, including the forced refresh. The Status stays Provisional because the
quarantine-read denial is untested until the quarantine bucket exists. What the test did not cover:

- **The quarantine half of criterion 3.** There is no quarantine bucket yet; that half, the
  quarantine-read denial, runs once the bucket exists. The Status changes to Accepted when it passes.
- **A repeatable test.** Criterion 3 was run by the one-shot script `s5_s8_check.py s8`; the repeatable
  live test is `tests/live/test_raw_bucket_deny.py`, and an offline policy test guards the explicit
  deny.
- **The raw-bucket policy.** The write denials rest on ResearcherRole's own explicit deny, not on a bucket
  policy, which was not built yet.
- **Update (2026-10).** The two items above are closed: `tests/live/test_raw_bucket_deny.py` exists and passed 7 of 7 on the deployed stack, and the raw bucket policy is built and deployed. The Status stays Provisional.
- **DuckDB attaches no catalog.** The measured read path is `iceberg_scan` on a metadata file. The
  Consequences line about attaching S3 Tables or Glue catalogs is unmeasured.

## Update (2026-10): scoped SQL

ResearcherRole's Glue grant names the ten catalog tables; it holds no wildcard and not `s1_probe`. `sql()` runs DuckDB under the researcher's scoped credentials, is read-only (a write raises `QueryError`), and registers a view only for a table the query names. Status stays Provisional: the quarantine half cannot run until that bucket exists.

## Update (2026-10): the web app

The web app uses no AWS credentials: it signs in with the Cognito access token only and every byte path goes through the API (JSON routes) or a presigned URL the API issued (MRI volumes, LFP overviews). Identity-pool credentials remain the SDK's data path (`sql()`, lazy reads); the compute, labels and imports routes are API routes under the JWT authorizer, so the browser analysis surface needs no ResearcherRole. A user in both `researchers` and `curators` keeps identity-pool mapping ([ADR-0004](0004-cognito-researchers-identity-center-admins.md)). The Status is unchanged.

## Current state

The SDK exchanges its Cognito sign-in for short-lived, group-scoped AWS credentials from a Cognito identity pool, and reads the lake directly. The reason is that it is the only way to run DuckDB and Polars locally over the lake while keeping the Cognito login. It holds: `identity.py` builds the identity pool and ResearcherRole, and the Glue grant names each catalog table. The check that quarantine reads are denied cannot run, because no quarantine bucket exists.
