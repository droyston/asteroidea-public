# Risks and technical debt

This chapter lists what could go wrong with the architecture as decided, the questions still open, and the
tensions between decisions. Each open item says its effect and where it stands. Items that
were closed are listed after the open ones.

## Open risks

### Platform and operations

| Risk | Effect | Where it stands |
|---|---|---|
| The first commit-function start after an image push is slow. | The finalize-to-queryable time (quality scenario 4) was 32.4 s for that start against 5.3 s to 8.6 s warm. Earlier cold starts took 52.3 s and 54.1 s, and Lambda's 10 s init limit was hit once (9,999.77 ms). A start in that state can approach the 60 s limit. | Open. Provisioned concurrency or a post-deploy warm-up would fix it. The module-level import stayed. |
| The extract function's first start after a push sits at the 10 s init cap. | 2 of 3 first invocations timed out in init, and Lambda re-ran it. | Accepted as a cost. |
| The first read of a slice can exceed 5 s. | It measured 1.4 s to 3.9 s in live runs. In a guided run the first attempt failed at 5.4 s and passed at 1.9 s on retry. | Open. The cause is not isolated. The guided command retries once. |
| `asteroidea.labels()` is slow in a fresh environment. | `labels()` took 7.57 s against a 5 s budget in a live check, 7.14 s of it in the SDK table read. Other runs took 9.09 s and 5.51 s. Results equal the route's to 0.0 relative difference. | Open. The SDK table read is the cost. |
| A session can stay in ACCEPTED. | If the commit function fails after the second claim, no code moves the session on. One real session is in that state, with a `raw/` object and no `assets` row. A researcher uploads again. | Open. Leases let `make reclaim` re-send a stopped session to its queue, but a copy already in `raw/` is not adopted. |
| A poison message that hard-fails stalls its FIFO group. | Every later file waits up to 1,440 s (twice the 720 s visibility) until the dead-letter queue takes it. | Mitigated: the handler never raises, and `make reclaim` prints both dead-letter depths. Both were empty after the real import. |
| Handler crashes no longer appear in the Lambda `Errors` metric. | The API returns a 500 body instead of raising, so Lambda sees a success. | Accepted. The `unhandled` log line records each crash. No alarm exists today ([Crosscutting concepts](08-crosscutting.md#error-handling)). |
| A re-extract is not atomic across tables. | A reader sees new units beside old intervals until the run ends. A failure names the tables replaced. | Accepted ([ADR-0003](adr/0003-iceberg-athena-catalog.md)). |
| The re-extract fan-out has run only offline and in small live runs. | Execution limits, serialisation and leases late in a long run are untested at scale. | Open. |
| One Iceberg snapshot per file per table. | Metadata grows. `assets` had 56 manifests and a warm scan took 2.0 s against 1.0 s. | Open. Table maintenance is a deliberate scope cut. |
| The compute image is 89 percent of its cap. | The next dependency meets the 1,200,000,000 B cap again. | Known. |
| The raw bucket's older files are unlocked. | Files committed before Object Lock was switched on have no retention. | Known limit; no back-fill. |
| Sandbox retention is 1 day. | A production deployment needs a longer `RAW_RETENTION_DAYS`, and the removal runbook for identifiable data needs a temporary role. | Open until a non-sandbox deployment. |
| The budget stop has never fired on real spend, and `cdk destroy` under the stop policy is untested. | The $10 cap may not stop spend as intended. | Open. The checks are in `infra/account/README.md`. |
| The AWS bill after the deploys is unread. | The cost estimates are unconfirmed. | Open. |
| No CI and no alarms. | The gates run by hand and nothing pages on a failing Lambda. | Open. |
| The hosted URL is the CloudFront default domain. | Replacing the distribution would break every invitation and sign-in callback. It sits in the protected stack. | Open. A custom domain is a later piece of work. |
| Sandbox test users remain. | Test users, including group-less ones, created by earlier live checks stay in the user pool. | Clean up before real researchers join. |


### Data and access

| Risk | Effect | Where it stands |
|---|---|---|
| No quarantine, no PHI check and no DICOM handling are built. | Only de-identified public data is imported. A file with identifiable data would not be caught. | Open ([ADR-0005](adr/0005-deidentified-only-quarantine.md)). Server-side checks would run after the bytes are in AWS, so quarantined data could sit up to 7 days. |
| Static files are public. | The app shell and the public index are reachable without sign-in. Both hold public identifiers and public archive metadata only. | Accepted. No WAF. |
| Athena results are shared under one prefix. | Any researcher can read another's query results in `athena/`. | Open. Scoping the prefix is not done. |
| Identity-pool role scoping may be too coarse. | Researchers could be over-privileged on direct reads. | Raw-bucket writes are tested (7 of 7 denied). The quarantine half and a per-dataset scope are untested. The fallback is API-brokered reads for any path that cannot be scoped ([ADR-0017](adr/0017-identity-pool-data-access.md)). |
| Shared labels are a write path open to every researcher. | Spam. | Accepted: at most 2,000 labels per asset and edits by their author only. |
| Every researcher sees every author's email address. | A result's record and listing carry `author_email`, shown on the results pages and returned by `GET /artifacts` to every researcher. The address is also copied into each record, so it cannot be hidden later without rewriting the records. Names and notes of results are readable by everyone too. | Accepted as a design choice. The researcher guide tells a researcher not to put private text in a name or note. |
| Results and their lineage are readable by all researchers. | A researcher cannot keep a result private; a wrong `asset_id` in `sources` is accepted if the SDK is bypassed. The artifact catalog tables (lineage, staleness) are not built. | Accepted. A planned follow-up loads the same records into catalog tables ([ADR-0018](adr/0018-artifact-lake-boundary.md), research-synthesis update). |
| Analysis defaults are a screen, not an analysis. | A reader may take a screened unit as a finding. The defaults follow the paper where one gives values and convention otherwise; several paper settings are listed as not reproduced (for example the pooled HFB z-score, and the AUC rule that the paper and the code state differently). | Mitigated by the label "exploratory screen" on every route body, frame, page and notebook result. The correction is open work, not a code gate. |
| The backfill of the 10 records made before the author email was recorded is deferred. | `make artifacts-backfill DRY=1` failed 10 of 10 with AccessDenied: the artifacts bucket's `DenyArtifactAccessExceptPlatform` denies the admin role. The 10 records are visible to their authors only, are missing from the All tab and reverse lookup, and have no email. | Open: the backfill moves into PlatformFunction, or the planned record-to-catalog load covers it. `IfMatch` and `IfNoneMatch` on `PutObject` stay unverified against live S3. |
| The session SDK calls are slow. | Measured in a live run: `load_session` 61.02 s, the first save 32.03 s, route rates 3.8 to 8.6 s per event type, route tuning 5.1 to 6.7 s; the session page header took 23 to 29 s in web-smoke. | Open, a performance item; no budget is set. |
| Session-level compute has no live timing. | The 152-unit rates body (5 MiB cap) and the "All" results listing (STOP over 5 s) were measured in process with stub Athena and S3 only. `screen_unresolved` on real 000623 sessions and the real `units.location` cells are unchecked. | Open until a live run covers them. |
| A second dataset with units outside top-level `units/` is untested. | The generalisation is proved by two real layouts that both use `units/`; 001775's timestamps-based LFP is skipped by the LFP builder. | Open ([ADR-0021](adr/0021-research-synthesis.md)). |
| Catalog cells can hold NaN, and older routes emit it as invalid JSON. | A catalog route can return unparsable JSON. | Open. |
| Results over 5 MiB and jobs over 30 s have no route. | Full-recording spectrograms and batch analysis wait for another compute tier. | Accepted ([ADR-0020](adr/0020-research-frontend-analysis.md)). |
| A FAILED extraction leaves no header rows, and the SDK cannot see `extract_state`. | A researcher may see an asset with no tables. | Open. `make import` prints FAIL lines; `GET /sessions/{id}` lacks the field. |
| A publisher URL that redirects or throttles. | A DANDI file fetch can fail. DANDI streams over HTTPS because the region lock denied CopyObject. | Residual risk. |
| DuckDB downloads its extensions on first use. | The researcher's machine needs network access. | Covered by live tests only. |
| The placeholder `session_start_time` (`2019-04-01`) in NWB files. | It could be read as a recording date. | Documented in [Crosscutting concepts](08-crosscutting.md#provenance-subject-identity-and-licence). |
| The notebook link in the `GET /get-started` response expires after 1 hour. | The page shows only the install command, whose wheel link is public and does not expire. | Accepted. |
| Runs by a person are not recorded. | The install-and-notebook rows, the hosted-setup rows on a clean machine and the browser rows are deferred. Only the headless runs are measured. | Open. |
| A live pass can hide later rows behind a FAIL. | `live-research-app` and `live-web-smoke` continue past a FAIL. `live-first-hour`, `live-hosted-setup` and `live-analysis` stop at the first. | Known. |


### Tooling and process

| Risk | Effect | Where it stands |
|---|---|---|
| The ty type checker is pre-1.0 and every rule is an error. | An upgrade breaks `make check`. | ty is pinned exactly and each bump goes in its own change ([ADR-0013](adr/0013-ty-strict.md)). |
| Strict rules meet untyped scientific libraries. | Every collision becomes a question for the maintainers. | The written escape-hatch policy lists the exceptions that need no decision. |
| A developer's uv is older than the pinned range. | `make install` fails. | `required-version` names the range and `make doctor` reports the miss. |
| The downloaded mdBook binary is tampered with. | A bad binary runs on developer machines. | `make tools` checks the sha256 digest and deletes the file on mismatch. |
| Python 3.15 reaches Lambda general availability, targeted for November 2026. | [ADR-0015](adr/0015-python-3-14.md) ages. | A watch item. Bump when Lambda and aws-cdk-lib both support it. |
| One workspace Python, 3.14. | Researchers on older Pythons, often in conda environments, cannot install the SDK. | Open. The SDK's floor is undecided. |
| An unexpected template change ships by accident. | A refactor changes a deployed resource. | `make gate-templates` fails on any difference except the lines in `infra/template-diff.expected` ([Deployment view](07-deployment.md#how-a-change-is-deployed)). |


## Closed risks

- **The raw bucket had no Object Lock.** Closed: Object Lock sets governance retention per object
  after the verified copy ([ADR-0005](adr/0005-deidentified-only-quarantine.md)).
- **The Cognito user pool sat in ServicesStack with removal policy DESTROY.** Closed: the pool is
  in DataStack with RETAIN, so `make destroy-services` no longer deletes researcher accounts.
- **No curator-only group.** Closed: the `curators` group gates the import routes.
- **CORS from the hosted web app was covered by a curl preflight only.** Closed by the hosted-app runs: the
  browser smoke run reached every page with 561 PASS and 0 FAIL.
- **A Cognito domain and stack-move ordering risk.** Closed: a new domain prefix
  `asteroidea-id-<account>`, and DataStack is termination-protected.
- **The ID token as the email path for version 2 results.** Closed: the live test saved with the ID token and
  the JWT authorizer accepted it; no fallback was needed.
- **The first read of an Image missed the presign cache** (3.05 s and 3.07 s against 3 s). Intermittent and
  accepted.


## Open questions

- **S3 Tables or Glue-managed Iceberg tables.** The platform uses Glue. S3 Tables has not been evaluated.
- **Zip or container image for the extractor.** Settled in practice: container images carry the commit,
  extract and compute functions, and zips carry the API functions.
- **Concurrent finalizes.** A FIFO queue now serialises catalog writes. The designed concurrency test did not
  run.
- **Nested-stack rollback coupling.** Whether a failed update of one nested stack rolls back the others has
  not been measured ([ADR-0002](adr/0002-stateful-stateless-stack-split.md)).
- **Who converts vendor raw ephys and DICOM into NWB and BIDS.** Open. See [Crosscutting concepts](08-crosscutting.md#format-conversion-open).
- **Multipart upload and the quarantine bucket.** Not built.
- **The SDK's Python floor.** Open ([ADR-0015](adr/0015-python-3-14.md)).
- **The artifact lake's design beyond the boundary.** Open ([ADR-0018](adr/0018-artifact-lake-boundary.md)).

## Design tensions

None of these reopens a decision. Each says where two decisions pull against each other and what settles it.

### Researcher credentials for direct reads

A researcher login yields Cognito tokens, but reading S3 and Iceberg with DuckDB or Polars, or querying
Athena, needs AWS credentials. **Answered** by identity-pool credentials
([ADR-0017](adr/0017-identity-pool-data-access.md)). The roles scope tightly enough for the raw bucket
(writes to the raw bucket are denied).

### A NAT gateway with no tenant

Every function runs without a VPC, so a NAT gateway would be the whole idle cost of an otherwise zero-idle
design. **Answered** by not building a VPC until a workload needs one
([ADR-0011](adr/0011-vpc-nat-on-demand.md)). None has.

### Rollback coupling inside ServicesStack

A single root stack with nested stacks was rejected because nested stacks cannot deploy alone and stateful
data would roll back with services. The same coupling applies inside ServicesStack: a failed update of one
nested stack rolls back the other. **Open.** The measurement would cause a deliberate failure in a nested
stack. If the stacks are coupled, nothing is restructured without a recorded decision
([ADR-0002](adr/0002-stateful-stateless-stack-split.md)). The split rule in `functions.py` (a deployed
function never moves) limits the exposure.

### Mutable QC columns against immutable catalog records

The catalog holds only finalized records ([ADR-0010](adr/0010-dynamodb-upload-sessions.md)), but quality
control state is set by people after finalize. The default is a separate append-only QC table so asset
records stay immutable. **Open**; the table is not built.

### Object Lock against PHI remediation

The raw bucket uses governance mode. If a file with identifiable data passed every check and reached raw,
a principal with the bypass permission could still delete it; compliance mode would block everyone,
including the account root. Governance mode is therefore the only remediation path, and enabling Object Lock
cannot be undone. No role holds the bypass permission today. **Recorded** in
[ADR-0005](adr/0005-deidentified-only-quarantine.md).

### Who converts vendor data to BIDS and NWB

If the platform converts, multi-gigabyte conversions do not fit a Lambda. If researchers convert before
upload, that is first-hour work the platform is meant to spare them. **Open.**

### A 3.14-only SDK against conda users

Neuroscience Python is often conda-based on 3.10 to 3.12, and the SDK is the researchers' main surface.
**Open**; the floor is undecided ([ADR-0015](adr/0015-python-3-14.md)).


## Assumptions still to verify

- DuckDB 2.0 can write Iceberg tables in S3 Tables, though its documentation contradicts itself. **Not
  tested.** The platform uses PyIceberg as the only writer.
- Athena's Iceberg support covers the table version S3 Tables uses; AWS pages contradict each other on
  version 2 against version 3. **Half answered:** Athena reads a Glue version 2 table. The S3 Tables half
  waits on its evaluation.
- Resolved: cdk-nag 3 can be called from Python. `Validations.of(app).add_plugins(AwsSolutionsChecks(app))`
  works, and a public bucket fails with AwsSolutions-S2 ([Crosscutting concepts](08-crosscutting.md#policy-as-code-cdk-nag)).

## Technical debt by design

These are deliberate scope cuts, not accidents.

- No continuous integration; the gates run on the developer's machine and before each merge.
- No alarms, dashboards or backup and restore drill.
- One version per dataset name.
- No table maintenance on Iceberg (snapshot expiry, compaction).
- No back-fill of Object Lock on older raw files.
- No multipart upload, no quarantine, no VPC, no custom domain, no WAF.
- The CDK CLI runs through `make synth`, `make diff` and `make deploy`, and `make doctor` checks only its
  version.

