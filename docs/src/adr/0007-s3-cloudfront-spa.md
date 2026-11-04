# ADR-0007: Frontend as S3 + CloudFront SPA

Status: Accepted
Date: 2026-10-22

## Context and Problem Statement

Researchers use a web app to sign in, upload data, browse the catalog and see query results. The
platform leaves hosting open: "hosted either amplify or fargate/other as appropriate". The app needs no server-side rendering: it calls the
platform's API and reads data with the signed-in researcher's credentials. The question is where and
how to host it.

## Considered Options

- A single-page app (SPA) as static files in S3, served by CloudFront with origin access control (OAC)
- AWS Amplify Hosting
- A server on AWS Fargate

## Decision Outcome

Chosen option: "A single-page app as static files in S3, served by CloudFront with origin access
control", because it uses stable, high-level CDK constructs and has no servers to run.

- The CDK's recommended S3 origin for CloudFront is `S3BucketOrigin.with_origin_access_control`, which
  keeps the bucket private and lets only CloudFront read it. The older origin access identity (OAI) is
  called legacy.
- The web app is the Web nested stack inside ServicesStack (see
  [ADR-0002](0002-stateful-stateless-stack-split.md)).
- Researchers sign in through the Cognito user pool with the authorization-code flow and PKCE (see
  [ADR-0004](0004-cognito-researchers-identity-center-admins.md)).

### Consequences

- Good: no servers and near-zero idle cost. CloudFront has a Free flat-rate plan at $0 covering
  1 million requests and 100 GB, and a Pro plan at $15 per month.
- Good: S3 and CloudFront with OAC are stable in `aws-cdk-lib`.
- Bad: everything the app does runs in the browser, so every protected action goes through the API or
  through the researcher's own scoped credentials; the app can hold no secrets.
- Neutral: the `web/` directory does not exist yet. It will use Biome as its formatter and linter.
- Update (2026-10): `web/` now exists, created for the dataset browser ([ADR-0019](0019-dataset-browser.md)) with Biome as its formatter and linter. It is a local dev-server app with no hosting or auth; auth and S3 + CloudFront hosting come later. This decision is unchanged.
- Update (2026-10): the existing `web/` app gains a first auth path, a minimal Cognito sign-in and a "my uploads" route, still with no hosting. S3 + CloudFront hosting and the full auth UX come later. This decision is unchanged.

## Pros and Cons of the Options

### S3 + CloudFront SPA with OAC

- Good: stable constructs, a private bucket and no servers.
- Bad: no server-side rendering, which this app does not need.

### AWS Amplify Hosting

- Good: a managed build-and-host service for web apps.
- Bad: rejected because its CDK construct is alpha. The
  module ships as the separate package `aws-cdk.aws-amplify-alpha`, and its README labels it
  experimental.
- Neutral: Amplify Hosting bills $0.15 per GB served and $0.023 per GB-month stored, with a 12-month
  free tier.

### A server on AWS Fargate

- Good: full control, including server-side rendering.
- Bad: rejected because there is no server-rendering need. Fargate was not studied further.

## More Information

- Related: [ADR-0002](0002-stateful-stateless-stack-split.md),
  [ADR-0004](0004-cognito-researchers-identity-center-admins.md).

## Update (2026-10): hosting

Hosting is now built (amending the Web placement): S3 plus CloudFront in DataStack. The app shell and the harvested public index are public static files; the shell renders sign-in first, and data comes only from signed-in API calls. No WAF is attached; a custom domain is not built. This decision is unchanged.

## Update (2026-10): router and UI kit

The SPA grows a router and a UI kit; hosting is unchanged.

- **Deep links rely on the existing 403/404 fallback.** CloudFront answers an unknown path with `index.html`, so routes such as `/catalog/dandi-000623/...` need no hosting change. No route uses `/assets/`; that prefix stays the build-output namespace. Routes live under `/files/` and `/catalog/`.
- **Presigned S3 GETs.** The browser never holds AWS credentials. `GET /catalog/assets/{asset_id}/download` returns a 300 s presigned GET pinned to the catalogued `VersionId`; the raw bucket carries one CORS rule (GET and HEAD, header `Range`, origins the hosted URL and `http://localhost:5173`). The hosted page sets no CSP that blocks it (confirmed by the live Image-tab runs).
- **Reload Notice after a publish.** `make web-publish` deletes stale `site/` keys, so a tab opened before a publish can ask for a chunk that no longer exists. `web/src/router/lazy.ts:: loadChunk` turns a failed dynamic import into the Notice "The app was updated. Reload to continue." with a Reload button. `publish.py` skips Vite's `.vite/` manifest and refuses (step `index-schema`) a `web/public/index.json` whose schema differs from `INDEX_SCHEMA`, after the stale schema-1 index reached the hosted app.

This decision is otherwise unchanged.

## Current state

The web app is a single-page app in a private S3 bucket, served by CloudFront with origin access control. The reason is stable high-level CDK constructs and no servers to run. It holds: `web_hosting.py` builds the bucket and the distribution with `S3BucketOrigin.with_origin_access_control`, and `make web-publish` uploads the built app. Nothing replaced it.
