# Developer guide

This page is for people who change the repository. It lists the commands by task, the layout, the
gates, the lint and type exceptions, the pinned versions, and the files you touch to add a route, a
function or a table. Researchers do not need it: see [For researchers](researchers.md).

Run `make help` for the live list of targets. The tables below group them by what you want to do.

## Set up

You need `git`, `uv` (0.13.5 or newer, below 0.14), Node.js 22.12 or newer (not 25) with `npx`, and
the docker daemon for anything that builds a container image.

| Task | Command |
|---|---|
| Check the toolchain; it names each missing or wrong tool | `make doctor` |
| Install the pinned mdBook into `.tools/bin` | `make tools` |
| Sync the Python workspace, install the git hooks and the web packages | `make install` |
| Remove virtual environments, build output and caches | `make clean` |
| Clean, then reinstall everything | `make rebuild` |


## Check your work

| Task | Command |
|---|---|
| Format, lint and type-check | `make format`, `make lint`, `make typecheck` |
| Run the Python tests (not the synth-marked ones) | `make test` |
| Run every gate | `make check` |
| Run every gate, then synthesise and run the synth-marked tests | `make check-full` |
| Build the docs | `make docs` |
| Serve the docs on `DOCS_PORT` (default 3000), then stop them | `make serve`, `make kill` |
| Run the web gates | `make web-check` |
| Run the web end-to-end tests (headless Chromium, 1440 x 900) | `make web-e2e` |


`make check` stays fast: a warm run takes at most 60 seconds. `asteroidea_infra/__init__.py` and
`config.py` import no `aws_cdk`, so importing the package does not load the CDK.

## Change the infrastructure

| Task | Command |
|---|---|
| Synthesise every stack offline | `make synth` |
| Compare the synthesised templates with `origin/main` | `make gate-templates` |
| Show the diff against the deployed stacks (live) | `make diff` |
| Deploy every stack (live; needs docker) | `make deploy` |
| Destroy ServicesStack only (live) | `make destroy-services` |
| Create or extend the catalog tables (live; `DRY=1` prints the plan) | `make catalog-init` |
| Build and size-check a Lambda image | `make commit-image`, `make extract-image`, `make compute-image` |
| Check the resource budget (offline) | `make resource-budget` |

The live targets load `~/.config/asteroidea/sandbox.env`. They refuse with exit 4 (make reports 2) when
`ASTEROIDEA_PROFILE` or `ASTEROIDEA_ACCOUNT` is unset. `make synth` and the tests are offline; `make diff` is live.

`make deploy` first checks that the docker daemon runs, because ServicesStack builds container images.
It refuses with exit 3 before any stack is touched when the daemon is down. Run `docker info` yourself
first; a non-zero exit means stop. `make gate-deploy-docker` tests that refusal offline with stubs.
The deploy also reads the deployed DataStack: when it exports the sandbox-test client id and
`SANDBOX_TEST` is not 1, it refuses with exit 2. Pass `SANDBOX_TEST=1` on every deploy. Other deploy
variables: `STACKS=<names>`, `COLD_NONCE=<n>` (forces one Lambda cold start) and
`RAW_RETENTION_DAYS=<n>` (Object Lock retention on raw files, default 1). `make deploy` and `make diff`
take the same variables.

The raw bucket has Object Lock on with no default retention. The commit Lambda sets governance
retention on each verified raw object version. No role can bypass governance retention.

## Run the data and the sandbox account

These targets are live. Each uses the sandbox profile.

| Task | Command |
|---|---|
| Create a researcher, write `platform.json` and build the SDK wheel | `make researcher EMAIL=<address>` |
| Print the SDK `platform.json` | `make sdk-config` |
| Build the SDK wheel into `dist/` | `make sdk-wheel` |
| Add a researcher to the curators group | `make curator EMAIL=<address>` |
| Import a dataset | `make import SOURCE=<dandi:ID@V or openneuro:ID@V>` |
| Load a subject-links CSV (new links added, changed ones replaced; `linked_by` must be your sign-in email, not `OWNER_SIGN_IN_EMAIL`) | `make crosswalk FILE=curation/subject-links/<file>.csv` |
| Make the studies tables equal `curation/studies/*.csv` (a dataset is in at most one study; run after the imports) | `make studies FILE=curation/studies/studies.csv` |
| Set dataset tiers (`curated`, `upload`, `sandbox`) from a CSV; each row is rewritten in full and only `tier` changes | `make tiers FILE=curation/tiers/tiers.csv` |
| Re-send stopped sessions to their queue | `make reclaim` |
| Re-run the extractors over catalog assets | `make reextract` |
| Read the open app-dev notes from the web app's Note button, newest first, as markdown | `make notes` |
| Mark one app-dev note handled (an unknown id is refused) | `make notes-done ID=<note id>` |
| Run one live test file | `make live T=tests/live/<file>` |
| Publish the web app, the SDK wheel and the notebook | `make web-publish` |

