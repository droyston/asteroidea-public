.DEFAULT_GOAL := help
.PHONY: help doctor tools install format lint typecheck test docs check help-check version-sync harvest peek serve kill clean rebuild \
  web-install web web-kill web-check web-e2e gate-docs gate-suppressions gate-doctor gate-deploy-docker gate-live live \
  synth diff check-full deploy destroy-services catalog-init reclaim import crosswalk studies tiers summaries notes notes-done live-iceberg-check commit-image extract-image compute-image resource-budget researcher researcher-add curator sdk-config sdk-wheel web-publish live-first-hour live-analysis notebook notebook-kill live-notebook live-hosted-setup live-research-app live-route-timing live-web-smoke live-ux-playtest

.PHONY: acceptance analysis notebook-run outsider-run rf-run rf-timing web-smoke rf-budget
acceptance: live-first-hour ## alias of live-first-hour
analysis: live-analysis ## alias of live-analysis
notebook-run: live-notebook ## alias of live-notebook
outsider-run: live-hosted-setup ## alias of live-hosted-setup
rf-run: live-research-app ## alias of live-research-app
rf-timing: live-route-timing ## alias of live-route-timing
web-smoke: live-web-smoke ## alias of live-web-smoke
rf-budget: resource-budget ## alias of resource-budget

MDBOOK_VERSION := 0.5.4
CDK_VERSION := 2.1146.1
MDBOOK := .tools/bin/mdbook
DOCS_PORT ?= 3000
WEB_PORT ?= 5173
NOTEBOOK ?= exemplar
NOTEBOOK_PORT ?= 8888
NOTEBOOK_ARGS ?=
PLAYWRIGHT_BROWSERS_PATH := $(CURDIR)/.tools/ms-playwright
WEB_DIRS := web/node_modules web/dist web/artboards web/test-results web/playwright-report web/blob-report
SANDBOX_ENV ?= $(HOME)/.config/asteroidea/sandbox.env

# Live targets load the sandbox env, then refuse (exit 4) if the profile or account is unset.
define SANDBOX_GUARD
set -a; if [ -f "$(SANDBOX_ENV)" ]; then . "$(SANDBOX_ENV)"; fi; set +a; \
  if [ -z "$$ASTEROIDEA_PROFILE" ] || [ -z "$$ASTEROIDEA_ACCOUNT" ]; then \
    echo "refusing: ASTEROIDEA_PROFILE and ASTEROIDEA_ACCOUNT are unset; $(SANDBOX_ENV) holds them"; \
    exit 4; fi
endef

help: ## List the targets
	@grep -E '^[a-zA-Z0-9_-]+:.*## ' Makefile | awk -F':.*## ' '{printf "%-12s %s\n", $$1, $$2}'

doctor: ## Check the toolchain; name each missing or wrong tool
	@fail=0; \
	  if command -v uv >/dev/null 2>&1; then \
	    v=$$(uv --version | awk '{print $$2}'); \
	    case "$$v" in \
	      0.13.[5-9]|0.13.[1-9][0-9]*) echo "ok    uv $$v";; \
	      *) echo "FAIL  uv $$v (need >=0.13.5,<0.14)"; fail=1;; \
	    esac; \
	  else echo "FAIL  uv missing (need >=0.13.5,<0.14)"; fail=1; fi; \
	  if command -v node >/dev/null 2>&1; then \
	    nv=$$(node --version); v=$${nv#v}; major=$${v%%.*}; rest=$${v#*.}; minor=$${rest%%.*}; \
	    if [ "$$major" -eq 25 ] || [ "$$major" -lt 22 ] || { [ "$$major" -eq 22 ] && [ "$$minor" -lt 12 ]; }; then \
	      echo "FAIL  node $$nv (need >=22.12, not 25)"; fail=1; \
	    else echo "ok    node $$nv"; fi; \
	  else echo "FAIL  node missing (need >=22.12, not 25)"; fail=1; fi; \
	  if command -v npx >/dev/null 2>&1; then \
	    out=$$(npx --yes aws-cdk@$(CDK_VERSION) --version 2>&1); \
	    case "$$out" in \
	      "$(CDK_VERSION)"|"$(CDK_VERSION) "*) echo "ok    aws-cdk $$out";; \
	      *) echo "FAIL  aws-cdk $(CDK_VERSION) not resolved by npx: $$out"; fail=1;; \
	    esac; \
	  else echo "FAIL  npx missing (ships with node)"; fail=1; fi; \
	  if command -v docker >/dev/null 2>&1; then \
	    if docker info >/dev/null 2>&1; then echo "ok    docker $$(docker --version | awk '{print $$3}' | tr -d ,)"; \
	    else echo "FAIL  docker daemon not running (needed to synth and deploy container Lambdas)"; fail=1; fi; \
	  else echo "FAIL  docker missing (needed to synth and deploy container Lambdas)"; fail=1; fi; \
	  if command -v git >/dev/null 2>&1; then echo "ok    $$(git --version)"; \
	  else echo "FAIL  git missing"; fail=1; fi; \
	  if [ -x $(MDBOOK) ] && [ "$$($(MDBOOK) --version)" = "mdbook v$(MDBOOK_VERSION)" ]; then echo "ok    $$($(MDBOOK) --version)"; \
	  else echo "FAIL  mdbook v$(MDBOOK_VERSION) not at $(MDBOOK): run make tools"; fail=1; fi; \
	  exit $$fail

