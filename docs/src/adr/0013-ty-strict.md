# ADR-0013: ty strict, version pinned

Status: Accepted
Date: 2026-10-22

## Context and Problem Statement

The codebase is empty, so this is the cheapest moment to make it strictly typed: there is nothing to
migrate. The project prefers Rust-based tools where they are mature. ty (from Astral, the makers of uv
and ruff) is a Rust type checker, but it is pre-1.0: it makes no stability claim, and the version installed is 0.0.91. The question is which type checker gates the code, how
strict it is, and how strictness bends when untyped scientific libraries arrive.

## Considered Options

- ty, every rule an error, exact version pin, with a written escape-hatch policy.
- pyright or mypy in strict mode as the gate, with ty optional.
- ty with its default rule levels.
- ty with a version range instead of an exact pin.

## Decision Outcome

Chosen option: "ty, every rule an error, exact version pin", because it gives the strictest gate on an
empty codebase from a Rust tool, and the exact pin stops a pre-1.0 release from changing the gate
under a developer.

### The strict settings

All of these are in the root `pyproject.toml`:

- ty: `[tool.ty.rules] all = "error"`, and in `[tool.ty.analysis]`
  `strict-equality-semantics = true` and `strict-generic-narrowing = true`; the dev group pins
  `ty==0.0.91`.
- ruff: `select = ["ALL"]` with `target-version = "py314"`, ignoring only `COM812`, `ISC001`,
  `D203`, `D213` and `CPY001` (the last because the root `LICENSE` covers the repository).
- pytest: `--strict-markers`, `--strict-config` and `--import-mode=importlib`; `xfail_strict = true`;
  `filterwarnings = ["error"]`.

ty has no single "strict" switch and no rule that requires annotations. Required annotations therefore come from ruff's
`ANN` rules, which `select = ["ALL"]` turns on.

### The escape-hatch policy

The strict settings relax in these ways only, without a further decision:

1. Test directories (`tests/*` and `*/tests/*`) may ignore `S101`, `ANN`, `D1`, `PLR2004` and
   `INP001` through ruff's `per-file-ignores`. This is already configured.
2. A third-party warning is filtered per module in pytest's `filterwarnings`, each entry with its
   reason.
3. A ty override is scoped to a path, each with its reason.
4. Anything wider needs an explicit decision.

The policy exists so that untyped scientific libraries (pynwb, neo, boto3 and others named in later
stages) do not turn every strict-rule collision into an open question.

### Consequences

- Good: type errors fail `make check` from the first line of real code.
- Bad: ty is pre-1.0 with every rule an error, so a ty upgrade can break `make check`. A ty bump goes
  in its own pull request.
- Bad: strictness will meet untyped third-party libraries as dependencies are added; the escape-hatch policy is
  the bounded answer, and a reviewer checks that every exception names its origin.

## Pros and Cons of the Options

### ty strict, exact pin (chosen)

- Good: Rust-based, fast, strict from day one; matches the uv and ruff toolchain.
- Bad: pre-1.0; no annotation rule of its own.

### pyright or mypy strict as the gate

- Good: mature, with stable strict modes; the tooling research recommended this because ty is
  pre-1.0.
- Rejected: ty with strict settings was chosen instead.

### ty at default rule levels

- Bad: any rule left below error level does not fail the gate, and on an empty codebase strictness
  costs nothing.

### ty with a version range

- Bad: a pre-1.0 minor release could change rule behaviour and break the gate without any change to
  the code.

## More Information

- Related: [ADR-0001](0001-rust-tooling-clean-mvp.md) (Rust tooling where mature),
  [ADR-0012](0012-uv-workspace-monorepo.md) (where the dev tools are pinned).
- Gate: `make typecheck` runs `uv run --frozen ty check`; `make check` runs it with the other gates.

## Current state

The ty type checker runs with every rule an error, at an exact pinned version. The reason is the strictest gate from a Rust tool, protected from pre-1.0 changes by the pin. It holds: `pyproject.toml` pins `ty==0.0.91`, and `make typecheck` is part of `make check`. Overrides are scoped to a path with a reason. Nothing replaced it.