`make reclaim` and `make reextract` take `DRY=1` to list without acting. `make reextract`
starts one state-machine run; the command line never invokes a function or sends to a queue.
`make reclaim` re-sends sessions that stopped part way; `FORCE=1 SESSION=<id>` first expires that
session's lease.

## Run the checks against the sandbox account

Each target below is live, creates its own researcher, and prints PASS or FAIL per step. Run
`make web-publish` and `make deploy` first. They write a report under `~/asteroidea-*/<run-id>/`.

| What it checks | Command |
|---|---|
| The first hour, step by step (`HEADLESS=1` runs without a human) | `make live-first-hour` |
| The six-step SDK analysis in a fresh environment | `make live-analysis` |
| A notebook run headless (`NOTEBOOK=showcase` for the showcase; default the exemplar), with its executed copy and HTML. `AS=me` creates no researcher: it runs as your own saved SDK sign-in (`~/.config/asteroidea`), so the results it saves are yours | `make live-notebook` |
| A new researcher's first hour (`PASS=2` for the second pass) | `make live-hosted-setup` |
| The hosted research app (`PASS=1` runs the first pass, `PASS=2` the second) | `make live-research-app` |
| Every page of the hosted app, headless; run it after each `make web-publish` | `make live-web-smoke` |
| The presign and label routes, from the log lines they write | `make live-route-timing` |

`make notebook-run NOTEBOOK=showcase AS=me` checks your sign-in first with one `asteroidea.studies()`
call. When `tokens.json` is missing or the sign-in is refused, the step fails and tells you to run
`asteroidea.sign_in()` once (in JupyterLab from `make notebook`, for example). The run reads that
directory and never copies or rewrites your tokens; the report's mode line records that the run
used your own sign-in.

The old names (`acceptance`, `analysis`, `notebook-run`, `outsider-run`, `rf-run`, `rf-timing`,
`web-smoke`, `rf-budget`) remain as aliases. The target `live-iceberg-check`
is a catalog probe that leaves a probe table behind.


Other targets: `make harvest` builds the DANDI and OpenNeuro index (network, about 80 seconds);
`make peek DANDISET=<id> VERSION=<v> ASSET=<uuid>` prints the shape of an NWB file read over HTTP;
`make notebook` opens JupyterLab on the exemplar notebook (`NOTEBOOK=showcase` opens the showcase)
and `make notebook-kill` stops it;
`make web` starts the Vite dev server on `WEB_PORT` (default 5173) and `make web-kill` stops it.

## Layout

| Path | What it holds |
|---|---|
| `infra/` | Package `asteroidea-infra`: the CDK app, the function registry, the live runners and the admin commands. `infra/account/` records the sandbox account setup. |
| `ingest/` | Package `asteroidea-ingest`: the Lambda handlers, the route table, the shared API kit and the three Dockerfiles. |
| `sdk/` | Package `asteroidea`, the researcher SDK. |
| `services/` | Package `asteroidea-services`: the dataset harvester and the NWB peek tool. |
| `web/` | The web app (React, Vite, TypeScript). It sits outside uv and is built with npm. |
| `notebooks/` | `exemplar.ipynb`, the researcher walk-through, and `showcase.ipynb`, one patient analysed in depth (spikes, LFP, anatomy). |
| `curation/` | The reviewed subject-links, studies and tiers CSVs and the ancillary-file manifest. |
| `docs/` | This book (an mdBook) and its decision records. |
| `scripts/` | The gate scripts (`scripts/gates/`) and the suppression allowlist. |
| `tests/` | Workspace-level tests, and `tests/live/` (collected only with `ASTEROIDEA_LIVE=1`). |


### Infrastructure modules