tools: ## Install the pinned mdBook into .tools/bin
	@set -e; \
	  if [ -x $(MDBOOK) ] && [ "$$($(MDBOOK) --version)" = "mdbook v$(MDBOOK_VERSION)" ]; then \
	    echo "mdbook v$(MDBOOK_VERSION) already installed"; exit 0; fi; \
	  case "$$(uname -s)-$$(uname -m)" in \
	    Darwin-arm64) triple=aarch64-apple-darwin; sha=03e8a6d8b13a2971e0b3280affd03b388373c1485e26f73407c3a76b0b1838df;; \
	    Darwin-x86_64) triple=x86_64-apple-darwin; sha=a47d7bf0d5d670cff9ee6cce95537cbeb62dc10704d9e7131ffbd13e2b59a5de;; \
	    Linux-x86_64) triple=x86_64-unknown-linux-gnu; sha=3f28de05dafca9d0f2eab99c662116b0e37b89b1d96a08f8f430b9eeae958cd7;; \
	    Linux-aarch64) triple=aarch64-unknown-linux-musl; sha=753e5c5c363ee8a56972344dcf91466f005a51db84a7aeffe427ae3ef83d6d44;; \
	    *) echo "no pinned mdBook for $$(uname -s)-$$(uname -m)"; exit 1;; \
	  esac; \
	  mkdir -p .tools/bin .tools/dl; \
	  tgz=.tools/dl/mdbook-v$(MDBOOK_VERSION)-$$triple.tar.gz; \
	  echo "downloading mdbook v$(MDBOOK_VERSION) ($$triple)"; \
	  curl -fsSL -o $$tgz https://github.com/rust-lang/mdBook/releases/download/v$(MDBOOK_VERSION)/mdbook-v$(MDBOOK_VERSION)-$$triple.tar.gz; \
	  if command -v sha256sum >/dev/null 2>&1; then got=$$(sha256sum $$tgz | awk '{print $$1}'); \
	  else got=$$(shasum -a 256 $$tgz | awk '{print $$1}'); fi; \
	  if [ "$$got" != "$$sha" ]; then \
	    rm -f $$tgz; echo "checksum mismatch for mdbook: expected $$sha, got $$got"; exit 1; fi; \
	  tar -xzf $$tgz -C .tools/bin mdbook; \
	  rm -f $$tgz; \
	  echo "installed $$($(MDBOOK) --version)"

install: ## Sync the workspace and install the git hooks
	uv sync --all-packages --frozen
	uv run --frozen prek install --allow-missing-config
	@$(MAKE) --no-print-directory web-install

format: ## Format the code with ruff
	uv run --frozen ruff format .

lint: ## Lint with ruff
	uv run --frozen ruff check .

typecheck: ## Type-check with ty (strict)
	uv run --frozen ty check

test: ## Run the tests, except the synth-marked ones
	uv run --frozen pytest -m "not synth and not docker"

docs: ## Build the docs with mdBook
	@test -x $(MDBOOK) || { echo "missing $(MDBOOK): run make tools"; exit 1; }
	$(MDBOOK) build docs

