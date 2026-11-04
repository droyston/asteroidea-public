# Deployment view

The platform runs in one AWS account (the sandbox account) in one region, us-east-1. Two top-level CDK
stacks hold it: DataStack keeps the data and ServicesStack runs the code. There is no VPC. Every Lambda
function runs outside a network of its own. This chapter says what each stack holds, how a change gets
deployed, what the platform costs while idle, and what is not built.

## The two stacks

```text
 Sandbox account, us-east-1
 +------------------------------------------------------------------------------------------+
 |  DataStack (termination-protected, every stateful resource RETAIN)                       |
 |   raw bucket (versioned, Object Lock)     artifacts bucket      derived bucket           |
 |   warehouse bucket (Iceberg data)         Athena-results bucket Glue database `asteroidea`|
 |   Athena workgroup `asteroidea`           DynamoDB: sessions, dataset owners, labels,     |
 |   web hosting: site bucket + CloudFront        import runs                                |
 |   Cognito user pool, clients, identity pool, ResearcherRole                              |
 +------------------------------------------------------------------------------------------+
 |  ServicesStack (stateless)                                                               |
 |   +--------------------------------------+  +----------------------------------------+   |
 |   | Ingest (nested)                      |  | Api (nested)                           |   |
 |   | commit, import, import-plan, extract,|  | HTTP API with a Cognito JWT authorizer,|   |
 |   | LFP-build and derive functions;      |  | six functions behind the routes        |   |
 |   | FIFO and standard queues with        |  |                                        |   |
 |   | dead-letter queues; re-extract       |  |                                        |   |
 |   | state machine                        |  |                                        |   |
 |   +--------------------------------------+  +----------------------------------------+   |
 +------------------------------------------------------------------------------------------+
 Created outside the stacks: the fourteen Iceberg tables (make catalog-init).
```

Splitting state from code means a failed services deploy cannot roll back data, and
`make destroy-services` cannot delete researcher accounts.

DataStack exports the identity values as `asteroidea-<OutputName>`. ServicesStack imports them and
outputs them again under the names it used originally (`UserPoolId`, `SdkClientId`, `WebClientId`,
`IdentityPoolId`, `ManagedLoginBaseUrl`, `ResearcherRoleArn`, `SandboxTestClientId`), so readers of those
outputs did not change.


## DataStack (termination-protected)

Every stateful resource lives here, so a services deployment never rolls it back.

- **Raw bucket.** Versioned, with S3 Object Lock enabled and no default retention. The commit function
  sets governance-mode retention on each verified `raw/` version for `RAW_RETENTION_DAYS` days (default 1 in
  the sandbox). A bucket policy denies `s3:PutObject*` and `s3:RestoreObject` to every principal except the
  session, commit and import roles (the import role reaches `landing/` only), and denies `s3:DeleteObject*`
  to every principal except the commit role. The roles have fixed names, so the policy needs no reference
  into ServicesStack. The bucket also enforces TLS, SSE-S3 and block-public-access, and the `landing/` prefix
  expires after 3 days (1 day for noncurrent versions). Governance mode keeps a removal path if identifiable
  data ever lands in raw; compliance mode would block everyone, the root user included
  ([ADR-0005](adr/0005-deidentified-only-quarantine.md)). Object Lock protects object versions only, so the
  bucket policy is what stops a new put or a delete marker. Files committed before Object Lock was switched
  on are not locked. `tests/live/test_raw_bucket_deny.py` checks this against the deployed bucket,
  which reports `ObjectLockEnabled: Enabled`.
- **Warehouse bucket.** Holds the Iceberg data and metadata under `iceberg/`.
- **Artifacts bucket.** Holds the files and provenance records of analysis artifacts. Only the platform
  function's role can read, write or delete there, so researchers reach it through presigned URLs that
  function signs after an author check.
- **Derived bucket.** Holds three kinds of generated object: `lfp/` (chunk index and overview), `summary/`
  (dataset and per-session summaries) and `rows/` (row bundles for a re-extract, expiring after 7 days). Two
  roles write there: the LFP builder writes `lfp/` and `summary/`, the re-extract task writes `rows/`, and
  the bucket policy refuses every other write. No researcher has a direct grant; researchers read it
  through the API.