`cdk.json` runs `python -m asteroidea_infra.app`. There are two stacks. DataStack holds the state
(buckets, tables, the catalog, the user pool and the identity pool) and has termination protection.
ServicesStack holds the code and has two nested stacks, Ingest and Api. There is no VPC. Key modules:

| Module | Role |
|---|---|
| `app.py`, `data_stack.py`, `api.py`, `ingest.py` | The stacks. |
| `functions.py` | The function registry: one row and one statement function per Lambda function, and the builder that loops over them. |
| `config.py` | The account, the region, the tags and the fixed IAM role names. |
| `schema.py`, `catalog.py`, `catalog_init.py` | The catalog tables and the live script that creates them. |
| `identity.py`, `storage.py`, `web_hosting.py`, `athena.py` | Identity, buckets and tables, the site bucket and CloudFront, the Athena workgroup. |
| `runner.py`, `steps.py`, `sandbox_guard.py`, `live_api.py` | The shared kit of the live runners. |
| `nag.py` | cdk-nag checks. Each acknowledgement carries a reason. |

In `ingest/src/asteroidea_ingest/`: `routes.py` (the route table), `api_common.py` (the shared handler
kit), one handler module per API function, `commit.py`, `import_file.py`, `import_plan.py`, the `extract/`
package and `timing.py` (the one place for timeouts, visibility times and leases). The commit image
must not carry h5py, remfile, pynwb or awscrt; a test pins that.

## The registry and the route table

Every Lambda function is one `FunctionSpec` row in `infra/src/asteroidea_infra/functions.py`. There are
12 functions: six in `API_FUNCTIONS` (Session, Platform, Catalog, Compute, Labels, Imports) and six in
`INGEST_FUNCTIONS` (Commit, Import, ImportPlan, Extract, LfpBuild, Derive). `api.py` and `ingest.py`
loop over the rows.

Every HTTP route is one `Route` row in `ingest/src/asteroidea_ingest/routes.py`. There are 36 routes.
A row holds the method, the path, the function that serves it, the Cognito group the caller must be in
(`researchers` or `curators`), and a one-line summary. `routes.py` imports only the standard library, so
the zip Lambdas can import it.

The [API reference](api.md) lists every route and describes its request, response and errors.

### Add a route

1. Add a `Route` row to `routes.py`. Put it at the end of its function's block or at the end of the
   table. A function's first row stays first, because CDK names the function's integration after the
   path of the first route bound to it.
2. Add a method to the function's Api class in its handler module, and a `DISPATCH` entry that maps the
   route key (for example `"GET /catalog/datasets"`) to the method name. The test
   `test_every_route_has_a_handler_and_every_handler_a_route` in `infra/tests/test_registry.py`
   fails when the table and a `DISPATCH` map disagree.
3. Add the route key to `infra/tests/fixtures/routes.txt`. Two tests read that file as an independent
   list of routes (`test_services_synth.py` and `test_timing_line_per_route.py`).
4. If the handler reads a resource its role cannot reach, add a statement to the function's
   `*_statements` function in `functions.py`.
5. Write the route's section on the API page (`docs/src/api.md`; a test fails when a route has no section).

A new HTTP method must be in `CORS_METHODS` in `api.py`. A new Cognito group needs a user-pool group in
`identity.py`, a constant in `routes.py`, and a place in the set of known groups that
`test_route_methods_are_in_the_cors_set_and_groups_are_known` checks.

### Add a function

1. Add a fixed role name to `config.py`.
2. Add a `*_env` function and a `*_statements` function to `functions.py`, then a `FunctionSpec` row to
   `API_FUNCTIONS` or `INGEST_FUNCTIONS`. Statements are ordinary CDK `PolicyStatement` lists, in
   template order. Never change the `construct_id` of a deployed function: CloudFormation would create
   the new function and role before it deletes the old ones, and the fixed role name makes that fail.
3. Write the handler module. A zip function imports only the standard library, the runtime's boto3 and
   `asteroidea_ingest`; `infra/tests/test_api_bundle.py` checks it. An image function with no `cmd`
   override must name the Dockerfile's `CMD` as its handler.
4. If the function needs a resource that no stack passes it yet, add a field to `ApiInputs` in `api.py`
   (or an argument to `IngestStack`) and wire it in `app.py`. Add an `app.py` output when a script needs
   the value.
5. Update the counts in `infra/tests/test_registry.py`
   (`test_the_registry_holds_all_twelve_functions`, and for an API function the list in
   `test_covers_the_six_api_functions`).