check: ## Run every gate: lock, format, lint, types, tests, docs, help, web, docs gates, suppressions
	uv lock --check
	uv run --frozen ruff format --check .
	@$(MAKE) --no-print-directory lint
	@$(MAKE) --no-print-directory typecheck
	@$(MAKE) --no-print-directory test
	@$(MAKE) --no-print-directory docs
	@$(MAKE) --no-print-directory help-check
	@$(MAKE) --no-print-directory web-check
	@$(MAKE) --no-print-directory gate-docs
	@$(MAKE) --no-print-directory gate-suppressions

check-full: ## check, then synth and the synth-marked tests
	@$(MAKE) --no-print-directory check
	@$(MAKE) --no-print-directory synth
	uv run --frozen pytest -m synth

synth: ## Synthesise every stack offline
	uv run --frozen npx --yes aws-cdk@$(CDK_VERSION) synth '*' $(CDK_CONTEXT) --quiet

diff: ## LIVE: show the CDK diff against the deployed stacks
	@$(SANDBOX_GUARD); \
	  uv run --frozen npx --yes aws-cdk@$(CDK_VERSION) diff $(STACKS) $(CDK_CONTEXT) --profile "$$ASTEROIDEA_PROFILE"

SANDBOX_TEST ?= 0
COLD_NONCE ?=
RAW_RETENTION_DAYS ?= 1
STACKS ?= '*'
CDK_CONTEXT := $(if $(filter 1,$(SANDBOX_TEST)),-c sandbox-test=true,) $(if $(COLD_NONCE),-c cold-nonce=$(COLD_NONCE),) -c raw-retention-days=$(RAW_RETENTION_DAYS)

deploy: ## LIVE: deploy every stack (needs docker; SANDBOX_TEST=1, STACKS=<names>)
	@$(SANDBOX_GUARD); \
	  if ! docker info >/dev/null 2>&1; then \
	    echo "refusing: the docker daemon is not running (ServicesStack builds container images; start Docker Desktop, then re-run make deploy); no stack was touched"; exit 3; fi; \
	  uv run --frozen python -m asteroidea_infra.admin deploy-check --sandbox-test $(if $(filter 1,$(SANDBOX_TEST)),1,0) || exit $$?; \
	  uv run --frozen npx --yes aws-cdk@$(CDK_VERSION) deploy $(STACKS) $(CDK_CONTEXT) --profile "$$ASTEROIDEA_PROFILE" --require-approval never

destroy-services: ## LIVE: destroy ServicesStack only
	@$(SANDBOX_GUARD); \
	  uv run --frozen npx --yes aws-cdk@$(CDK_VERSION) destroy ServicesStack --profile "$$ASTEROIDEA_PROFILE" --force

catalog-init: ## LIVE: create or extend the catalog tables (DRY=1 prints the plan)
	@$(SANDBOX_GUARD); \
	  uv run --frozen python -m asteroidea_infra.catalog_init $(if $(filter 1,$(DRY)),--dry-run,)

import: ## LIVE admin: import a dataset (SOURCE=dandi:ID@V or openneuro:ID@V)
	@$(SANDBOX_GUARD); \
	  if [ -z "$(SOURCE)" ]; then echo "usage: make import SOURCE=dandi:000623@0.240227.2023 [INCLUDE=...] [EXTRA=...]"; exit 2; fi; \
	  uv run --frozen python -m asteroidea_infra.curate import --source "$(SOURCE)" --include "$(INCLUDE)" $(if $(EXTRA),--extra "$(EXTRA)",)

crosswalk: ## LIVE admin: load a subject-links CSV (FILE=<csv>)
	@$(SANDBOX_GUARD); \
	  if [ -z "$(FILE)" ]; then echo "usage: make crosswalk FILE=curation/subject-links/bmovie.csv"; exit 2; fi; \
	  uv run --frozen python -m asteroidea_infra.curate crosswalk --file "$(FILE)"

studies: ## LIVE admin: make studies and study_datasets equal the CSVs (FILE=<studies.csv>)
	@$(SANDBOX_GUARD); \
	  if [ -z "$(FILE)" ]; then echo "usage: make studies FILE=curation/studies/studies.csv"; exit 2; fi; \
	  uv run --frozen python -m asteroidea_infra.curate studies --file "$(FILE)"

