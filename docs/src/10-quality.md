# Quality requirements

This chapter turns the quality goals of the [introduction](01-introduction.md) into scenarios that can be
measured, and records what was measured. The numbers in the scenario table are targets. The result sections
that follow say what was measured, met or missed, with its margin. Scenarios 1 to 4, 7 and 9
are measured and met, scenarios 1, 2 and 9 in part (the table says which part). Scenario 5 (the bill) is not
measured. Scenarios 6 and 8 are not built.

## Quality goals, ranked

1. Raw-data integrity and immutability.
2. Reproducible queries.
3. Low idle cost.
4. De-identified-only posture.
5. Operable by one developer.
6. A researcher is productive within the first hour.


## Quality tree

```text
Quality
├── Integrity
│   ├── Q1  Raw objects cannot be overwritten or deleted by researchers
│   └── Q2  Every accepted object has a verified checksum
├── Reproducibility
│   ├── Q3  A query re-run at a recorded snapshot returns the same rows
│   └── Q4  A finalized record becomes queryable within a stated time
├── Cost
│   └── Q5  Idle monthly cost stays under a stated ceiling
├── Privacy posture
│   └── Q6  Files that fail the de-identification check are isolated and expire
├── Operability
│   └── Q7  One developer can gate and deploy from a clean checkout
└── Researcher experience
    ├── Q8  A 5 GB upload survives a dropped connection
    └── Q9  The first-hour script runs end to end
```

## Scenarios

| Id | Stimulus | Expected response | Target | Status |
|---|---|---|---|---|
| Q1 | A researcher's credentials try to overwrite or delete an object in the raw bucket, or write to a quarantine bucket. | Access denied; the object is unchanged. | 0 successful writes across every researcher role. | Met for the raw bucket. The quarantine half cannot run: no quarantine bucket exists. |
| Q2 | An upload arrives whose CRC64NVME checksum does not match the client's manifest. | Finalize rejects the asset; no catalog row is written. | 100% of catalog rows carry a verified checksum. | Met for a single PUT. A multipart upload is not built. |
| Q3 | A researcher re-runs a query, citing the snapshot id from the first run, after new uploads landed. | The same rows come back. | Identical result sets in 100% of re-runs. | Met. |
| Q4 | An upload session is finalized successfully. | Its record is visible to Athena and to the SDK. | Queryable within 60 s of finalize returning success. | Met, with a thin margin on a cold start. |
| Q5 | The platform is deployed and nobody uses it for a month. | The bill stays under the ceiling. | Under $10 a month (see the ceiling below). | Not measured: the first full month's bill is unread. |
| Q6 | A file passes the SDK's header check but fails the server-side check. | The file moves to an admin-only quarantine and expires after 7 days. | No quarantined object older than 7 days plus the lifecycle run; 0 researcher reads. | Not built. |
| Q7 | A developer with a fresh clone runs the gate and a deploy. | `make check` passes; one command deploys. | `make check` within 60 s with a warm cache. | Met. |
| Q8 | A 5 GB upload loses its connection halfway. | The SDK resumes and re-sends at most one part per interruption. | The upload completes with a matching checksum. | Not built: no multipart upload. |
| Q9 | A new researcher with an account follows the first-hour script below. | Each step works with the documented command and no help. | All steps in under 60 minutes. | Met for steps 1 to 5. Step 6 (artifact) is built and exercised by the headless runs. |



## Idle monthly cost ceiling (Q5)

**Target: under $10 a month with no NAT gateway. If a VPC with one NAT gateway in each of 2 Availability
Zones is ever built, under $76 a month.** Both exclude storage that grows with the data. Neither has been
checked against a bill.

| Component | Idle cost |
|---|---|
| Lambda, API Gateway, DynamoDB on-demand, Athena | $0 when unused; billed per request, per query or per GB stored (DynamoDB $0.25 per GB-month; Athena $5 per TB scanned) |
| Glue Data Catalog | $0 for the first 1 million objects and 1 million requests |
| Cognito user pool | $0 up to 10,000 monthly active users on the free tier (50 for federated users) |
| CloudFront | $0 on the Free flat-rate plan (1 million requests, 100 GB) |
| S3 and DynamoDB gateway endpoints | $0 |
| VPC with 0 NAT gateways | $0 |
| NAT gateway, if enabled | About $33 a month each before data ($0.045 per hour plus $0.045 per GB) |
| Interface endpoints (not planned) | About $7.30 a month per AZ each |

