# Constraints

These constraints are fixed. To change one, write a new decision record that supersedes the old one.

## Technical constraints

| Constraint | What it means | Decision |
|---|---|---|
| AWS, one account, one region | Every service is an AWS service. The platform runs in one account in us-east-1. A multi-account layout is out of scope. | [ADR-0002](adr/0002-stateful-stateless-stack-split.md) |
| Infrastructure as code in Python CDK | The CDK library is `aws-cdk-lib` 2.274.0, which needs Python 3.10 or newer. The CDK command line is a Node package, `aws-cdk` 2.1146.1, run through `npx`. The CDK prerequisites ask for Node 22 or newer. The repository pins Node 24. The app is two stacks, `DataStack` and `ServicesStack`, in `infra/`. | [ADR-0002](adr/0002-stateful-stateless-stack-split.md) |
| Python 3.14 everywhere | Every workspace member targets Python 3.14, and every Lambda function runs the Python 3.14 runtime. A Lambda `python3.15` runtime is in public preview only. Whether the SDK should support older Pythons is open. | [ADR-0015](adr/0015-python-3-14.md) |
| One uv workspace | One repository has four Python members (`infra/`, `services/`, `ingest/` and `sdk/`) and one lockfile, and uv enforces one `requires-python` across members. uv is pinned to at least 0.13.5 and below 0.14. The web app in `web/` (npm) and the docs in `docs/` sit outside uv. | [ADR-0012](adr/0012-uv-workspace-monorepo.md) |
| Strict static checks | ruff runs with every rule selected. ty (pinned to 0.0.91) runs with every rule set to error. The few allowed relaxations are listed in the [developer guide](developers.md). | [ADR-0013](adr/0013-ty-strict.md) |
| Rust-based tools where mature | The repository prefers Rust-based tools (uv, ruff, ty, prek, mdBook, the DuckDB and Polars engines). Some Rust data tools are before version 1.0 or need a Postgres server (DataFusion 55, iceberg-rust 0.10, Lakekeeper 0.14), so the catalog does not depend on them. | [ADR-0001](adr/0001-rust-tooling-clean-mvp.md) |
| Clean MVP | Build the smallest platform that serves the user stories: no transform layer, no warehouse, no cluster. | [ADR-0001](adr/0001-rust-tooling-clean-mvp.md) |
| Docs as arc42 and ADRs in mdBook | mdBook 0.5.4 is installed from a checksummed binary. There are no mdBook preprocessors. Diagrams are ASCII or SVG. | [ADR-0008](adr/0008-arc42-adr-mdbook.md) |


## Data and domain constraints

| Constraint | What it means | Decision |
|---|---|---|
| De-identified data only | The platform accepts only de-identified data. No automated de-identification check exists yet. The planned SDK header check and the quarantine bucket for uploads that fail a server check are not built. | [ADR-0005](adr/0005-deidentified-only-quarantine.md) |
| BIDS and NWB native | MRI data follows the Brain Imaging Data Structure (BIDS), whose entity table lists 35 entities. Electrophysiology is stored as Neurodata Without Borders (NWB) files. Vendor raw formats (Open Ephys, SpikeGLX, Intan, Blackrock) are accepted at ingest only. Who converts vendor data to NWB is open. | [ADR-0006](adr/0006-bids-nwb-native.md) |
| Raw data is read-only | Once finalized, raw data is never modified or deleted by a researcher or a service. The raw bucket is versioned, has Object Lock, and denies writes to every role except three named ingest roles. | [ADR-0005](adr/0005-deidentified-only-quarantine.md) |


## Organisational constraints

| Constraint | What it means | Decision |
|---|---|---|
| Apache-2.0 licence | The repository is licensed under Apache-2.0. The ruff copyright-header rule is off because the root licence covers every file. | [ADR-0014](adr/0014-apache-2-license.md) |
| One developer operates it | Every design choice must suit one person: few moving parts, no always-on servers to patch. | [ADR-0001](adr/0001-rust-tooling-clean-mvp.md) |
| Small, cost-sensitive organisation | Low idle cost matters more than peak throughput (quality goal 3). | [ADR-0001](adr/0001-rust-tooling-clean-mvp.md) |
| No continuous integration | Checks run locally with `make check`. | none |

