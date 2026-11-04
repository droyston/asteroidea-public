# ADR-0019: Dataset browser: a local survey tool for DANDI and OpenNeuro, built before the platform deployment

Status: Accepted
Date: 2026-10-23

## Context and Problem Statement

The project needs to survey open neuroscience archives before choosing exemplar datasets for the platform: search DANDI and OpenNeuro together, filter by species, recording type, spike units, LFP, data
standard, licence and size, open one dataset to see its files, and keep a short pick list for later
ingest. The first page of the platform's web frontend can be this tool, so it should not be throwaway
work. The platform has no web code yet.

## Considered Options

- A local web app in a new `web/` directory over a harvested JSON index, plus a terminal peek command
  (chosen).
- A hosted app on Amplify or another host now. Rejected: hosting stays on S3 and CloudFront
  ([ADR-0007](0007-s3-cloudfront-spa.md) stands), and the choice may be reopened later.
- Notebooks and ad hoc scripts only. Rejected: the need is faceted browsing and a pick list.
- Wait for the planned frontend work and build the browser there. Rejected: its tooling is pulled forward.

## Decision Outcome

Chosen option: the local web app and the peek command, because they let candidates be loaded and examined
before locking exemplars, without any AWS work.

- **Location and stack.** `web/` starts here as a React, Vite and TypeScript app with
  TanStack Table, filtering a harvested JSON index in the browser. The frontend tooling planned for later (Biome, strict
  `tsc`, Vitest) comes forward; sign-in and hosting are still to be added.
- **Harvester.** A standard-library Python job in `services/` writes the index as
  `web/public/index.json`. It is gated by ruff, ty and pytest like other workspace code. A later change can
  run it nightly and write to S3.
- **Archives.** DANDI and OpenNeuro, behind one source interface other archives can join later.
  Only published dandisets are listed; drafts are not.
- **Depth.** The index holds one summary per dataset. The detail pane fetches the live file list on
  demand and lets individual files be picked. The browser never reads inside NWB files.
- **Species and LFP.** An OpenNeuro dataset with no species stated gets the value
  "not stated". `has_lfp` is true only for an NWB LFP object; iEEG is a separate recording-type value.
- **Pick list.** Picks live in the browser's local storage and export to, and
  import from, a versioned `targets.json`. Each pick carries archive, dataset id, version, an optional
  file path and a note. Checksums, content URLs and licence are not in the pick list; the ingest design adds what
  ingest needs.
- **Peek.** `make peek` in `services/` prints whether an NWB file over HTTP holds Units, LFP and
  ElectricalSeries, with their shapes, by range reads. It brings h5py and remfile, the first third-party
  dependencies in `services/`. The depth rule stands: no NWB reads in the browser.
- **Manifest.** The pick list is the target manifest. Its provenance fields name which archive, dataset,
  version and file a pick came from.
- **Order.** This tool is built before the platform and does not displace it.

### Consequences

- Good: candidate datasets can be loaded and examined before the platform build starts.
- Good: `web/` and its tooling exist when the full frontend begins; it then adds auth and hosting, not a toolchain.
- Good: the context chapter can say what is in scope: browse and pick, no pull.
- Bad: the repository gains a Node toolchain and third-party Python dependencies before any platform
  code, which widens `make check`.
- Bad: the index is a local file produced on demand; it goes stale until a later change schedules it.
- Bad: the archives' public APIs are outside the platform's control; the harvester counts and reports
  records it cannot parse instead of hiding them.
- Update (2026-10): the dataset browser at `/` is unchanged and needs no sign-in. The platform build adds an opt-in "Sign in" control to the app shell; when signed in, a "My uploads" panel lists the researcher's upload sessions. The control is hidden when the build has no auth configuration, so fixture mode and the existing end-to-end specs are unchanged. This decision is unchanged.

## Pros and Cons of the Options

### Local web app over a harvested index, plus peek (chosen)

- Good: no hosting, no credentials, nothing to deploy; the same app moves into `web/` for the full frontend.
- Bad: two more toolchains in `make check`.

### Hosted app on Amplify now

- Rejected: S3 and CloudFront were chosen for hosting, and Amplify's CDK construct is alpha.

### Notebooks and scripts only

- Good: least code.
- Rejected: no faceted browsing, no pick list for ingest.

### Wait for the full frontend

- Rejected: the exemplar choice for the platform cannot wait.

## More Information

- DANDI has 464 published dandisets.
- Related: [ADR-0007](0007-s3-cloudfront-spa.md) (hosting, unchanged),
  [ADR-0006](0006-bids-nwb-native.md) (BIDS and NWB), [ADR-0012](0012-uv-workspace-monorepo.md) (web
  outside uv).

## Update (2026-10): curator tool and catalog view

The frontend's role widens: the app is a curator tool plus a read-only catalog view (datasets, assets, one asset, served by CatalogFunction). The opt-in sign-in is superseded when the auth env is present: the hosted app signs in first. Without the auth env the control stays hidden, as before. The rule that tokens, the PKCE verifier and state live in `sessionStorage`, never `localStorage`, stays. This decision is otherwise unchanged.

## Update (2026-10): the research frontend

The dataset-browser app grows into the research frontend: a router with bookmarkable addresses, an in-house token set and a minimal kit (`web/src/kit/`: Shell, Nav, Button, Notice, Tabs, KeyValue, Table), catalog depth pages (dataset, subject, file), and on a file page the Units, LFP, Events and Image tabs. The earlier rule stands: the browser reads no NWB or NIfTI internals and does no analysis arithmetic; it draws what the API returns on in-house canvas plots. NiiVue 0.69.0 is the first on-demand third-party dependency: it sits in its own chunk (`viewer`, 314.4 KB gzip, budget 392.5 KB), loaded only when the Image tab opens, and `bundle-size.mjs` budgets every lazy chunk. The sign-in rules of the earlier updates stand (tokens in `sessionStorage`). Rationale and options are in [ADR-0020](0020-research-frontend-analysis.md).

## Current state

The dataset browser is a local React and Vite app over a harvested index of DANDI and OpenNeuro, plus a command that peeks into one remote NWB file. The reason is to examine candidate datasets before choosing exemplars, without any AWS work. It holds: `make harvest`, `make peek` and `web/` exist and are used. The web app has since grown into the research frontend described in ADR-0020, and a hosted copy is published with `make web-publish`.
