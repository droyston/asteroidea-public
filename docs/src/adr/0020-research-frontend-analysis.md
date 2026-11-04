# ADR-0020: Research frontend: one analysis implementation, server-side compute, Lambda now

Status: Accepted
Date: 2026-10-26

## Context and Problem Statement

The web app and the SDK should both let a researcher look at the ingested data: how often each unit fires around the experiment's events, spectrograms of the local field potential (LFP), event periods that the experiment provides and the researcher adds, and 3D MRI slices. Two questions follow. Where does the arithmetic run, and how do the web app and the SDK avoid computing the same result in two ways? And what is built now, given that a Lambda call is capped at 30 seconds behind API Gateway and the LFP files are 0.5 to 1 GB with gzip chunks one or two channels wide?

## Considered Options

- Hybrid (chosen): the browser draws, one Python module does the arithmetic, and a Lambda runs that module next to the files.
- Browser-native analysis in JavaScript or WASM, or Pyodide (rejected): it needs NWB reads in the browser and a second implementation of every function.
- Server for everything, including a long-job tier now (rejected): no VPC exists, so every container tier (Batch, ECS, Fargate) needs a decision on a customer VPC first.
- A per-request spectrogram under a window cap: superseded after the LFP read-cost measurement.

## Decision Outcome

Chosen option: the hybrid, with these parts.

- **One analysis implementation.** `sdk/src/asteroidea/analysis.py` (polars and numpy) holds rates around events, the z-score and the spectrogram. The SDK exports it. The compute Lambda's image copies the same files, and a Docker test compares each copied file's sha256 with `sdk/src/asteroidea/<file>`; an allowed-imports test keeps the module free of anything the image lacks. The exemplar notebook calls the same functions and keeps its pandas cell as an independent cross-check. `ingest/src/asteroidea_ingest/compute_api.py` only fetches inputs and encodes outputs.
- **Spike data in two places.** The extractor writes spike times to the `unit_spikes` catalog table at ingest. For a file not yet re-extracted, or when a unit has no rows, the Lambda reads the NWB file (`source=file`). `make reextract` replaces a file's rows table by table, so every extractor change can be applied to old data.
- **Routes (API Gateway, JWT authorizer).** `/compute/rates` (align to `intervals:`, `frames:` scene cuts, `events:` on the same BIDS session, `label:` and `label_name:`), `/compute/overview`, `/compute/spectrogram`, `/files/{asset_id}/labels`, `/imports`. A response is JSON with `allow_nan=False` and at most 5 MiB; a larger body is 413.
- **Spectrograms are designed for, not squeezed under a cap.** At ingest a builder Lambda (own queue, up to 900 s) stores a chunk-offset index per NWB file and a coarse whole-recording overview per LFP series (1 s steps, 64 log-spaced bands, float16) in a platform-written derived store. The LFP tab opens on the overview. A zoomed window is computed on demand from exact byte ranges read through the index, with no h5py open. The window limit is what one response can carry: 1,200 s, derived from the 5 MiB body bound (4,036 B per window second plus 43,008 B fixed).
- **Compute tier: Lambda now.** No container tier and no VPC are built. Re-extraction fans out with Step Functions over derive Lambdas that write row bundles to `rows/` in the derived store (a second platform writer), then one pointer message per asset goes to the extract FIFO, which commits serially. There is no asynchronous job route for researchers. Batch on Fargate is recorded as the tier for jobs over 15 minutes, to be built together with a decision on the VPC.
- **Compute image.** `ingest/Dockerfile.compute` installs the lock without `pyiceberg` and `pyarrow`, because nothing on the compute import path uses them. The image is 1,063,708,994 B against `IMAGE_MAX_BYTES` 1,200,000,000; before the fix it was 1,234,924,749 B. The extract and commit images are unchanged in dependencies.
- **Zip Lambdas.** A zip-packaged Lambda may import only the standard library, the runtime's boto3 and `asteroidea_ingest`; `infra/tests/test_api_bundle.py` enforces it after `ImportsFunction` failed on `pyiceberg`. Doubles stay doubles in the catalog.
- **Order.** Web import came last, after the live sweep.

### Next hosting tier

Not built. Each row names the use case that forces it, so the next step starts from the force and not from the technology.

| Tier | Use case that forces it |
|---|---|
| Lambda function URL with response streaming | a result over 5 MB: full-recording spectrograms, many-unit rasters |
| COOP/COEP headers (CloudFront response-headers policy) | in-browser WASM needing `SharedArrayBuffer` (for example DuckDB-WASM threads, a WASM viewer) |
| Job-tier runtime (queued, longer than 30 s) | batch spike sorting, cross-session analyses, re-extracting a whole archive faster than one FIFO writer |
| GPU or Fargate | FreeSurfer-class surface reconstruction and coregistration |

The second row has no forcing case yet: NiiVue 0.69.0 ran with `crossOriginIsolated` false throughout the viewer measurement.

### Consequences

- Good: the browser and the SDK cannot disagree about a rate or a spectrogram; one test per function covers both.
- Good: a zoomed 60 s window costs 6 requests and 0.47 MB instead of the 17 to 22 MiB file open that the LFP read-cost measurement found.
- Good: no VPC, NAT or container service is added.
- Bad: ingest now writes and keeps derived data (index, overview, row bundles), and a re-extract must rebuild it.
- Bad: a re-extract is not atomic across tables; a reader can see new units beside old intervals until the run ends.
- Bad: the compute image is 1.06 GB, 89 percent of the cap; polars and numpy arrive in a second image.
- Bad: a result over 5 MiB, or a job over 30 seconds, has no route until a row of the table above is built.
- Measurements that refined this decision: LFP read cost, the NiiVue viewer, the compute tier and Athena precision.

## Pros and Cons of the Options

### Hybrid, one module

- Good: one implementation; no NWB reads in the browser.
- Bad: each analysis needs an API route and a Lambda budget.

### Browser-native analysis

- Good: no compute bill.
- Bad: NWB reads in the browser, a second implementation, and large downloads.

### Server tier for everything now

- Good: no 30-second ceiling.
- Bad: needs a VPC decision before any code; none of the four use cases needs it.

## More Information

- Related: [ADR-0003](0003-iceberg-athena-catalog.md), [ADR-0004](0004-cognito-researchers-identity-center-admins.md), [ADR-0007](0007-s3-cloudfront-spa.md), [ADR-0017](0017-identity-pool-data-access.md), [ADR-0018](0018-artifact-lake-boundary.md), [ADR-0019](0019-dataset-browser.md).

## Current state

The research frontend and the SDK share one analysis module (`sdk/src/asteroidea/analysis.py`), and heavy compute runs in Lambda functions behind API routes, not in the browser. The reason is one implementation to trust and a response small enough for one API call. It holds: the compute Lambda image copies the SDK analysis files, and a test compares them byte for byte. Nothing replaced it.

## Update (2026-10): browser-side inspection

The browser may now compute over data the API has returned, for interactive inspection: flexible axes, filtering, scaling and plotting one value against another. This narrows the earlier rule that the web app does no analysis arithmetic. It does not move saved results: a result with provenance still comes only from the shared analysis module, and the compute Lambda functions and the byte-for-byte copy of the analysis files stay as decided above.

