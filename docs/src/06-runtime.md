# Runtime view

This chapter follows the main journeys through the running system, one at a time. Each journey has
numbered steps, the code that runs each step and the ways it can fail. It is written from the code. The
[building block view](05-building-blocks.md) names the parts, the [API reference](api.md) lists every
route and the [developer guide](developers.md) says how to change them.

Three terms recur. A **session** is the record of one upload or one imported file in the DynamoDB
sessions table. A **writer** is a function that changes the catalog: the commit function and the extract
function. A **lease** is a time limit on a claim; when it passes, another run may take the work.


## Signing in

Every API call carries a Cognito token. There are two ways to get one.

**The web app** uses the authorization code flow with PKCE (a one-time proof that ties the code to the
browser that asked for it).

1. The app makes a random code verifier and its SHA-256 challenge, and a random `state` value.
2. It sends the browser to the Cognito hosted login page with the challenge.
3. The researcher signs in. Cognito sends the browser back to `/auth/callback` with a one-time code.
4. The app checks `state`, then sends the code and the verifier to the Cognito token endpoint.
5. Cognito returns ID, access and refresh tokens. The app keeps them in `sessionStorage`, never in
   `localStorage`.

The sign-in control shows only when three Vite variables are set (`web/.env.example`). Without them the
public dataset browser works and nothing is signed in.

**The SDK** cannot count on a browser on its own machine, so it uses a copy-and-paste variant of the same
flow (`sdk/src/asteroidea/_auth.py`).

1. `asteroidea.sign_in()` reads the token cache `tokens.json` in the config folder (file mode 0600,
   folder mode 0700). A valid refresh token ends the flow here.
2. Otherwise it prints a login address that carries the PKCE challenge.
3. The researcher opens the address in any browser and signs in.
4. The browser lands on a page that shows the redirect address. The researcher pastes it, or the bare
   code, into the SDK prompt. The SDK allows three tries and checks `state`.
5. The SDK exchanges the code and the verifier for tokens and writes the cache. It refreshes the tokens
   five minutes before they expire.

For direct reads, the SDK sends the ID token to the Cognito identity pool (`_creds.py`). The pool returns
temporary AWS credentials for `ResearcherRole`. A user in no group gets `NotAResearcherError`. The SDK
uses these credentials for DuckDB and PyIceberg reads and for ranged reads of the raw bucket.

**Failure branches.** A wrong password stops at step 3 on the hosted page; no code is issued. An expired
access token makes the API answer 401. The web app then refreshes the token once and repeats the call
(`web/src/auth/apiGet.ts`); a second 401, or a failed refresh, ends the session with "Your session has
ended". A revoked refresh token makes the SDK delete the cache entry and ask for a new sign-in without
AWS error text.

The user pool supports PKCE for public clients. Cognito documents no device-code grant, which is why the
SDK uses copy and paste. In the first-build check the hosted login accepted the redirect to
`http://localhost:8765/callback`; the code exchange returned all three tokens, the refresh grant returned a
new access token with the group `researchers`, the cache modes were 0600 and 0700, and the credentials
resolved to `ResearcherRole`. A forced expiry of the cached credentials gave a new
access key and the next read worked. The SDK client's refresh after a real token
expiry is not measured; a stand-in forced the expiry.


## One API request

All routes share one path through `api_common.py`. Every journey below that starts with an API call
runs these steps first.

1. **Authorize.** API Gateway checks the bearer token with a JWT authorizer. The authorizer accepts a
   token from the user pool whose audience is one of the platform's clients (SDK, web and, in the
   sandbox, the test client). A missing or invalid token gets a 401 from the gateway, and no platform
   code runs. The gateway body is `{"message": ...}`, not the platform's error body.
2. **Enter.** The gateway invokes the function that `routes.py` names for the route. The function's
   `handle` calls `serve` with the event and its `DISPATCH` map (route key to method name).
3. **Find the route.** `serve` looks the route key up in `routes.ROUTES`. An unknown key is a 404
   "no such route".