6. For an API function, add its routes as above.

### Add a table

1. Add a `TableSpec` to `TABLES` in `infra/src/asteroidea_infra/schema.py`. `TABLE_NAMES` follows from
   it, and the researcher role's Glue grant is built from `TABLE_NAMES`. There are 14 tables today.
2. Run `make catalog-init` (live). It creates the missing table and adds missing optional columns. It
   never drops or retypes a column and refuses a type conflict.
3. Grant each function that needs the table: the table tuples in `functions.py` (`CATALOG_TABLES`,
   `COMPUTE_TABLES`, `LABELS_TABLES`, `COMMIT_TABLES`, `EXTRACT_TABLES`) feed the Athena and Glue
   statements.
4. Copy the name into `TABLE_NAMES` in `sdk/src/asteroidea/_sql.py`. The SDK cannot import infra, so it
   keeps its own copy. Only a count test (`sdk/tests/test_sql.py`) guards the copy, so check the names
   by hand.
5. Update the counts in `infra/tests/test_catalog_schema.py` and `sdk/tests/test_sql.py`.
6. If an extractor fills the table, add it to the table sets in `extract/handler.py`.

### When to split a nested stack

CloudFormation allows 500 resources per template. Today the largest template (Api) has 98. Each route
costs two resources and each routed function costs four.

- A deployed function never changes its `construct_id` or the stack that holds it.
- The budget for one template is 300 resources, 150 outputs, 150 parameters and 800,000 bytes. The
  stack-budget test in `infra/tests/test_stack_budget.py` fails above any of them.
- When a stack would pass its budget, new functions go into a new nested stack with their own tuple, in
  a labelled infrastructure change with a deploy. Existing functions never move.

`make gate-templates` synthesises `origin/main` and your branch and fails on any template difference
other than asset hashes. A refactor that is meant to change nothing deployed must pass it.

A change that alters infrastructure on purpose (in its own labelled change) replaces the contents of
`infra/template-diff.expected` with the exact `DIFF` lines the gate prints, one per line (`#` lines and blank
lines are ignored). When that file differs from `origin/main`'s copy, the gate passes only if the `DIFF` lines
equal the file's lines (it prints `UNEXPECTED` or `MISSING` lines otherwise); the next change need not touch it.

## Error handling

Every refusal the Lambda functions send has the same body (the values here are an example):

```json
{"error": "not_found", "detail": "No such route."}
```

- `error` is a short stable code. The SDK and the web app branch on it.
- `detail` is one sentence for a person. It is the sentence a researcher sees.
- Handlers raise `ApiError(status, detail, code=...)`. The code defaults from the status: 400
  `invalid_request`, 403 `forbidden`, 404 `not_found`, 409 `conflict`, 410 `gone`, 413 `too_large`,
  422 `unprocessable`, 500 `internal_error`, 502 `upstream_failed`, 503 `unavailable`. A refusal that a
  client must tell apart from its status default takes an explicit code, for example `dataset_not_yours`
  or `session_expired`.
- An exception that is not an `ApiError` is logged as one JSON line with the exception class, the
  request id, the route and the traceback. The caller gets status 500, code `internal_error` and a
  detail that names the request id.
- API Gateway's own answers keep the shape `{"message": ...}`: 401 from the authorizer, 404 for an
  unrouted path, 500 when a function fails to start, and 503 on the 30-second integration timeout.
  Clients branch on the status for these.
- Because the handler returns a 500 instead of raising, the Lambda `Errors` metric no longer counts
  a handler crash. The logged `unhandled` line is where crashes are counted.

Every API request writes one JSON timing line, with the route, the status, the request id and the time
spent in each phase. `make live-route-timing` reads those lines back.

## The gates

`make check` runs these, each on its own line and never piped:

| Gate | Command |
|---|---|
| Lock file current | `uv lock --check` |
| Format | `uv run --frozen ruff format --check .` (this also formats Python code blocks in the markdown files) |
| Lint, all rules | `uv run --frozen ruff check .` |
| Types, strict | `uv run --frozen ty check` |
| Tests | `uv run --frozen pytest` (strict markers, warnings are errors, an unexpected pass fails) |
| Docs build | `.tools/bin/mdbook build docs` |
| Help text on every target | `make help-check` |
| Web gates | `make web-check`: Biome, strict `tsc`, Vitest, Vite build, bundle-size cap |
| No suppression comments | `make gate-suppressions` |