tiers: ## LIVE admin: set datasets.tier from a CSV (FILE=<tiers.csv>)
	@$(SANDBOX_GUARD); \
	  if [ -z "$(FILE)" ]; then echo "usage: make tiers FILE=curation/tiers/tiers.csv"; exit 2; fi; \
	  uv run --frozen python -m asteroidea_infra.curate tiers --file "$(FILE)"

reclaim: ## LIVE admin: re-send stopped sessions to their queue (DRY=1 lists only)
	@$(SANDBOX_GUARD); \
	  uv run --frozen python -m asteroidea_infra.reclaim $(if $(filter 1,$(DRY)),--dry-run,) $(if $(SESSION),--session "$(SESSION)",) $(if $(filter 1,$(FORCE)),--force,)

reextract: ## LIVE admin: re-run the extractors over catalog assets (DRY=1 lists only)
	@$(SANDBOX_GUARD); \
	  uv run --frozen python -m asteroidea_infra.reextract $(if $(DATASET),--dataset "$(DATASET)",) $(if $(ASSET),--asset "$(ASSET)",) $(if $(FORMATS),--formats "$(FORMATS)",) $(if $(filter 1,$(DRY)),--dry-run,) $(if $(filter 1,$(FORCE)),--force,)

artifacts-backfill: ## LIVE admin: fill author_email, data_key and index objects on old results (DRY=1 lists only)
	@$(SANDBOX_GUARD); \
	  uv run --frozen python -m asteroidea_infra.artifacts_backfill $(if $(filter 1,$(DRY)),--dry-run,)

summaries: ## LIVE admin: queue a summary rebuild per dataset, the backfill (DATASET=<d> for one)
	@$(SANDBOX_GUARD); \
	  uv run --frozen python -m asteroidea_infra.summaries $(if $(DATASET),--dataset "$(DATASET)",)

notes: ## LIVE admin: print the open app-dev notes, newest first, as markdown
	@$(SANDBOX_GUARD); \
	  uv run --frozen python -m asteroidea_infra.notes

notes-done: ## LIVE admin: mark one app-dev note handled (ID=<note id>; refuses an unknown id)
	@$(SANDBOX_GUARD); \
	  if [ -z "$(ID)" ]; then echo "usage: make notes-done ID=<note id>"; exit 2; fi; \
	  uv run --frozen python -m asteroidea_infra.notes --done "$(ID)"

live-iceberg-check: ## LIVE admin: append, read and add-column probe of the catalog
	@$(SANDBOX_GUARD); \
	  uv run --frozen python -m asteroidea_infra.iceberg_check --profile "$$ASTEROIDEA_PROFILE"

IMAGE_MAX_BYTES ?= 1200000000

# Adds <repository>:<git rev-parse HEAD> beside :local, so `make resource-budget` reads an image built at this commit and
# never one built at another (scripts/gates/rf-images.sh). A tree with uncommitted tracked changes is not tagged:
# the image would not be HEAD's. Argument: the docker repository name.
TAG_IMAGE_SHA = sh -c 'repo=$$1; sha=$$(git rev-parse HEAD) || exit 1; \
  if [ -n "$$(git status --porcelain --untracked-files=no)" ]; then \
    echo "tree has uncommitted changes: $$repo:local is not tagged with HEAD, so make resource-budget will not use it"; \
  else docker tag "$$repo:local" "$$repo:$$sha" && echo "tagged $$repo:$$sha"; fi' tag-image

commit-image: ## Build the commit Lambda image and check its size; needs docker
	@if ! docker info >/dev/null 2>&1; then \
	    echo "refusing: the docker daemon is not running (start Docker, then re-run make commit-image)"; exit 3; fi
	docker build --platform linux/arm64 -f ingest/Dockerfile -t asteroidea-commit:local .
	@size=$$(docker image inspect -f '{{.Size}}' asteroidea-commit:local); \
	  echo "image size (bytes): $$size (limit $(IMAGE_MAX_BYTES))"; \
	  if [ "$$size" -gt "$(IMAGE_MAX_BYTES)" ]; then \
	    echo "STOP: image is $$size bytes, over the $(IMAGE_MAX_BYTES) limit"; exit 1; fi