4. **Check the group.** The route row names a Cognito group, taken from the token's `cognito:groups`
   claim. A caller outside it gets 403 `forbidden`. A curator is also a researcher.
5. **Run the method.** `serve` builds a `Request` (the route, the caller's `sub` and email, the groups,
   path and query parameters, the body, the clock and the time left in the Lambda) and calls the method
   with it and a `Timing` object.
6. **Answer.** The method returns the response. If it raises an `ApiError`, `serve` turns it into the
   standard body. Any other exception is logged as one `unhandled` JSON line and answered with 500
   `internal_error` and a sentence that names the request id.
7. **Log the timing.** In every case `serve` writes one JSON line (`timing_log.py`): the route name,
   the phases the method timed in milliseconds, `total_ms`, whether the execution environment was cold,
   the status and the request id. The line never holds a token, a user id or a URL. An optional `asset_id`
   and `cache` ("hit" or "miss") are added when the route has them.

The standard refusal body is `{"error": code, "detail": sentence}`. The code is short and stable; a
client branches on it. The sentence is for a person. Ten statuses have a default code (400
`invalid_request`, 403 `forbidden`, 404 `not_found`, 409 `conflict`, 410 `gone`, 413 `too_large`, 422
`unprocessable`, 500 `internal_error`, 502 `upstream_failed`, 503 `unavailable`), and a refusal may carry
a more specific one such as `dataset_not_yours`. The codes of each route are in the
[API reference](api.md#error-body).

The API functions have timeouts under the gateway's 30 second limit: 10 s for the session, platform and
imports functions, 20 s for the catalog and labels functions and 28 s for compute (`timing.py`). A route
that runs past 30 s gets a 503 `{"message": ...}` from the gateway.


## Upload a file

Only the manifest JSON goes through API Gateway, which caps a payload at 10 MB. The bytes go straight to
S3 on a presigned URL. S3 accepts a single PUT up to 5 GB; a multipart upload would use parts of 5 MiB to
5 GiB and at most 10,000 parts, and is not built. A URL signed with role credentials lasts no longer than
the signing session, and S3 checks expiry on every request. For a multipart upload a full-object checksum
exists only for CRC64NVME, CRC32 and CRC32C; SHA-256 is composite only, so the platform uses CRC64NVME.

The SDK sends one file to the platform and waits for its catalog row. `asteroidea.upload` sends one file.
`asteroidea.upload_dataset` sends a folder, tags every file with one `upload_id` and may send the
dataset's title, licence and description. The code is `sdk/src/asteroidea/_upload.py` and
`ingest/src/asteroidea_ingest/uploads_api.py`.

```text
SDK              SessionFunction     sessions table    raw bucket          CommitFunction    catalog
 |--1 POST /sessions ->|--- OPEN --------->|                  |                  |             |
 |<-- id + signed URL -|                   |                  |                  |             |
 |--2 PUT -------------------------------------------------> landing/            |             |
 |--3 POST finalize --->|-- FINALIZING --->|--- commit queue ----------------->  |             |
 |                      |                  |  4 verify, 5 claim, copy to raw/    |             |
 |                      |                  |                  |  6 append rows -------------->|
 |--7 GET /sessions/id -|<- COMMITTED + asset id + snapshot id|                  |             |
```

1. **Create.** The SDK sends `POST /sessions` with `{dataset, path, size, crc64nvme}`. The function
   checks the shape, then asks the dataset-owners table whether the dataset name is free or already the
   caller's (`owners.py`). It refuses a key that already exists in `raw/`. It writes the session in state
   OPEN, valid for 48 hours, and returns the session id, the expiry and one presigned PUT URL for
   `landing/<session id>/<path>`. The URL is valid for one hour and is signed with the file's CRC64NVME
   checksum.
2. **Transfer.** The SDK sends the bytes in one PUT. S3 refuses a body whose checksum differs from the one
   signed. A file may be at most 5 GiB.
3. **Finalize.** The SDK calls `POST /sessions/{id}/finalize`. A conditional update moves the session
   from OPEN to FINALIZING, and only the call that wins sends a message `{session_id, finalize_token}` to
   the commit queue. The answer is 202. A repeated call sends nothing and reports the current state.
4. **Verify.** The commit queue delivers the message to `CommitFunction` (`commit.py`). The function
   claims the session only if its state and token match and no live lease holds it. It reads the stored
   checksum and size of the landing object. A missing object or a different checksum or size sets the
   session INCOMPLETE; nothing is copied.
5. **Claim and copy.** A second conditional update, FINALIZING to ACCEPTED, comes before any write. The
   function copies the verified landing version to `raw/<dataset>/<path>`, checks the copy's checksum
   (a mismatch deletes the copy and sets INCOMPLETE) and sets Object Lock retention on that version. The
   asset id is a hash of the raw key and version id (`ids.py`).
6. **Append.** `CatalogWriter.append_if_absent` adds one row to `assets` and one to `datasets`, each
   only if its key is not there yet. A failed Iceberg commit is retried up to three times. The session
   becomes COMMITTED with `asset_id`, `snapshot_id` and `extract_state` PENDING, and a message goes to
   the extract queue.
7. **Report.** The SDK polls `GET /sessions/{id}` every second. It returns the asset id and snapshot id
   once the state is COMMITTED. If that takes more than 60 s after the 202, the SDK raises
   `FinalizeTimeoutError`. `GET /uploads` lists the caller's own sessions, newest first, at most 50.

**Failure branches.**

- A wrong checksum or a missing object sets INCOMPLETE. The error text is on the session, and a later
  finalize answers 409 `session_incomplete`.
- A dataset owned by another researcher gets 403 `dataset_not_yours` at step 1. A target that exists gets
  409 `raw_key_exists`. An expired session gets 410 `session_expired`.
- A commit that stops after the claim leaves the session in ACCEPTED with a lease of 150 s. When the lease
  passes, `make reclaim` re-sends the message. The new run adopts a raw copy that is already complete or
  copies again; the appends are keyed, so no row doubles.
- Each queue delivery counts as an attempt. After three attempts the guard in `queues.py` stops the
  session and the handler returns, so one bad message never blocks the queue (see
  [two writers, one message group each](#extract-a-file)).

In the first-build check, a wrong client checksum left the session INCOMPLETE, a `ResearcherRole`
`HeadObject` on the `raw/` key returned 404 and no `assets` row existed. `make acceptance` runs this
slice.


## Import a public dataset

A curator copies files from DANDI or OpenNeuro into the platform. The files take the same commit path as
an upload. There are two entry points, and both plan the run with `import_plan.py`.

**From the web app.** A curator opens Discover, then Imports.

1. `POST /imports` with `{source, include}`. The source is `dandi:<id>@<version>` or
   `openneuro:<id>@<version>`; `include` is up to 20 globs. `ImportsFunction` (`imports_api.py`) writes a
   run in state PLANNING to the import-runs table and invokes `ImportPlanFunction` asynchronously. It
   answers 202 with the run id. Planning can take longer than the gateway allows, so the planner runs
   after the answer.
2. The planner lists the archive, reads the dataset's title and licence before it invokes anything, and
   chooses a mode per file from the source bucket's region: `s3` (same region, a server-side copy) or
   `https` (another region or a small ancillary file, a streamed anonymous GET). It skips files that are
   already in the catalog.
3. The planner refuses the whole run, with the reason on the run, when the dataset has no title or
   licence, when a second version of an imported dataset is asked for, when the run holds more than 50
   files, or when nothing matches. Otherwise it invokes `ImportFunction` once per file, asynchronously,
   and records each file's session id. The run becomes RUNNING.
4. `ImportFunction` (`import_file.py`) derives the session id from the dataset, the path and the source
   version, so a repeated delivery finds the same session. It claims the dataset for the administrator
   (a dataset owned by a researcher is refused), moves the bytes to `landing/`, checks the source's
   SHA-256 and finalizes the session. From there the file takes steps 4 to 6 of [Upload a file](#upload-a-file).
5. `GET /imports/{run_id}` reads the run and, for each file, the outcome from its session. A run whose
   files all have an outcome reads DONE. A run still PLANNING or RUNNING after its lease reads FAILED
   "the planner did not finish". Another caller's run reads as 404.

**From the command line.** `make import SOURCE=... [INCLUDE=...] [EXTRA=...]` runs the same planning
core on the administrator's machine (`curate.py`). It writes a manifest, invokes `ImportFunction` for
each file (at most four in flight) and polls each session until COMMITTED and then extracted, for up to
30 minutes. It prints PASS or FAIL per file. Exit 0 means every file passed; 1 means a failure; 2 means
an unknown archive or a second dataset version. `EXTRA` adds small ancillary files, such as scene cuts, to
the plan.

**Failure branches.** A copy that does not match the source digest sets the session INCOMPLETE and
`make import` prints FAIL. A planner that crashes leaves the run PLANNING until its lease passes. Each file
has its own session, so one file that fails does not stop the others of the run.


## Extract a file

After a commit, the extract function reads the file and writes its detail tables. The commit queue and the
extract queue are FIFO queues, and each uses one message group. One message in a group runs at a time, so
each catalog writer runs alone.

1. The commit function sends `{session_id}` to the extract queue. `ExtractFunction`
   (`extract/handler.py`) claims the session: its state must be COMMITTED and `extract_state` PENDING
   (or EXTRACTING with a past lease). The claim sets EXTRACTING.
2. The function picks an extractor from the path (`extract/bids.py`): `nwb`, `nifti`, a BIDS JSON
   sidecar, `participants.tsv` or `*_events.tsv`. Any other file is marked SKIPPED and has only its
   `assets` row.
3. The extractor reads the raw object at its pinned version, with ranged reads through a presigned URL
   for NWB (`pynwb` with `remfile`) and with `nibabel` for NIfTI. It does not download the file.
4. It appends rows for each table that file type fills, each only if its key is absent, and
   `asset_headers` last. An NWB file fills `series`, `electrodes`, `units`, `intervals`, `unit_spikes`,
   `subjects` and `sessions`; an `_events.tsv` file fills `events`; `participants.tsv` fills
   `subjects`; a NIfTI file or a JSON sidecar fills `asset_headers` only. A resumed run skips tables
   that are done.
5. For an NWB file, the function then sends `{asset_id, dataset, s3_key, s3_version_id}` to the LFP build
   queue. A failed send is logged and does not change the extract result.
6. The session ends EXTRACTED, SKIPPED or FAILED.

**Failure branches.** A transient error (a lost connection, a timeout) is retried inside the same
invocation, twice, after 2 s and 5 s. A deterministic error (a malformed file, a denied request) sets
FAILED at once and records the error class and message on the session as `extract_error`. The handler
never raises, so a poison message does not hold the group. A FAILED extraction leaves the asset with its
`assets` row and no detail rows. `make reclaim` re-sends sessions whose lease has passed and prints both
dead-letter queue depths; `FORCE=1 SESSION=<id>` re-runs a FAILED extraction on purpose.


## Re-extract existing files

When an extractor improves, `make reextract` rewrites the detail tables of files already in the catalog.
It runs a Step Functions state machine, because many files must run in parallel while the catalog is
still written by one writer. The state machine is built in `reextract_flow.py` and the command is
`reextract.py`.

1. The command lists the `assets` rows that match `DATASET`, `ASSET` and `FORMATS`. It writes one
   tracking item per asset in the sessions table (`reextract:<run id>:<asset id>`, state QUEUED, lease
   600 s, expiry in 7 days). An asset whose format has no extractor gets a SKIP line. An asset that
   another run holds with a live lease is refused, and the line names the run; `FORCE=1 ASSET=<id>`
   expires that lease first.
2. The command starts one execution of the `asteroidea-reextract` state machine, or one per 500 assets,
   and polls every 10 s. It never invokes a function and never sends to a queue.
3. The state machine's Map state runs `DeriveFunction` for each asset, four at a time
   (`extract/derive.py`). The function runs the same extractor as the normal path, but its writer only
   records each catalog operation. It writes one gzip bundle and then a manifest under
   `rows/<asset id>/<run id>/` in the derived bucket. The manifest goes last, so a manifest always names
   a bundle that exists.
4. For each derived asset, the state machine puts one pointer message `{asset_id, run_id, manifest_key}`
   on the extract queue, in the same message group as every other catalog write.
5. The extract handler reads the manifest and checks it before it writes anything: the asset and run
   match, the bundle key is the run's, only per-asset tables are replaced, only `subjects` and
   `sessions` are appended, and the last operation is `asset_headers`. Then it commits each table with
   `CatalogWriter.replace` (one Iceberg commit per table), `asset_headers` last. For an NWB file it also
   asks for a new LFP build.
6. The handler sets the tracking item DONE. The command prints PASS for it.

**Failure branches.** An error in one asset's derive sets its item FAILED and the Map goes on. An error
while committing sets the item FAILED and lists the tables already replaced, so the operator knows what
changed. A Lambda or queue error that the state machine catches is recorded on the item the same way. The
`rows/` prefix expires after 7 days.


## Build the LFP index

LFP (local field potential) files are large, so the API never opens them whole. A builder reads each file
once and stores a small index and an overview for fast reads later (`lfp_build.py`, `lfp_index.py`,
`lfp_overview.py`).

1. `LfpBuildFunction` receives one message from the LFP build queue. This queue is a standard queue with
   its own dead-letter queue, so it never touches the FIFO queues.
2. It opens the raw object at its pinned version with h5py over ranged reads and lists every
   `ElectricalSeries`.
3. For each series it builds a chunk-offset index: for every chunk of the data array, the byte range in
   the file, so a later reader can fetch exactly the chunks a window needs.
4. It builds an overview: a coarse whole-recording spectrogram of 1 s segments, from exact byte ranges, one
   chunk column at a time, with eight ranged requests in flight.
5. It writes `lfp/<asset id>/<version id>/<series>/index.bin` and `overview.bin` to the derived bucket,
   and the manifest `lfp/<asset id>/<version id>/manifest.json` last.

**Failure branches.** A series that cannot be indexed (no sample rate, no chunks, a shuffle filter, a
non-numeric type) is skipped. The reason is logged and listed in the manifest. A failure of the whole build
raises, so the queue delivers the message again and then moves it to the dead-letter queue. A rebuild of
the same version writes the same keys. Until the manifest exists, the LFP routes answer 404
`overview_not_built` or `index_not_built`.

The build of the largest test file took 54.6 s.


## Read the catalog and files

Three read paths exist, and none of them changes the catalog.

**SQL and filters in the SDK.** `asteroidea.sql(query)` binds one DuckDB view per catalog table on the
table's Iceberg metadata, using the researcher's temporary credentials, and rebinds a view only when the
metadata location changes. `asteroidea.query(subject=..., datatype=...)` scans `assets` with PyIceberg and
returns file references (asset id, S3 key, version id, checksum, size) with the snapshot id. Passing
`snapshot_id=n` reads the table as it was; `SnapshotGoneError` reports an expired snapshot. Neither call
returns bytes.

**Catalog routes.** `CatalogFunction` (`catalog_api.py`) serves the web app. It runs fixed SQL on Athena
with every value passed as an execution parameter, never inside the query text. Athena returns every cell
as text, so the function converts values. A small cache of asset rows (300 s) makes the presign that
follows a file-page load a cache hit. `GET /catalog/assets/{asset_id}/download` presigns a GET of the
raw object at its version. Athena failures are 502, and a query past its budget is 503.

**Lazy file reads.** `open_file` returns a read-only seekable file. Each `read(n)` is one ranged
`GetObject` pinned to the catalogued version, with no read-ahead and no cache (`_remote.py`).
`open_image` hands it to `nibabel`, so `img.dataobj[:, :, 48]` fetches only that slice. `open_nwb` hands
it to `pynwb` through a block cache. If credentials expire mid-read, the SDK refreshes them and repeats
the failed range once; otherwise it raises `ReadInterruptedError`. An unknown asset raises
`AssetNotFoundError`.

**Failure branches.** The identity-pool role allows only the tables and workgroup that `ResearcherRole`
names; a refusal becomes a permission error in the SDK without the raw AWS code.

Athena bills per terabyte scanned with no idle charge. Polars' `scan_iceberg` takes a PyIceberg table.
The pynwb documentation calls remfile "very new", says fsspec is slow for HDF5 and says the PyPI build of
h5py lacks ROS3. S3 checks expiry on every request, so a long read can fail half way. No age or QC filter
exists: neither the NIfTI header nor BIDS defines a standard QC field, so those would be platform-owned
columns.

In the first-build check, a query pinned to the first snapshot returned the same 3 rows after a second
upload made the current table 4 rows. One slice of a 3,146,080-byte NIfTI file
fetched 33,120 bytes (limit 65,536) in 1.4 s to 3.9 s against a 5 s limit. In the
first acceptance run the same read failed at 5.4 s and passed at 1.9 s on retry; the cause is open.


## Compute in the API

`ComputeFunction` (`compute_api.py`) answers five routes. It runs from its own container image, because it
needs numpy and h5py. Every number it returns comes from `asteroidea.analysis`, the one analysis module that
the SDK also uses, so the browser and the SDK show the same values. The function bins nothing itself and the
browser redoes no arithmetic.

**Firing rates: `GET /compute/rates`.**

1. The request names an asset, the units (`all`, a range such as `0-9`, or a list) and an `align` value:
   `intervals:<table>`, `events:<asset id>`, `frames:...` (movie frames), `label:<id>`,
   `label_name:<name>` or `event:<type>[:<attribute>=<value>]`. With an `event:` align, the window
   (`pre_s`, `post_s`, `bin_s`) may be left out; the dataset's analysis defaults fill it.
2. The function reads the asset, the units and the spike times from the catalog with one Athena batch
   (`athena_query.py`), and the alignment times for the chosen mode. If a unit has no `unit_spikes` rows
   it reads that unit's spikes from the NWB file through block-cached ranged reads. The body reports which
   source it used (`catalog` or `file`).
3. It estimates the response size from the spike counts before it builds the raster, and refuses when the
   body would pass 5 MiB.
4. `analysis.rates` bins the spikes. The optional z-score uses the baseline spread. With an `event:` align,
   the body also carries the defaults used and a responsive-unit screen (`analysis.responsive`).
5. The function answers JSON without NaN.

**LFP: `GET /compute/overview` and `GET /compute/spectrogram`.** The overview route returns one series'
whole-recording overview from the derived bucket: inline JSON when small, otherwise a presigned GET of the
binary object. The spectrogram route plans byte ranges for the requested window from the chunk index,
fetches them with a pool of eight ranged GETs on the version-pinned raw object, decodes the samples
without opening the file in h5py and runs `analysis.spectrogram`. A window is at most 1,200 s.

**Events: `GET /compute/events`.** The function reads the dataset's recipe (`analysis.load_recipe`: a JSON
file per dataset, or a default recipe) and resolves each event type of the recipe for the file: its onsets,
attributes and source (an intervals table, an events file, a stimulus file or labels). The body also lists
frames that fall outside the file's series.

**Tuning: `GET /compute/tuning`.** The request names an asset, an event type and an attribute. The
function resolves the events as above, takes the units' spikes and calls `analysis.tuning`. The result
carries the settings, the versions of the defaults and the recipe, and the label "exploratory screen".

**Failure branches.** The function refuses with 400 for a bad parameter, 404 for an unknown asset, 422
`not_nwb` for a file that is not NWB, 422 `screen_unresolved` when a tuning screen cannot run, 413 `too_large` for a
body over 5 MiB, and 503 `slow` once the Lambda's remaining time, less two seconds, has passed. A 413 names
the remedy (fewer units, a shorter window or `raster=0`).

Ten warm `frames:` rate calls took a median of 2.79 s and at most 5.05 s. A 60 s spectrogram window
through the index took 3.26 s cold and a median of 1.97 s warm, with 6 requests and 470,757 bytes; the
presigned overview fetch took 1.61 s.


## Mark and share an event period

A researcher marks a period on a file and shares it. Labels live in the DynamoDB labels table, outside the
catalog (`labels_api.py`).

1. The Events tab draws the file's trials and events and a Labels lane. Dragging in the lane starts a
   label. Saving sends `POST /files/{asset_id}/labels` with a name, a start and stop time in seconds
   and an optional note and series.
2. `LabelsFunction` reads the asset's dataset and S3 version through Athena. In parallel it reads the
   current snapshot id of `intervals`, `events` and `asset_headers`: Glue `GetTable` gives the Iceberg
   metadata file, and one S3 read of that file gives `current-snapshot-id`. It stamps the label with
   all of these and with the author's token `sub`.
3. It checks the caps (2,000 labels per file, 500 per author per file) and puts the item with
   `attribute_not_exists(label_id)`. The answer is 201.
4. Every researcher sees every label (`GET /files/{asset_id}/labels`). Only the author changes or deletes
   one. `PATCH` and `DELETE` carry the author and the label's `version` in a DynamoDB condition, so there
   is no read-then-write gap.
5. Labels feed back into the analysis. The `label:<id>` and `label_name:<name>` alignments of the rates
   route read the labels table, and `asteroidea.labels(<asset id>)` returns the records as a table.

**Failure branches.** A caller who is not the author gets 403. A label that changed since the caller read
it gets 409 "the label changed; reload it and try again". A file with 2,000 labels, or an author with 500,
gets 409. A snapshot read that S3 refuses leaves the snapshots null and still stores the label.


## Analysis runs and shared results

A researcher saves a result with a record of what produced it, and other researchers find it. The code is
`sdk/src/asteroidea/_run.py` and `_artifacts.py` in the SDK and `artifacts.py` behind `PlatformFunction`.

1. The researcher opens `with asteroidea.run(name, params) as r:`. While it is open, the SDK records every
   `sql()` table with its snapshot id, every file opened with its object version, every `query()` file and
   every result opened, and, for `Session.rates` and `Session.tuning`, the defaults and recipe versions.
   Runs do not nest (`NestedRunError`).
2. `r.save_artifact(path)` sends `POST /artifacts` with the record (`schema` `asteroidea.provenance/2`) and
   the ID token, because the record needs the author's email. The function validates the shape, checks that
   every `artifact` input names a COMMITTED result, writes the record as PENDING under
   `artifacts/<sub>/<id>/provenance.json` and writes index objects: `index/by-id/<id>`, one
   `index/by-input/<input>/<id>` per input. It returns a presigned PUT URL.
3. The SDK sends the file and calls `POST /artifacts/{id}/finalize`. The function checks the object's size
   and checksum against the record, sets COMMITTED and writes `index/by-time/...` and, for a record with a
   run, `index/by-run/<run>/<id>`.
4. `GET /artifacts?scope=all` lists the newest 100 finished results of every author from `index/by-time/`.
   `?input=<id>` lists the results that used one input, and `?run=<run id>` the results one run saved. Each
   row carries its `run_id`. `GET /artifacts/{id}` returns any author's COMMITTED or WITHDRAWN record with a
   15 minute presigned GET that serves the record's content type inline (`?download=1`: as an attachment).
5. Only the author edits `note` and `name` (`PATCH`), withdraws a result (`POST .../withdraw`, which keeps
   the bytes) or deletes it (`DELETE`).

The web app shows these on the Results pages: the list, a result page with its run, inputs and "used by"
panels, a preview (an image inline, an HTML page in a sandboxed frame), the other files of the same run,
and a download link.

**Failure branches.** A size or checksum mismatch at finalize deletes the object and sets INCOMPLETE (409
`upload_incomplete`). A finalize before the upload is 409 `upload_pending`. A non-author gets 403
`not_author` on a readable record and 404 on an unfinished one. A delete of a result that another result
used is 409 `has_dependents` and names the dependents. A record with a schema but a token without an email
is 401 `id_token_required`. Records written before the index existed are filled in by
`make artifacts-backfill`.


## Analyze a session in the SDK

`asteroidea.load_session(dataset, subject, session)` loads one session's files, units, spikes and events
in one call (`_session.py`). A session is `<dataset>/<subject>/<session or _>`; `_` means no BIDS session.

1. The loader reads the session's `files`, `units`, `unit_spikes`, `series`, `intervals` and `events`
   through `sql()`.
2. It resolves the dataset's recipe into alignment events. For an event type that comes from a stimulus
   file, it reads the dataset's CSV through `open_file` and the frame times through `open_nwb`.
3. The researcher calls `Session.rates(event_type)` for the PSTH and responsive screen,
   `Session.tuning(event_type, attribute)` for the tuning screen, `Session.band_power(event_type)` for
   event-aligned band power, `Session.lfp(...)` for a window of the raw signal and `Session.to_pynapple()`
   for pynapple objects (optional extra). Each result row carries its settings, the versions of the defaults
   and the recipe, and the label "exploratory screen".

**Failure branches.** A session whose units sit in more than one file raises `ValueError`, because unit
indexes would collide. A tuning screen with fewer than two kept levels raises `ValueError` that says why.
A missing extra raises `ExtraMissingError` naming it.


## Deploy by an administrator

The administrator signs in through a named SSO profile pinned to the AWS account.

1. `aws sso login` gives temporary credentials that refresh on their own.
2. `make diff` runs `cdk diff` for each top-level stack, DataStack and ServicesStack. The security-change
   preview covers top-level stacks, so the administrator reads nested-stack changes as CloudFormation
   change sets.
3. `make deploy` runs `cdk deploy`. ServicesStack builds container images, so the Docker daemon must run;
   the target refuses when it does not. The command refuses too when the deployed DataStack still exports
   the sandbox test client and `SANDBOX_TEST` is not 1.
4. `make web-publish` builds the web app and the SDK wheel and uploads them to the site bucket. Run
   `make web-smoke` after it.

**Failure branches.** CloudFormation rolls a failed top-level stack back to its last good state. A
ServicesStack failure cannot roll DataStack back, which is why the stacks are split. DataStack is
termination-protected and keeps its buckets and tables on delete, so an accidental `cdk destroy` fails.
Replacing the raw bucket shows in `cdk diff` and is refused in review.

Identity Center credentials use the OIDC device-code flow and refresh automatically. The deploy permission
set is an IAM role that only Identity Center users can assume. The pinned command line is
`npx aws-cdk@2.1146.1`. The security-change preview covers top-level stacks only, so
nested-stack changes are read as CloudFormation change sets (`cdk deploy --method=prepare-change-set`)
until a spike shows otherwise. Nested stacks cannot be deployed alone, so they deploy with ServicesStack.
The first DataStack deploy failed on the Glue Iceberg table and left the stack in ROLLBACK_COMPLETE; the
stack and the orphans that Retain left were cleaned up by hand.


## History

The first design had more steps than the system runs today. It had a header check on the researcher's
machine before an upload, multipart uploads, a validator for de-identification and BIDS or NWB structure, a
quarantine bucket for failed uploads and an artifact lake with its own catalog. None of these is built.
The SDK sends one file per session in one PUT, and the commit function verifies the checksum only. The
first design also had the SDK query Athena directly; the SDK reads through PyIceberg and DuckDB and the web
app reads through the catalog routes. The decision records are in the [decisions chapter](09-decisions.md).

A DICOM series or a vendor ephys folder has no single "done" event, which is why finalize is an explicit
call. S3 object-created events carry the version id and a sequencer; the first design used them to
deduplicate repeated events.