The sum with no NAT is $0 from the components above. The $10 ceiling leaves a margin for small fixed
charges that the list prices above do not cover, such as log storage and encryption keys. With two NAT gateways the
sum is 2 x $33 = $66, plus the same margin, so $76. Storage is excluded because it scales with the data. The
S3 Tables storage rate, for reference, is $0.0265 per GB-month.


## First-hour script (Q9)

A new researcher, given an account by an administrator, runs these steps in order on their own machine.

1. **Install the SDK** into a Python 3.14 environment (the package name is `asteroidea`; its Python floor is
   open, see [ADR-0015](adr/0015-python-3-14.md)). The hosted app's Get started page gives the
   `asteroidea-setup` command, which installs the wheel and writes `platform.json`. 
2. **Sign in** with one call, `asteroidea.sign_in()`. On a machine without a browser the SDK prints a login
   URL; the researcher opens it elsewhere and pastes the result back
   ([ADR-0009](adr/0009-sdk-copy-paste-login.md)). Credentials for reading the lake follow automatically
   ([ADR-0017](adr/0017-identity-pool-data-access.md)).
3. **Upload a sample** with one call: `ds, path = asteroidea.write_sample("sample")`, then
   `asteroidea.upload(path, dataset=ds)`. It sends one synthetic 3 MiB T1w file, opens an upload session,
   uploads the file and finalizes it, with no header check
   ([ADR-0010](adr/0010-dynamodb-upload-sessions.md), [ADR-0016](adr/0016-presigned-uploads.md)).
4. **Query the corpus** for the sample's scans, for example
   `asteroidea.query(datatype="anat", subject="01")`. The result gives file references and the snapshot id.
5. **Read a file lazily**, loading only the slice that is needed:
   `asteroidea.open_image(asset_id).dataobj[:, :, 48]` fetched 33,120 bytes of a 3,146,080-byte file.
6. **Write an analysis artifact** with provenance links to the inputs of steps 4 and 5
   ([ADR-0018](adr/0018-artifact-lake-boundary.md)).