extract-image: ## Build the extract Lambda image and check its size; needs docker
	@if ! docker info >/dev/null 2>&1; then \
	    echo "refusing: the docker daemon is not running (start Docker, then re-run make extract-image)"; exit 3; fi
	docker build --platform linux/arm64 -f ingest/Dockerfile.extract -t asteroidea-extract:local .
	@$(TAG_IMAGE_SHA) asteroidea-extract
	@size=$$(docker image inspect -f '{{.Size}}' asteroidea-extract:local); \
	  echo "image size (bytes): $$size (limit $(IMAGE_MAX_BYTES))"; \
	  if [ "$$size" -gt "$(IMAGE_MAX_BYTES)" ]; then \
	    echo "STOP: image is $$size bytes, over the $(IMAGE_MAX_BYTES) limit"; exit 1; fi
	uv run --frozen pytest -m docker infra/tests/test_extract_image.py

compute-image: ## Build the compute Lambda image and check its size; needs docker
	@if ! docker info >/dev/null 2>&1; then \
	    echo "refusing: the docker daemon is not running (start Docker, then re-run make compute-image)"; exit 3; fi
	docker build --platform linux/arm64 -f ingest/Dockerfile.compute -t asteroidea-compute:local .
	@$(TAG_IMAGE_SHA) asteroidea-compute
	@size=$$(docker image inspect -f '{{.Size}}' asteroidea-compute:local); \
	  echo "image size (bytes): $$size (limit $(IMAGE_MAX_BYTES))"; \
	  if [ "$$size" -gt "$(IMAGE_MAX_BYTES)" ]; then \
	    echo "STOP: image is $$size bytes, over the $(IMAGE_MAX_BYTES) limit"; exit 1; fi
	uv run --frozen pytest -m docker infra/tests/test_compute_image.py

resource-budget: ## Offline: check the resource budget (bundle, images, rates latency)
	@sh scripts/gates/rf-budget.sh

DIR ?= $(HOME)/.config/asteroidea

researcher: ## LIVE: create a researcher (EMAIL=<e>), write platform.json, build the SDK wheel
	@test -n "$(EMAIL)" || { echo "usage: make researcher EMAIL=<address> [DIR=<dir>] [RESEND=1]"; exit 2; }
	@$(SANDBOX_GUARD); \
	  uv run --frozen python -m asteroidea_infra.admin researcher "$(EMAIL)" --dir "$(DIR)" $(if $(RESEND),--resend)
	$(MAKE) sdk-wheel

researcher-add: researcher ## LIVE: alias of researcher

curator: ## LIVE: add a researcher to the curators group (EMAIL=<e>)
	@test -n "$(EMAIL)" || { echo "usage: make curator EMAIL=<address>"; exit 2; }
	@$(SANDBOX_GUARD); \
	  uv run --frozen python -m asteroidea_infra.admin curator "$(EMAIL)"

sdk-config: ## LIVE: print the SDK platform.json
	@$(SANDBOX_GUARD); \
	  uv run --frozen python -m asteroidea_infra.admin config

live: ## LIVE: run one live test file (T=tests/live/<file>)
	@test -n "$(T)" || { echo "usage: make live T=tests/live/<file>"; exit 2; }
	@$(SANDBOX_GUARD); \
	  ASTEROIDEA_LIVE=1 uv run --frozen pytest -rP "$(T)"

live-first-hour: ## LIVE: the first-hour acceptance, R0 to R6 (HEADLESS=1 runs without a human)
	@$(SANDBOX_GUARD); \
	  JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION=1 uv run --frozen python -m asteroidea_infra.first_hour $(if $(filter 1,$(HEADLESS)),--headless,)

live-analysis: ## LIVE: the six-step SDK analysis in a fresh venv (HEADLESS=1)
	@$(SANDBOX_GUARD); \
	  JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION=1 uv run --frozen python -m asteroidea_infra.analysis_check $(if $(filter 1,$(HEADLESS)),--headless,)

