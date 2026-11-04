# ADR-0004: Cognito user pool for researchers; Identity Center for admins only

Status: Accepted
Date: 2026-10-22

## Context and Problem Statement

The platform has two kinds of users:

- **Researchers** use the web app and a Python SDK to upload, find, read and analyse data. They are
  capable programmers but want simple interfaces.
- **Admins** deploy and operate the platform through the AWS CLI with single sign-on (SSO) and the AWS
  console.

AWS offers two identity services that fit. Amazon Cognito user pools sign users in to applications.
AWS IAM Identity Center signs people in to AWS accounts, and each permission set creates an IAM role
that only Identity Center users can assume. The question is which population uses which service, and for what.

## Considered Options

- **Identity Center everywhere**: admins and researchers in one identity source. The SDK signs in with
  `aws sso login` and calls an IAM-authorized API with SigV4; the web app uses Cognito federated to
  Identity Center by SAML. Not chosen.
- **Cognito for researchers**: a separate Cognito user pool for researchers' web and SDK access, with
  admins on Identity Center.
- **Leave open for a time-boxed experiment**. Not chosen.

The decision is Cognito for researchers' frontend and API access, and Identity Center exclusively for
CDK system control. That narrows the second option to the form chosen below.

## Decision Outcome

Chosen option: "Cognito for researchers", with Identity Center exclusively for admin and CDK
control, because the people who run the platform and the people who use it sign in
through separate systems, and only admins can change infrastructure.

- Researchers sign in to the web app with the OAuth authorization-code flow with PKCE, which Cognito
  supports for public clients such as a browser app or an SDK.
- Cognito groups map to IAM roles in a Cognito identity pool, which issues researchers short-lived AWS
  credentials for direct reads.
- Admins use `aws configure sso` and Identity Center permission sets for the CDK, the CLI and the
  console. Identity Center credentials use the OIDC device-code flow and refresh automatically.

### Consequences

- Good: researchers never hold credentials that can deploy or change infrastructure.
- Good: Cognito's free tier covers 10,000 monthly active users signing in directly with the user pool.
- Bad: two identity systems to run. A person who is both a researcher and an admin has two sign-ins.
- Bad: Cognito user pools document no device-code grant, the flow that `aws sso login` uses for
  sign-in without a local browser. The SDK therefore needs its own headless sign-in;
  see [ADR-0009](0009-sdk-copy-paste-login.md).
- Bad: Identity Center permission sets are manual setup steps for the account owner before the first deploy.
- Neutral: both Cognito pools live in DataStack.

## Pros and Cons of the Options

### Identity Center everywhere

- Good: one AWS-native sign-in; the SDK could sign requests with AWS credentials from `aws sso login`,
  as an earlier survey recommended.
- Good: one set of user accounts.
- Bad: not chosen. Researchers would receive AWS account sign-ins, which this decision reserves for
  system control.
- Bad: the web app would need Cognito federated to Identity Center by SAML. The research could not
  verify the SAML federation steps, and Cognito's free tier covers only 50 monthly active users who
  sign in through SAML or OIDC federation.

### Cognito for researchers

- Good: a hard line between users and operators.
- Bad: two identity systems; the SDK needs a headless sign-in that Cognito does not provide out of the
  box.

### Leave open for a time-boxed experiment

- Good: would measure the SDK sign-in before deciding.
- Bad: not chosen; the question was answered directly.

## More Information

- Related: [ADR-0009](0009-sdk-copy-paste-login.md), [ADR-0017](0017-identity-pool-data-access.md),
  [ADR-0007](0007-s3-cloudfront-spa.md).

## Update (2026-10): curators

Curators are Identity Center administrators who run `make import`, `make crosswalk` and `make reclaim` with sandbox-profile credentials. No curator group exists in the Cognito pool, which keeps only `researchers`. Status stays Accepted.

## Update (2026-10): the curators group

A third Cognito group, `curators`, gates web-started imports. A curator is also a researcher (the user pool keeps both groups on one user). No role ARN is set on `curators`, so the identity-pool mapping stays unambiguous: a two-group user keeps identity-pool credentials (checked by `tests/live/test_rf_curators.py`). `make curator EMAIL=<e>` adds a user to the group. The routes `POST /imports`, `GET /imports`, `GET /imports/{run_id}` refuse a non-curator with 403 through `uploads_api.group_refusal(claims, group)`; the web app reads the group from the access token for display only and the server enforces it. A run takes at most 50 files; a larger run is refused. Researchers keep the curator tool's earlier behaviour; the Imports page is the web route to the same import code. Otherwise unchanged.

## Current state

Researchers sign in through a Cognito user pool, and admins control the deployment only through IAM Identity Center. The reason is two separate sign-in systems, with only admins able to change infrastructure. It holds. DataStack owns the user pool (retained on stack deletion) with SDK and web clients, and `infra/account/README.md` documents the Identity Center setup for admin access. Nothing replaced it.