Target: all six steps in under 60 minutes, with every error message saying what to do next (see the SDK
rules in [Crosscutting concepts](08-crosscutting.md#sdk-design-principles)). The guided command measured
steps 1 to 5.

### Q9 result: met for steps 1 to 5

`make acceptance` (now `make live-first-hour`) runs account creation, SDK install, managed-login sign-in,
upload, the snapshot-pinned query, the one-slice read and the web "My uploads" check as one guided command,
and writes a report to its run directory. `HEADLESS=1` runs it with no human and labels each
substitution (an `.invalid` email, sign-in through the `sandbox-test` client, the page checked by headless
Chromium). The slice-read step has a 5 s limit with a thin margin, so the guided command retries it once.



## Results: dataset extraction and queries

Measured in the sandbox account. Figures marked "live" are live runs; the
rest are figures reported by the build's own checks.

| Id | Result | Margin and what stays open |
|---|---|---|
| Extraction cost | 4.4 s and 1.86 MiB fetched on the 1,257,984,064 B file (live). Limits 300 s and 64 MiB. | Local, over HTTPS. |
| Extractor image | Image 1,061,799,673 B against 1,200,000,000 B. Cold init after a push: 9,999.61 ms and 9,999.92 ms (both timeouts) and 4,817.69 ms; a non-first cold start 2,925.09 ms (live). | The first start after a push sits at the 10 s cap and is accepted as a cost. |
| `sql()` first call | 5.27 s first, 0.26 s on the same tables, 1.14 s after a new commit; bound 10 s. | Binding every view first took 13.5 s to 21 s; `sql()` now binds only the named tables. |
| P-nwb | 25.6 s and 45.4 MiB in 12 requests; bounds 60 s and 64 MiB. | The analysis step measured 23.7 s (LFP) and 19.1 s (scene cuts). |
| P-gz | 3.5 s and 4.0 MiB; bounds 20 s and 32 MiB. | Analysis step 4.2 s. |
| Concurrent finalizes | 10 of 10 rows in 49 s, 0 overlaps (live). | The designed concurrency test did not run; a FIFO queue now serialises writes. |
| Real imports | First import: 44 units, 96 electrodes, 41 intervals. Then 62 real assets, 4,038,001,058 B, 102 units of which 72 amygdala; dead-letter queues empty (live). | The optional full 27.7 GB import was not run. |
| Cold query in acceptance | One FAIL at 10.7 s, then PASS at 5.2 s (live). | Variance against the 10 s limit. |
| Finalize to queryable | 17.3 s in the headless acceptance run; limit 60 s. | The queue adds poll latency. |

A guided `make analysis` run passed six of six: datasets 3.0 s; subjects
2.2 s; amygdala units 1.0 s (sub-CS41 2, sub-CS62 70); scene cuts 21.6 s (93 cuts, 29.4 MiB); LFP slice
24.3 s (45.4 MiB); fMRI volume 3.8 s (4.0 MiB). No step used a substitution.


## Results: hosted app and catalog

Measured in the sandbox.

| Check | Result |
|---|---|
| Standalone Athena query latency (from the test machine; 128 assets, 33 datasets, 20 runs per query) | p50 1.49 to 1.95 s, max 2.45 to 4.56 s, 2.8 to 67 KB scanned per query |
| Catalog through API Gateway and the catalog function (10 calls per route over 2+ minutes) | Wall p50: datasets 2.33 s, assets 1.83 s, asset 2.27 s. Wall max 3.16, 2.59 and 3.70 s. 0 cold starts in 33 invocations |
| Object Lock on an existing versioned bucket with objects | CloudFormation enabled it with no replacement. A governance delete was refused, a rollback delete and a landing delete were allowed, and a resume re-lock was allowed. The live raw bucket reads `ObjectLockEnabled: Enabled` |
| Ownership and lock, live | 5 passed. The first commit after the deploy was a cold start with Init Duration 4903.01 ms |
| Deny rules, live | 7 passed |
| Artifacts, live | 4 passed. A PUT of other bytes is refused by S3 and never commits |
| Headless install-and-notebook run | All steps PASS in 174 s. The executed notebook had 5 figures and 0 errors |
| Headless hosted-setup run | All rows PASS in 85 s |
| `make notebook-run` with the save section | All cells ran |

The catalog p50 values sit inside the 5 s target and the worst case (4.56 s) inside the 10 s limit. Not
measured: the install-and-notebook rows run by a person, the hosted-setup rows on a clean machine, and the AWS bill after
the deploys.


## Results: research web app

Measured in the sandbox from headless runs and the live test files. The budgets are those set for the
web app.

| Check | Budget | Result |
|---|---|---|
| LFP read cost on a laptop, h5py open (before the redesign) | Window warm at most 15 s | 3 of 4 series fail at every window in {10, 20, 30, 60} s; 8 to 11 s of it is the file open |
| LFP window through the index, live | Warm at most 15 s, cold at most 25 s | 60 s window: cold 3.26 s, warm p50 1.97 s, max 3.43 s; 6 requests, 470,757 B |
| LFP tab in the browser | Overview, window and pan each at most 15 s | 9.69 s, 2.61 s, 3.47 s |
| `/compute/rates` `frames:` warm, 10 calls | p50 at most 4 s, max 10 s | p50 2.79 s, max 5.05 s |
| Units rates in the browser | At most 8 s | 4.74 s |
| Image presign | At most 3 s | 3.05 s and 3.07 s (1.95 to 2.47 s in the three runs before); intermittent |
| Image volume (10.7 MB over a home network) | Judged by the person running it | 5.18 to 9.79 s |
| Label POST | At most 3 s | 4.24 s before the fix; 1.93 to 2.79 s after |
| `labels()` in a fresh venv | At most 5 s | 9.09 s, then 5.51 s (FAIL); results equal the route to 0.0 relative |
| Compute image | At most 1,200,000,000 B | 1,063,708,994 B |
| Main JavaScript, gzip | Estimate +15 KB, stop above +22.5 KB | 94.7 KB, then 95.6 KB after later changes (+0.6 KB over its base) |
| NiiVue chunk, gzip | 1.25 x measured | 314.4 KB; budget 392.5 KB |

Not measured: the rows judged by a person, the browser rows, and the AWS bill.


