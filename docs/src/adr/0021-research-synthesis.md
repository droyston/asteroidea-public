# ADR-0021: Research synthesis: sessions, recipe-driven events and readable result records

Status: Accepted
Date: 2026-10-26

## Context and Problem Statement

The app shows public datasets one file at a time. A researcher who wants to know how every recorded unit behaved around the events of one recording session has no page or SDK object for the session, no common way to turn a dataset's trial tables, events files and stimulus files into aligned events, and no way to see which data a saved result came from or who made it. It is implemented and was validated live (see Validation).

## Considered Options

- Derive the session from the catalog; one recipe file per dataset and one shared resolver; a richer result record that every researcher can read; defaults as an exploratory screen; delivered in two parts (chosen).
- A session table, a catalog table of resolved events and the four lineage tables in the on-demand part (rejected; the lineage tables move to the precomputed part).
- Private results with explicit sharing (rejected).
- Per-dataset code for event alignment, as the rates route does for 000623 today.

## Decision Outcome

Chosen option: the first, with these parts.

- **The session is a derived triple.** A session is (dataset, subject, session label), with `_` when the path has no `ses-` label, taken from the columns `assets` already carries. There is no new table. The SDK gains `sessions_index`, `load_session` and `Session` (`lfp`, `rates`, `tuning`, `band_power`, `to_pynapple`); the web gains a session page at `/data/<dataset>/<subject>/<session>` with Units, Events, Tuning, Files and Results tabs. `GET /catalog/datasets/{dataset}/assets` takes a `session` filter (`_` for no label).
- **Recipe-file alignment events with one shared resolver.** A per-dataset recipe at `sdk/src/asteroidea/recipes/<dataset>.json` names event sources (NWB intervals, BIDS events, stimulus files, labels) and their attributes. One pure function in the shared analysis module resolves a recipe into typed events. The compute image and the SDK wheel carry the same recipe files, so the Lambda and the researcher read one definition (extends ADR-0020's one-implementation rule). `GET /compute/events` lists the resolved events. 000623 needs a per-dataset `lfp_clock_offset_s: 10` because its LFP clock runs 10 s ahead of the trials.
- **Result record version 2, readable by all researchers, with the author's email.** The provenance record gains the run (function, parameters, defaults version, SDK version), every table snapshot read, files read and results used. Every researcher can read and download every result and sees the author's email; only the author edits or withdraws it, and a result that another result used cannot be deleted. The SDK sends its ID token on create so the Lambda reads the `email` claim; a version 1 create (shipped wheels) takes any token and stores a null email, which `make artifacts-backfill` fills. The records stay as JSON in the artifacts bucket in the on-demand part, with `index/by-id`, `by-input` and `by-time` objects the PlatformFunction writes, and load into catalog tables unchanged in the precomputed part. The SDK `run()` context records every table snapshot, file version, query and result a piece of work read, and the defaults and recipe versions used.
- **Analysis defaults as an exploratory screen.** A versioned defaults file holds bins, windows, bands, wavelet cycles, baseline and test settings: a per-dataset block from the paper where one exists (000623 uses raw rates), else convention. Per-unit statistics carry a raw p, a Benjamini-Hochberg q and the area-proportion test, and every page says which defaults and baseline it used and that the result is an exploratory screen. `GET /compute/rates` with `align=event:<type>` fills its window from the defaults when it is omitted and returns `defaults`, `responsive` and `label`; `GET /compute/tuning` returns the tuning screen; `units=all` and ranges lift the unit list, bounded at 152 units by the 5 MiB body. Event band power is an SDK and notebook feature in the on-demand part.
- **Two parts.** The on-demand part computes on request. The precomputed part (outlined only) adds precomputed summaries, staleness, the lineage tables and web band power.

### Consequences

- Good: one definition of "event" for the web, the SDK and the compute Lambda; no schema change in the on-demand part unless the second dataset needs one.
- Good: a result shows its inputs and its author, and a file shows the results that used it.
- Bad: every researcher sees every author's email, copied into each record (a privacy risk, [Risks](../11-risks.md#data-and-access)); a recipe change needs a new wheel and an image deploy; the artifacts bucket carries index objects the PlatformFunction writes.
- Bad: the statistics are a screen over 44 to 152 units with permutation tests; a reader can take a screened unit as a finding. The label is the mitigation, not a gate.
- Open: `make artifacts-backfill` is denied by the artifacts bucket policy (the backfill moves into PlatformFunction), and `load_session` (61 s), the first save (32 s) and the session page header (23 to 29 s) are slow (Validation).
- Measurements that refined this decision: the 000623 paper settings and trial columns with the LFP clock offset; a second dataset (dandi 001775: no units outside `units/`, timestamps-based LFP); band power in the notebook (229 s); pynapple 0.11.4 on Python 3.14; the 152-unit bound. The live test accepted the ID token, so the `AdminGetUser` fallback is not needed.

## Pros and Cons of the Options

### Derived session, recipes, readable records (chosen)

- Good: small change to the catalog; the design can be checked against two datasets.
- Bad: sessions without a `ses-` label share the `_` key within a subject.

### Session table, events table and lineage tables now

- Good: SQL over everything.
- Bad: three schema changes and a re-extract before any researcher sees a session.

### Private results

- Good: no email exposure.
- Bad: contradicts the decision that every researcher can read every result.

## Validation

The JWT authorizer accepts the ID token. Timings on the deployed platform: `load_session` 61.02 s, route rates 3.8 to 8.6 s, route tuning 5.1 to 6.7 s, first save 32.03 s, other calls 0.4 to 1.2 s.

- The session page renders 44 units on the 000623 session and 82 units with 2 event types on dandi-001775.
- Not validated: `make artifacts-backfill DRY=1` is refused with AccessDenied by `DenyArtifactAccessExceptPlatform`; it is deferred to a PlatformFunction path.

## More Information

Related: [ADR-0018](0018-artifact-lake-boundary.md), [ADR-0020](0020-research-frontend-analysis.md).