notebook: ## Open JupyterLab on a notebook (NOTEBOOK=exemplar or showcase, default exemplar; NOTEBOOK_PORT, default 8888)
	@mkdir -p .tools
	@if [ ! -f notebooks/$(NOTEBOOK).ipynb ]; then \
	    echo "no notebook notebooks/$(NOTEBOOK).ipynb; NOTEBOOK is exemplar or showcase"; exit 2; fi
	@if [ -f .tools/notebook.pid ] && kill -0 $$(cat .tools/notebook.pid) 2>/dev/null; then \
	    echo "already running (pid $$(cat .tools/notebook.pid)); run make notebook-kill"; exit 1; fi
	JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION=1 uv run --frozen python -m asteroidea_infra.notebook prepare
	@(exec nohup .tools/notebook-venv/bin/jupyter lab notebooks/$(NOTEBOOK).ipynb --port=$(NOTEBOOK_PORT) --ServerApp.port_retries=0 $(NOTEBOOK_ARGS) </dev/null >.tools/notebook.log 2>&1) & \
	  pid=$$!; echo $$pid > .tools/notebook.pid; \
	  echo "starting JupyterLab (pid $$pid) on port $(NOTEBOOK_PORT)"; \
	  i=0; while [ $$i -lt 120 ]; do \
	    if ! kill -0 $$pid 2>/dev/null; then \
	      echo "jupyter exited:"; cat .tools/notebook.log; rm -f .tools/notebook.pid; exit 1; fi; \
	    if curl -fs -o /dev/null http://localhost:$(NOTEBOOK_PORT)/api; then \
	      grep -m1 'http://localhost:$(NOTEBOOK_PORT)/' .tools/notebook.log; \
	      echo "serving http://localhost:$(NOTEBOOK_PORT) (make notebook-kill stops it)"; exit 0; fi; \
	    sleep 0.5; i=$$((i+1)); done; \
	  echo "jupyter not ready after 60 s:"; cat .tools/notebook.log; kill $$pid; rm -f .tools/notebook.pid; exit 1

notebook-kill: ## Stop the JupyterLab server
	@if [ ! -f .tools/notebook.pid ]; then echo "notebook server not running"; exit 0; fi; \
	  pid=$$(cat .tools/notebook.pid); \
	  if kill -0 $$pid 2>/dev/null && ps -p $$pid -o command= | grep -q jupyter; then \
	    kill $$pid && echo "stopped notebook server (pid $$pid)"; \
	  else echo "notebook server not running (stale pid file for $$pid)"; fi; \
	  rm -f .tools/notebook.pid

live-notebook: ## LIVE: run a notebook headless through the SDK, writing the executed copy and HTML (NOTEBOOK=exemplar or showcase, default exemplar; AS=me uses your own saved sign-in, no test researcher)
	@$(SANDBOX_GUARD); \
	  JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION=1 uv run --frozen python -m asteroidea_infra.notebook run --notebook $(NOTEBOOK) $(if $(AS),--as $(AS))

live-hosted-setup: ## LIVE: the outsider's first hour headless (PASS=2 for the second pass)
	@$(SANDBOX_GUARD); \
	  JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION=1 uv run --frozen python -m asteroidea_infra.hosted_setup_check run $(or $(PASS),1)

live-research-app: ## LIVE: drive the hosted research app headless (PASS=1 is R0-R8; PASS=2 is R9-R13)
	@$(SANDBOX_GUARD); \
	  JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION=1 uv run --frozen python -m asteroidea_infra.research_app_check run $(or $(PASS),1) $(if $(NO_PROMPT),--no-prompt)

live-route-timing: ## LIVE, read-only: time the presign and label routes
	@$(SANDBOX_GUARD); \
	  JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION=1 uv run --frozen python -m asteroidea_infra.route_timing run

live-web-smoke: ## LIVE: visit every page of the hosted app headless; run after web-publish
	@$(SANDBOX_GUARD); \
	  test -d web/node_modules || { echo "missing web/node_modules: run make web-install"; exit 1; }; \
	  JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION=1 uv run --frozen python -m asteroidea_infra.web_smoke run

live-ux-playtest: ## LIVE: the UX play-test, headless then guided (NO_PROMPT=1 skips the prompts)
	@$(SANDBOX_GUARD); \
	  test -d web/node_modules || { echo "missing web/node_modules: run make web-install"; exit 1; }; \
	  JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION=1 uv run --frozen python -m asteroidea_infra.ux_playtest run $(if $(NO_PROMPT),--no-prompt)

web-publish: ## LIVE: build the web app with the deployed ids, upload it to site/ with the public site/platform.json, and the SDK wheel, notebook and manifest.json to handout/ (site bucket), invalidate CloudFront; PASS/FAIL per step
	@$(SANDBOX_GUARD); \
	  test -d web/node_modules || { echo "missing web/node_modules: run make web-install"; exit 1; }; \
	  uv run --frozen python -m asteroidea_infra.publish

