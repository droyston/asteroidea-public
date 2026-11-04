# ADR-0001: Rust-based tooling where mature; clean-MVP scope

Status: Accepted
Date: 2026-10-22

## Context and Problem Statement

asteroidea is a cloud data platform on AWS for multimodal neuroscience research data, such as MRI
images and electrophysiology recordings. The initial project brief named several mechanisms: DuckDB, RDS
or Glue for the catalog, Amplify or Fargate for the frontend, and nested CDK stacks for the
infrastructure. These were only starting points. Rust-based tools are preferred wherever possible,
the tool choice is open, and only the research-data domain and a clean minimum-viable-product (MVP)
scope are fixed. Each of those starting-point mechanisms therefore becomes a separate decision.

Two questions follow. How far does the Rust preference reach when the Rust option is immature? And
what does "clean MVP" leave out?

## Considered Options

- Rust-based tools where they are mature; AWS-managed services and established Python libraries elsewhere
- Rust-based tools everywhere, including the data layer
- A managed data platform (Snowflake or Databricks)
- A transformation layer (dbt or SQLMesh) from the start
- The conventional Python toolchain this project grew from

## Decision Outcome

Chosen option: "Rust-based tools where they are mature; AWS-managed services and established Python
libraries elsewhere", because it follows the Rust preference and keeps pre-1.0 data
infrastructure off the critical path of an MVP.

What this means today:

- **Developer tooling is Rust-based.** uv packages the workspace, ruff formats and lints, ty checks
  types, prek runs the git hooks and mdBook builds this document. Each of these five binaries, as
  installed for this repository, contains Rust compiler paths; two control binaries that are not Rust
  (`/bin/ls` and `git`) contain none.
- **Every tool is pinned.** ruff 0.17.2, ty 0.0.91 and prek 0.5.7 are locked in `uv.lock`; the
  workspace requires uv `>=0.13.5,<0.14`; mdBook 0.5.4 installs from a checksummed release binary
  into `.tools/bin`.
- **The data path uses mature engines.** Athena, an AWS-managed service, runs corpus-wide SQL. The
  SDK reads with DuckDB and with Polars, which are among the Rust-based libraries. See
  [ADR-0003](0003-iceberg-athena-catalog.md).
- **Clean MVP.** The platform is built up from a small base: the repository and this architecture document first,
  then sign-in, upload, query and read, each added only as a plan names it.

### Consequences

- Good: one fast, consistent toolchain, and every version is pinned exactly, so a fresh clone gets
  the same tools as every other machine.
- Good: the data layer stays on services with no idle cost and on libraries with working write paths,
  instead of pre-1.0 Rust catalogs.
- Bad: several of the chosen tools are themselves pre-1.0. ty is at 0.0.91 and runs with every rule
  as an error, so a ty upgrade can break `make check`; the mitigation is the exact pin, with
  upgrades in their own pull request. prek describes
  itself as "pretty new".
- Bad: ruff runs with every rule family selected, which includes CPY001, the rule that demands a
  copyright notice at the top of each file. CPY001 is ignored repository-wide, because the root
  `LICENSE` file covers the whole repository.
- Bad: `prek install` writes the shared `.git/hooks/pre-commit` file, which blocks commits in git
  worktrees that have no `prek.toml`. `make install` therefore runs
  `prek install --allow-missing-config`.
- Neutral: "where mature" is a judgment made per decision. Where a later ADR picks a non-Rust option,
  it gives the maturity or fit reason, as ADR-0003 does for Athena and PyIceberg.
- Neutral: the clean-MVP scope defers options that were surveyed: Snowflake, Databricks,
  Lakekeeper, Apache Polaris, DuckLake, Lance, TileDB, moving NWB to Zarr, Redshift, and dbt or SQLMesh.

## Pros and Cons of the Options

### Rust-based tools where mature; AWS-managed services and Python libraries elsewhere

- Good: follows the Rust preference wherever a Rust tool is ready.
- Good: uv and ruff each replace several older tools (see the last option).
- Bad: the data path is mixed-language (Athena, DuckDB, PyIceberg, Polars).

### Rust-based tools everywhere, including the data layer

- Good: the fullest reading of the Rust preference.
- Bad: the Rust data options are pre-1.0 or need a Postgres server: DataFusion 55, iceberg-rust and
  pyiceberg-core 0.10, and Lakekeeper 0.14, whose only catalog backend is Postgres 15 or newer.
- Bad: Polars 2.0 reads Iceberg tables through PyIceberg, a Python library, and its native reader is
  flagged unstable, so even the Polars read path is not Rust-only.

### A managed data platform (Snowflake or Databricks)

- Good: mature SQL engines with large ecosystems.
- Bad: rejected for cost and lock-in at this scale. The prices behind that judgment are third-party
  figures, because the official pricing pages did not render.
- Bad: both are overbuilt for metadata-sized tables over files stored in S3.

### A transformation layer (dbt or SQLMesh) from the start

- Good: a standard way to manage derived SQL models.
- Bad: rejected because ingestion metadata needs no transform layer; ingestion writes the tables
  directly and SQL views cover the rest.
- Bad: dbt v2 ("Fusion") ships under a proprietary licence; dbt Core 1.x stays Apache-2.0, with 1.13
  as its last minor release.

### The conventional Python toolchain

- Good: familiar to the maintainers.
- Bad: the earlier setup uses Poetry, and black, isort and flake8 pinned in 2021. The project aims to modernize and prefers Rust tools. In this
  repository uv replaces Poetry, and ruff runs both `make format` and `make lint`.

## More Information

- Related: [ADR-0003](0003-iceberg-athena-catalog.md), [ADR-0008](0008-arc42-adr-mdbook.md),
  [ADR-0012](0012-uv-workspace-monorepo.md), [ADR-0013](0013-ty-strict.md).

## Current state

The project prefers Rust-based tools where they are mature, and uses AWS-managed services and established Python libraries elsewhere. The reason is a fast, pinned toolchain without pre-1.0 data infrastructure on the critical path. This still holds: uv, ruff, ty, prek and mdBook are pinned and in use, and the data path uses Athena, DuckDB and Polars. The clean-MVP part is finished: the CDK app, the ingest code, the SDK and the web app are all built. Nothing replaced this decision.
