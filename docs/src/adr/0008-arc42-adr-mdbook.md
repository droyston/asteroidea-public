# ADR-0008: arc42 + ADRs in mdBook

Status: Accepted
Date: 2026-10-22

## Context and Problem Statement

The project needs a Software Architecture Document (SAD): the requirements and design choices that
the implementation starts from. The document needs a structure that a new contributor can navigate, a record of each decision and why it was made, and a format that lives in the repository and
builds with the rest of the tooling.

## Considered Options

- arc42 chapters plus Architecture Decision Records (ADRs), built with mdBook
- A Python documentation site (Sphinx or MkDocs) with the same content
- One free-form design document

## Decision Outcome

Chosen option: "arc42 chapters plus ADRs, built with mdBook", because arc42 gives a standard place for
each concern, ADRs record each decision with its alternatives, and mdBook is a single Rust binary (see
[ADR-0001](0001-rust-tooling-clean-mvp.md)).

- **arc42** is a template for architecture documentation with twelve sections, from introduction and
  goals to the glossary. This site has those twelve chapters,
  then the ADRs.
- **ADRs** follow MADR 4, the Markdown Architectural Decision Records template. Each ADR lists the options
  considered, with the reason each rejected one lost.
- **Statuses** are exactly one of Accepted, Provisional (a measurement is still outstanding),
  Proposed (suggested, not yet agreed) and Open (decision deferred). A status
  changes only by a dated update.
- **mdBook** 0.5.4 builds the site from `docs/`. `make tools` downloads it into `.tools/bin` and checks
  its sha256 digest; `make docs` builds and `make serve` serves the site locally.

### Consequences

- Good: the document is plain Markdown in the repository, reviewed in the same pull requests as the
  code it describes.
- Good: a SUMMARY entry for a missing chapter fails the build, because `create-missing = false` is set.
- Bad: the site uses no mdBook preprocessors, so there are no rendered diagram languages; diagrams are
  ASCII or SVG.

## Pros and Cons of the Options

### arc42 + ADRs in mdBook

- Good: standard structure, decisions with reasons, one pinned Rust binary to build it.
- Bad: no diagram preprocessor.

### A Python documentation site (Sphinx or MkDocs)

- Good: mature, with many plugins, including diagram support.
- Bad: not chosen; mdBook is the Rust-based option that the tooling preference favours (see
  ADR-0001), and a Python site generator would add documentation dependencies to the workspace.

### One free-form design document

- Good: quickest to start.
- Bad: not chosen; it gives no fixed place for each concern and no separate, dated record for each
  decision, which later work relies on to avoid reopening settled decisions.

## More Information

- Template: `docs/src/adr/template.md`.
- Related: [ADR-0001](0001-rust-tooling-clean-mvp.md).

## Current state

The architecture document is arc42 chapters plus ADRs, built with mdBook. The reason is a standard place for each concern and a single Rust binary as the builder. It holds: `make docs` builds the book with a pinned, checksummed mdBook. This decision record is itself part of that book. Nothing replaced it.