sdk-wheel: ## Build the SDK wheel into dist/
	uv build --package asteroidea --wheel --out-dir dist

help-check: ## Fail when a target lacks help text
	@a=$$(mktemp); b=$$(mktemp); \
	  $(MAKE) --no-print-directory help > $$a; \
	  awk '{print $$1}' $$a | sort > $$a.names; \
	  grep -E '^[a-zA-Z0-9_-]+:' Makefile | grep -v ':=' | sed 's/:.*//' | sort > $$b; \
	  if diff $$a.names $$b; then echo "help-check: every target documented"; rc=0; \
	  else echo "help-check: targets on '>' lines lack help text"; rc=1; fi; \
	  rm -f $$a $$a.names $$b; exit $$rc

version-sync: ## Write VERSION into every package manifest, then regenerate uv.lock and web/package-lock.json
	uv run --frozen python -m asteroidea_infra.version_sync
	uv lock
	npm install --package-lock-only --ignore-scripts --no-audit --no-fund --prefix web

harvest: ## Harvest DANDI and OpenNeuro into web/public/index.json (network)
	@mkdir -p web/public
	uv run --frozen python -m asteroidea_services.harvest --out web/public/index.json $(if $(FORCE),--force,)

peek: ## Peek into an NWB file over HTTP (DANDISET=, VERSION=, ASSET=; network)
	@if [ -z "$(DANDISET)" ] || [ -z "$(VERSION)" ] || [ -z "$(ASSET)" ]; then \
	    echo "usage: make peek DANDISET=<id> VERSION=<version> ASSET=<uuid>"; exit 1; fi
	uv run --frozen python -m asteroidea_services.peek --dandiset "$(DANDISET)" --version "$(VERSION)" --asset "$(ASSET)"

serve: ## Serve the docs on DOCS_PORT (default 3000)
	@test -x $(MDBOOK) || { echo "missing $(MDBOOK): run make tools"; exit 1; }
	@if [ -f .tools/serve.pid ] && kill -0 $$(cat .tools/serve.pid) 2>/dev/null; then \
	    echo "already running (pid $$(cat .tools/serve.pid)); run make kill"; exit 1; fi
	@nohup $(MDBOOK) serve docs --hostname 127.0.0.1 --port $(DOCS_PORT) </dev/null >.tools/serve.log 2>&1 & \
	  pid=$$!; echo $$pid > .tools/serve.pid; \
	  echo "starting mdbook serve (pid $$pid) on port $(DOCS_PORT)"; \
	  i=0; while [ $$i -lt 40 ]; do \
	    if ! kill -0 $$pid 2>/dev/null; then \
	      echo "mdbook serve exited:"; cat .tools/serve.log; rm -f .tools/serve.pid; exit 1; fi; \
	    if grep -q 'Serving on' .tools/serve.log && curl -fs -o /dev/null http://127.0.0.1:$(DOCS_PORT)/; then \
	      echo "serving http://localhost:$(DOCS_PORT) (make kill stops it)"; exit 0; fi; \
	    sleep 0.5; i=$$((i+1)); done; \
	  echo "mdbook serve not ready after 20 s:"; cat .tools/serve.log; kill $$pid; rm -f .tools/serve.pid; exit 1

kill: ## Stop the docs, web and notebook servers
	@if [ ! -f .tools/serve.pid ]; then echo "not running"; exit 0; fi; \
	  pid=$$(cat .tools/serve.pid); \
	  if kill -0 $$pid 2>/dev/null && ps -p $$pid -o command= | grep -q mdbook; then \
	    kill $$pid && echo "stopped mdbook serve (pid $$pid)"; \
	  else echo "not running (stale pid file for $$pid)"; fi; \
	  rm -f .tools/serve.pid
	@$(MAKE) --no-print-directory web-kill
	@$(MAKE) --no-print-directory notebook-kill

clean: ## Remove virtual environments, build output and caches
	@$(MAKE) --no-print-directory kill
	rm -rf .venv .tools docs/book cdk.out .ruff_cache .pytest_cache $(WEB_DIRS)
	find . -name __pycache__ -not -path './.git/*' -prune -exec rm -rf {} +

rebuild: ## Clean, then reinstall the tools and the workspace
	@$(MAKE) --no-print-directory clean
	@$(MAKE) --no-print-directory tools
	@$(MAKE) --no-print-directory install