- **Athena-results bucket.** Query results under `athena/`, SSE-S3, expiring after 1 day. A result is
  reproducible from its snapshot id, so it is not kept. All researchers share the prefix.
- **Glue database `asteroidea` and the Athena workgroup `asteroidea`.** All platform and SDK queries run in
  the workgroup, which fixes the results location. The fourteen Iceberg tables are created by `make catalog-init`
  through PyIceberg's Glue catalog, because CloudFormation rejects an Iceberg `AWS::Glue::Table`
  ([ADR-0003](adr/0003-iceberg-athena-catalog.md)).
- **DynamoDB tables.** All on-demand, RETAIN. `sessions` (key `session_id`, time-to-live on `expires_at`,
  point-in-time recovery, index `by_owner`; [ADR-0010](adr/0010-dynamodb-upload-sessions.md)), the dataset
  owners, the labels, and the import runs (index `by_caller`).
- **Web hosting.** A private site bucket served only through CloudFront with Origin Access Control
  ([ADR-0007](adr/0007-s3-cloudfront-spa.md)). The distribution fixes the hosted URL, which every
  invitation and sign-in callback names, so it lives in the protected stack. The bucket also holds the
  `handout/` prefix (SDK wheel, exemplar notebook and manifest), which CloudFront never serves. The SDK wheel
  is also copied under `site/sdk/`, which CloudFront serves to anyone; the wheel holds only SDK code. No WAF and no
  custom domain are attached.
- **Cognito.** The user pool (RETAIN, domain prefix `asteroidea-id-<account>`), the clients `sdk` and `web`
  (and `sandbox-test`, only when deployed with `SANDBOX_TEST=1`), the identity pool with token role mapping,
  the groups `researchers` and `curators`, and ResearcherRole. Losing the pool would lose every researcher
  account, which is why it is here ([ADR-0004](adr/0004-cognito-researchers-identity-center-admins.md),
  [ADR-0017](adr/0017-identity-pool-data-access.md)).
- **The cost alarm is not in any stack.** The sandbox policy denies `budgets:*`. The one budget,
  `asteroidea-sandbox-monthly`, sits in the organization's management account and stops spend
  automatically at $10. The stop has never fired on real spend. See `infra/account/README.md`.


## ServicesStack (nested stacks)

ServicesStack imports the identity values from DataStack and holds two nested stacks. A table in
`infra/src/asteroidea_infra/functions.py` lists every function once (handler, role name, memory, timeout,
environment, permissions), and one builder loops over it. Python functions use the `PYTHON_3_14` runtime on
arm64.

| Function | Stack | Package | Memory | Timeout | Job |
|---|---|---|---|---|---|
| Session | Api | zip | 256 MB | 10 s | Upload sessions and artifacts |
| Platform | Api | zip | 256 MB | 10 s | `GET /get-started` and the artifact routes |
| Catalog | Api | zip | 256 MB | 20 s | Catalog reads and download links |
| Compute | Api | image | 2,048 MB | 28 s | Rates, overview and spectrogram |
| Labels | Api | zip | 256 MB | 20 s | Shared labels |
| Imports | Api | zip | 256 MB | 10 s | Curator import runs |
| Commit | Ingest | image | 2,048 MB | 120 s | Verified copy and catalog write |
| Import | Ingest | image | 1,024 MB | 900 s | One public-archive file into `landing/` |
| Import plan | Ingest | image | 1,024 MB | 300 s | Plans a web import and starts Import per file |
| Extract | Ingest | image | 2,048 MB | 120 s | Header and table extraction |
| LFP build | Ingest | image | 1,024 MB | 900 s | LFP chunk index and overview |
| Derive | Ingest | image | 2,048 MB | 300 s | Per-asset task of the re-extract state machine |

Memory and timeouts are read from `functions.py` and `timing.py`. The API timeouts
stay under the HTTP API's 30-second integration limit. The commit, extract and LFP-build functions each read
one queue with batch size 1. Commit and extract use FIFO queues with one message group, each with a FIFO
dead-letter queue, so one function at a time writes the catalog; the LFP build uses a standard queue and its
own dead-letter queue. Queue visibility is six times the function timeout. The re-extract state machine
(`asteroidea-reextract`) runs Derive per asset, at most four at a time, then sends one message per asset to
the extract queue.

