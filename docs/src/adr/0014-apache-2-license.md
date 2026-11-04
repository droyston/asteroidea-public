# ADR-0014: Apache-2.0

Status: Accepted
Date: 2026-10-22

## Context and Problem Statement

The repository started with no license. Its README is written for a public audience, and the platform is meant to be usable
and readable by other research groups. Without a license, nobody outside the maintainers may legally reuse
the code. The question is which license the repository carries.

## Considered Options

- Apache License 2.0.
- MIT.
- No license (all rights reserved).

## Decision Outcome

Chosen option: "Apache License 2.0", because it is a permissive license with an explicit patent
grant, which matters for a platform that research institutions and companies may build on.

The root `LICENSE` is the Apache License, Version 2.0, January 2004, and its section 3 is the "Grant
of Patent License". The root `LICENSE` covers the whole repository, so ruff's per-file copyright-header rule
(`CPY001`) is ignored repository-wide.

### Consequences

- Good: anyone may use, change and redistribute the code, including commercially, and contributors
  grant a patent license for their contributions (Apache-2.0 section 3).
- Good: no per-file license headers to maintain.
- Bad: redistributors must keep the license and state their changes, which is a little more
  bookkeeping than the shortest permissive licenses.

## Pros and Cons of the Options

### Apache-2.0 (chosen)

- Good: permissive, with an explicit patent grant.
- Bad: longer than MIT; redistributors must state changes.

### MIT

- Good: very short and widely used.
- Bad: its text has no explicit patent grant.
- Rejected: Apache-2.0 was chosen instead.

### No license

- Bad: a public README with no license would describe code that nobody may reuse.
- Rejected: Apache-2.0 was chosen instead.

## More Information

- License text: `LICENSE` at the repository root.
- Related: [ADR-0001](0001-rust-tooling-clean-mvp.md) (clean-MVP scope),
  [ADR-0013](0013-ty-strict.md) (the strict ruff settings that `CPY001` is carved out of).

## Current state

The repository is licensed under Apache-2.0. The reason is a permissive license with an explicit patent grant. It holds: the root `LICENSE` is the Apache License, Version 2.0, and it covers the whole repository. Nothing replaced it.