.PHONY: api-docs
api-docs: ## Regenerate the route table in docs/src/api.md
	uv run --frozen python -m asteroidea_infra.api_docs

.PHONY: architecture
architecture: ## Regenerate the architecture SVGs in docs/src/architecture
	uv run --frozen python -m asteroidea_infra.architecture

gate-docs: ## Docs gate: stale scope phrases; offline
	@sh scripts/gates/docs.sh

.PHONY: gate-templates
gate-templates: ## Synthesized templates equal BASE's (default origin/main) apart from asset hashes
	@BASE="$(or $(BASE),origin/main)" sh scripts/gates/templates.sh

gate-suppressions: ## Fail on any lint or type suppression not on the allowlist
	@sh scripts/suppression-gate.sh

gate-doctor: ## make doctor under four stub node versions
	@sh scripts/gates/doctor.sh

gate-deploy-docker: ## make deploy refuses when the docker daemon is down; offline
	@sh scripts/gates/deploy-docker.sh

gate-live: ## Harvest counts and peek bytes against the live APIs (network)
	@sh scripts/gates/live.sh

web-install: ## Install the web app's pinned npm packages (npm ci --prefix web)
	npm ci --prefix web

web: ## Start the Vite dev server on WEB_PORT (default 5173); PID in .tools/web.pid
	@test -d web/node_modules || { echo "missing web/node_modules: run make web-install"; exit 1; }
	@mkdir -p .tools
	@if [ -f .tools/web.pid ] && kill -0 $$(cat .tools/web.pid) 2>/dev/null; then \
	    echo "already running (pid $$(cat .tools/web.pid)); run make web-kill"; exit 1; fi
	@(cd web && WEB_PORT=$(WEB_PORT) exec nohup ./node_modules/.bin/vite </dev/null >../.tools/web.log 2>&1) & \
	  pid=$$!; echo $$pid > .tools/web.pid; \
	  echo "starting vite dev server (pid $$pid) on port $(WEB_PORT)"; \
	  i=0; while [ $$i -lt 60 ]; do \
	    if ! kill -0 $$pid 2>/dev/null; then \
	      echo "vite exited:"; cat .tools/web.log; rm -f .tools/web.pid; exit 1; fi; \
	    if curl -fs -o /dev/null http://localhost:$(WEB_PORT)/; then \
	      echo "serving http://localhost:$(WEB_PORT) (make web-kill stops it)"; exit 0; fi; \
	    sleep 0.5; i=$$((i+1)); done; \
	  echo "vite not ready after 30 s:"; cat .tools/web.log; kill $$pid; rm -f .tools/web.pid; exit 1

web-kill: ## Stop the web server started by make web
	@if [ ! -f .tools/web.pid ]; then echo "web server not running"; exit 0; fi; \
	  pid=$$(cat .tools/web.pid); \
	  if kill -0 $$pid 2>/dev/null && ps -p $$pid -o command= | grep -q vite; then \
	    kill $$pid && echo "stopped web server (pid $$pid)"; \
	  else echo "web server not running (stale pid file for $$pid)"; fi; \
	  rm -f .tools/web.pid

web-check: ## Web gates: biome ci, tsc --noEmit, vitest, vite build, bundle size
	@test -d web/node_modules || { echo "missing web/node_modules: run make web-install"; exit 1; }
	cd web && ./node_modules/.bin/biome ci .
	cd web && ./node_modules/.bin/tsc --noEmit
	cd web && ./node_modules/.bin/vitest run
	cd web && ./node_modules/.bin/vite build
	cd web && node scripts/bundle-size.mjs


web-e2e: ## Playwright e2e, headless 1440x900; E2E_LIVE=1 drives the running make web server
	@test -d web/node_modules || { echo "missing web/node_modules: run make web-install"; exit 1; }
	@if ! ls -d .tools/ms-playwright/chromium* >/dev/null 2>&1; then \
	    cd web && PLAYWRIGHT_BROWSERS_PATH=$(PLAYWRIGHT_BROWSERS_PATH) ./node_modules/.bin/playwright install chromium; fi
	cd web && WEB_PORT=$(WEB_PORT) PLAYWRIGHT_BROWSERS_PATH=$(PLAYWRIGHT_BROWSERS_PATH) ./node_modules/.bin/playwright test