Three container images exist: commit (also used for Import and Import plan with a different start command),
extract (also Derive) and compute (also LFP build). Each must stay under 1,200,000,000 bytes. Measured
sizes: the extract image 1,061,799,673 B and the compute image 1,063,708,994 B.
The commit image carries no harvester library (h5py, remfile, pynwb), and a test pins that.

The Api stack is an HTTP API with a Cognito JWT authorizer whose audience is the `sdk`, `web` and (when
deployed) `sandbox-test` clients. Its CORS rule allows `GET`, `POST`, `PATCH` and `DELETE` from the hosted
origin and the local dev server. The route list and each route's rules are in the [API reference](api.md).

Lambda limits that shape the extractor: 250 MB unzipped for a zip package, 10 GB for a container image,
900 s timeout and 10,240 MB of memory ([AWS Lambda limits](https://docs.aws.amazon.com/lambda/latest/dg/gettingstarted-limits.html)).

A stack must stay far below CloudFormation's limit of 500 resources. A test fails a template over 300
resources, 150 outputs, 150 parameters or 800,000 bytes. A deployed function never changes its construct id
or its stack, because the fixed role name would then block the replacement. New functions that would pass
the budget go into a new nested stack, in a labelled infrastructure change with a deploy.


## How a change is deployed

Offline commands (`make synth`, `make check-full`, the tests) need no account. Live commands load
`~/.config/asteroidea/sandbox.env` and refuse, with exit 4, unless the profile and account are set.


1. **Check the machine.** `make doctor` names each missing tool. A deploy builds container images, so
   `docker info` must succeed. `make deploy` runs the same check first: with the daemon down it prints a
   refusal, touches no stack and exits 3 (`make gate-deploy-docker` tests this offline).
2. **Read the diff.** `make diff SANDBOX_TEST=1` is live. For a code-only change it lists `Code`, `Handler`
   and `ImageConfig` changes and no replacement.
3. **Publish the web app first when the change alters API error bodies.** `make web-publish` builds the web
   app and the SDK wheel with the deployed ids and uploads them. The new web app and SDK read both the old
   and the new error body, so they go out before the Lambdas change. Run `make live-web-smoke` after each
   publish. `web-publish` refuses first if `web/public/index.json` carries a different schema number than
   the code.
4. **Deploy.** `make deploy SANDBOX_TEST=1`. `STACKS=<names>` limits it, `RAW_RETENTION_DAYS=<n>` sets the
   Object Lock retention (default 1), and `COLD_NONCE=<n>` forces a cold start for the next call. Every
   deploy passes `SANDBOX_TEST=1`: the deployed DataStack exports the `sandbox-test` client id, ServicesStack
   imports it, and a deploy without the flag would delete an export that is still in use. `make deploy`
   reads the DataStack outputs first and refuses, naming the export, when the flag is missing.
5. **Deploy in order when Object Lock or the identity values change.** `make deploy STACKS=DataStack`, then
   read the raw bucket's Object Lock setting, then deploy ServicesStack. CloudFormation enabled Object Lock
   on the existing versioned bucket without replacing it.
6. **Run the live checks.** Run web-smoke, the hosted-setup and research-app passes, the
   analysis run, route timing, the live imports test and one re-extract. These are the `live-*` targets and
   `make reextract`; each writes a report under `~/asteroidea-*/<run-id>/`.

### What gates a change before it merges

`make check` is the daily gate and `make check-full` adds synth, cdk-nag and the stack assertions. Two more
gates guard the deployed shape.

- **`make gate-templates`** synthesises `origin/main` and the branch, each plain and with `sandbox-test`,
  and fails on any template difference except asset hashes. A refactor that must change nothing deployed has
  to pass it. A change to infrastructure on purpose goes in its own labelled change and replaces
  `infra/template-diff.expected` with the exact `DIFF` lines the gate prints. At the base of this chapter
  that file lists eight lines: the entry-module renames (platform to get-started, session to uploads, import
  to import-file, and the derive handler). The gate reads the file only when the branch changes it against its
  base, so the next change need not touch it.


## Idle cost

"Idle" means the platform is deployed and holds data but nobody uses it. The prices are list prices from
AWS pricing pages, several for a region other than us-east-1, so read them as orders of magnitude.

| Resource | Idle charge |
|---|---|
| Lambda, API Gateway | $0 when not called; billed per request and per GB-second |
| Athena | $0; $5 per TB scanned |
| Glue Data Catalog | $0 for the first million objects and requests |
| S3 buckets | Storage only, in proportion to the data held |
| DynamoDB on-demand | Storage only, $0.25 per GB-month |
| ECR (container images) | Storage only at $0.10 per GB-month; the commit image is about 0.3 GB compressed, so about $0.03 a month; not measured |
| Cognito | $0 under 10,000 monthly active users |
| CloudFront | $0 on the Free flat-rate plan (1 million requests, 100 GB) |
| AWS Budgets alarm (management account) | $0 for alert-only budgets |
| NAT gateway (not built) | About $33 a month per gateway before data |

With no VPC, the compute and service floor is about $0 a month, and what remains is storage that grows
with the data. Nothing here has been checked against a bill: the first full month's bill is read at the
start of the month after the sandbox has been up for one month. If a VPC with a NAT gateway in each of two
Availability Zones ever arrives, it adds about $66 a month. The ceiling and its derivation are in
[Quality requirements](10-quality.md#idle-monthly-cost-ceiling-q5).


## Architecture diagrams

The figures below are generated by `make architecture` from the constants the stacks are built from: the
function table, the route table, the Iceberg schema and a small hand-written edge model. Do not edit them by
hand; a test fails when one differs from what the generator renders. They are simplified, not
resource-exact: resources are grouped by kind with a bullet per member, and each read a box makes is listed
in it rather than drawn. A synth-marked test checks every edge against the IAM grants, event sources and
state machine of the synthesized stacks.


![System overview: callers, edge and identity, API functions, storage, the ingest pipeline and the admin and archive sources, with the main data-flow edges](architecture/overview.svg)

### Curator import

A curator posts `/imports` from the web app, or runs `make import`; the planner lists the archive and invokes
one import per asset. Each file lands in `landing/` and runs through commit, extract and the LFP build.

![Curator import: the import planner, the import and the commit, extract and LFP chain lit and numbered](architecture/curator-import.svg)

### Researcher upload

A researcher signs in, creates a session and puts the file to a presigned `landing/` URL. Finalize queues
the commit, and the same commit, extract and LFP chain as an import follows.

![Researcher upload: the sessions API, the presigned upload and the commit chain lit and numbered](architecture/researcher-upload.svg)

### SDK analysis

The SDK trades its sign-in for read-only credentials through the identity pool and reads `iceberg/` and
`raw/` directly, or through Athena. Catalog questions go through the HTTP API.

![SDK analysis: the identity pool, the researcher role and its reads lit and numbered](architecture/sdk-analysis.svg)

### Web browse and compute

CloudFront serves the web app from `site/`, and the app calls the catalog and compute routes with the id
token. Those functions query Athena and read `raw/`, `lfp/`, `summary/` and the labels.

![Web browse and compute: CloudFront, the catalog and compute functions and their reads lit and numbered](architecture/web-browse-compute.svg)

### Artifacts and provenance

The SDK posts `/artifacts`, puts the result to a presigned `artifacts/` URL and finalizes it, which writes
the `index/` entry. `GET /artifacts` reads the index back.

![Artifacts and provenance: the platform function and the artifacts bucket lit and numbered](architecture/artifacts-provenance.svg)

### Re-extract

`make reextract` starts the state machine, which runs derive for each session to rebuild `rows/` and then
sends the extract pointer. A failed session is recorded in the sessions table.

![Re-extract: the state machine, derive and extract lit and numbered](architecture/reextract.svg)


## Not built

- **BaseStack and the VPC.** The design is a VPC over 2 Availability Zones with public and
  private-with-egress subnets, gateway endpoints for S3 and DynamoDB, and no NAT gateway until a workload
  needs one ([ADR-0011](adr/0011-vpc-nat-on-demand.md)). No component needs a VPC:
  `app.py` creates DataStack and ServicesStack only.
- **The quarantine bucket.** Planned as admin-only with a 7-day expiry ([ADR-0005](adr/0005-deidentified-only-quarantine.md)).
- **Multi-account separation.** A production account is an operations decision for later work.
- **Alarms and CI.** Nothing alerts on a failing Lambda, and the gates run by hand.

## History

The stack split is [ADR-0002](adr/0002-stateful-stateless-stack-split.md), still Provisional because the
rollback test of nested stacks has not run.

