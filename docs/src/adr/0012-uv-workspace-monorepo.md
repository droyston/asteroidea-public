# ADR-0012: uv workspace monorepo (infra, services, sdk; web and docs outside uv)

Status: Accepted
Date: 2026-10-22

## Context and Problem Statement

The platform has three Python parts that change together: the CDK infrastructure app, the service
code that runs in Lambda, and the researcher SDK. A change to an upload API usually touches all
three. The repository also holds a web frontend (later) and this documentation site, neither of
which is Python. The question is how to lay out the repository so that one developer can change,
lock and test the Python parts together.

## Considered Options

- One uv workspace with three members (`infra`, `services`, `sdk`) and one lockfile; `web/` and
  `docs/` sit outside uv.
- Three independent uv projects in one repository, each with its own lockfile.
- Separate repositories for infrastructure, services and SDK.
- A Poetry project, as in the earlier Python setup the conventions came from.

## Decision Outcome

Chosen option: "One uv workspace with three members", because one lockfile and one install command
keep the three Python parts on the same dependency versions, which a one-developer platform needs.

The workspace as built: a virtual root `pyproject.toml` with
`members = ["infra", "services", "sdk"]`, the dev tools in a `dev` dependency group, and
`required-version = ">=0.13.5,<0.14"` for uv itself. The members are the distributions `asteroidea-infra`, `asteroidea-services` and
`asteroidea` (the SDK), each in a `src/` layout.
The documentation is an mdBook site under `docs/`,
installed as a pinned, checksummed binary rather than a Python package. The web frontend lives in `web/`, outside uv, with Biome as its tooling.

Update (2026-10): `web/` was created earlier than planned, for the dataset browser ([ADR-0019](0019-dataset-browser.md)),
outside uv with Biome, and `services/` now holds the dataset harvester and the NWB peek tool. The
decision (three members, `web/` and `docs/` outside uv) is unchanged.

Update (2026-10; done, see the later update below): `services/`
served both the dataset harvester and peek tool and the commit and session Lambdas, so it was split into
two members. This note changed no decision.

### Consequences

- Good: `make install` runs one `uv sync --all-packages --frozen`, and the lock check gate fails on
  any drift.
- Good: the SDK can depend on shared code in the workspace without publishing it.
- Bad: a uv workspace shares one lockfile and enforces one `requires-python` across members. The SDK cannot support an older Python than the
  services without changing this. [ADR-0015](0015-python-3-14.md) records that choice and its
  review.
- Bad: uv documents Lambda packaging of workspace members only for container images. Zip packaging still needs measuring against container packaging.
- The layout itself is Accepted; only the Lambda packaging may be refined.

## Pros and Cons of the Options

### One uv workspace (chosen)

- Good: one lock, one install, one gate.
- Bad: one Python floor for every member; Lambda packaging of members is not documented for zips.

### Three independent uv projects in one repository

- Good: each member could have its own Python floor.
- Bad: three locks that can drift apart; conflicting dependencies or per-member environments are
  uv's own stated reasons to avoid a workspace, and neither applies yet.

### Separate repositories

- Bad: a cross-cutting change becomes several pull requests to merge in order, for one developer.

### Poetry

- Bad: the earlier Poetry setup came with 2021-era pins of black, isort and flake8; the project wants a modern, Rust-based toolchain
  where mature, and uv is that.

Update (2026-10): the split landed as a fourth member, `ingest/`
(`asteroidea-ingest`, package `asteroidea_ingest`), holding the commit Lambda, the session API and the commit
image `Dockerfile`. `services/` keeps the harvester and peek tool. The members are now `infra/`, `services/`,
`ingest/` and `sdk/`; `web/` and `docs/` stay outside uv. The commit image carries no harvester-only
dependency (h5py, remfile, numpy): a gate checks the export of `asteroidea-ingest`, and the same export for
`asteroidea-services` lists h5py. `requests` stays, because pyiceberg
0.12.0 depends on it.

## More Information

- Related: [ADR-0001](0001-rust-tooling-clean-mvp.md) (tooling direction),
  [ADR-0013](0013-ty-strict.md) (type checker), [ADR-0015](0015-python-3-14.md) (the one Python).

## Update (2026-10): extraction extra

Extraction is an optional extra `extract` of `asteroidea-ingest` (pynwb 4.2.0, h5py, remfile, nibabel) with its own image, `ingest/Dockerfile.extract`, and no fifth workspace member. The commit image exports no extras and carries no pynwb, h5py or awscrt (a test pins this). The extract image is 1,061,799,673 B against a limit of 1,200,000,000 B. Status stays Accepted.

## Current state

One uv workspace with one lockfile holds the Python parts, with the web app and the docs outside uv. The reason is the same dependency versions across parts for a one-developer platform. It holds, with one change: the workspace now has four members (`infra`, `services`, `ingest`, `sdk`), where the decision named three. The web app is managed by npm in `web/`.