`make check-full` adds `make synth` and `pytest -m synth` (cdk-nag and the stack assertions). The CDK
tests carry the `synth` marker, so `make check` skips them. Other gates:

| Gate | Command |
|---|---|
| Docs: no stale scope phrases | `make gate-docs` |
| Pre-commit hooks, before you open a pull request | `uv run --frozen prek run --all-files` |
| Toolchain | `make doctor`, and `make gate-doctor` under four stub node versions |
| Deploy refuses without docker | `make gate-deploy-docker` |
| Templates equal the base branch | `make gate-templates` |
| Harvest counts and peek bytes against the live APIs (network) | `make gate-live` |


A docs-only change re-runs the docs build, `make gate-docs` and the format check.

## Lint and type exceptions

The strict settings (ruff `ALL`, ty `all = "error"`, strict pytest) relax in these ways only:

- Files under `tests/` and `*/tests/` may ignore `S101`, `S105`, `S106`, `ANN`, `D1`, `PLR2004` and
  `INP001`.
- `infra/src/asteroidea_infra/runner.py` and no other path may ignore `S603` and `S607`. It runs
  argument lists with `shell=False` and a timeout. Every live runner runs its child commands through
  `runner.execute`.
- `sdk/src/asteroidea/_setup.py` and no other path may ignore `S603`, for the same reason.
- The copyright-header rule `CPY001` is ignored for the whole repository, because the root `LICENSE`
  covers it.
- No suppression comment: `noqa`, `ty: ignore`, `type: ignore`, `biome-ignore`, `@ts-ignore`,
  `@ts-expect-error`. `make gate-suppressions` fails on any that `scripts/suppression-allowlist.txt`
  does not list. The allowlist is empty (its non-comment line count is 0), and a stale line fails the
  gate too.
- Third-party warnings are filtered per module in `filterwarnings`, each with its reason.
- ty overrides are scoped to a path, each with its reason: `catalog_writer.py` (untyped PyIceberg and
  pyarrow results), and `api.py` and `identity.py` (an aws-cdk-lib protocol mismatch).
- cdk-nag acknowledgements in `nag.py` are data with a reason, not exceptions.

Anything wider needs a maintainer decision.

## Pins

Pins are exact. Bump one in its own change.

| Tool | Version |
|---|---|
| uv | `>=0.13.5,<0.14` (`required-version`) |
| Python | 3.14 (`.python-version`), for every workspace member |
| Node | 24 (`.node-version`); `make doctor` accepts 22.12 or newer, except 25 |
| aws-cdk CLI | 2.1146.1, through `npx` |
| mdBook | 0.5.4, a checksummed binary |
| ruff, ty, pytest, prek | 0.17.2, 0.0.91, 9.1.1, 0.5.7 |
| aws-cdk-lib, constructs, cdk-nag | 2.274.0, 10.8.1, 3.0.2 (`infra/pyproject.toml`) |
| pyiceberg, boto3 | 0.12.0, 1.43.118 (`ingest/pyproject.toml`) |
| pynwb, h5py, remfile, nibabel | 4.2.0, 3.16.0, 0.1.15, 5.4.2 (the `extract` extra of `ingest/`) |
| duckdb, polars | 1.5.6, 2.0.0 (root dev group) |
| matplotlib, ipython | 3.11.2, 9.17.1 (root dev group; the notebook pins repeat them) |
| scipy, nilearn | 1.18.1, 0.14.1 (root dev group; the notebook pins repeat them) |
| jupyterlab, ipykernel, nbconvert | 4.5.11, 7.4.0, 7.17.2 (the notebook environment only) |
| React, Vite, TypeScript, Vitest, Biome, Playwright | 19.3.1, 8.3.4, 7.0.2, 5.0.5, 2.5.15, 1.64.1 (`web/package.json`) |
| Source Serif 4, IBM Plex Sans, IBM Plex Mono (Fontsource) | 5.3.1, 5.3.0, 5.3.0 (`web/package.json`; latin `.woff2` only, files unmodified under the OFL, texts in `web/public/licenses/`) |

The ingest runtime pins stay exact, and the commit image carries no harvester dependency. The notebook
tools stay out of `uv.lock`; `make notebook` builds its own environment.

