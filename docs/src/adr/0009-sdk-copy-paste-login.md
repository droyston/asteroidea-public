# ADR-0009: SDK headless sign-in by copy-paste login URL (PKCE)

Status: Accepted
Date: 2026-10-22

## Context and Problem Statement

Researchers use the Python SDK from laptops, but also from remote servers over SSH, where no browser
runs on the machine that runs the SDK. Researchers sign in through a Cognito user pool (see
[ADR-0004](0004-cognito-researchers-identity-center-admins.md)). Cognito documents these OAuth grants:
authorization code with PKCE, implicit, client credentials and refresh. No device-code grant was found
on that page. The device-code flow is what `aws sso login` uses to sign in without a local browser.

PKCE (Proof Key for Code Exchange) lets a public client, one that cannot keep a secret, complete the
authorization-code flow safely: the client sends a hash of a one-time secret with the sign-in request
and proves it holds the secret when it exchanges the code for tokens. Cognito supports PKCE for public app clients.

The question is how the SDK signs a researcher in when there may be no browser on the same machine.

## Considered Options

- Copy-paste login URL: the SDK prints a sign-in URL, the researcher completes sign-in in any browser
  and pastes the result back into the SDK, which finishes the PKCE exchange
- Device-code grant, as `aws sso login` does
- Local loopback redirect only: the SDK opens a browser and listens on a local port for the redirect

## Decision Outcome

Chosen option: "Copy-paste login URL", because it works with Cognito's authorization-code flow with
PKCE and does not need a browser on the machine that runs the SDK.

The sign-in journey, as the runtime view lays it out:

1. The SDK prints a sign-in URL.
2. The researcher opens it in a browser on any machine and signs in.
3. The researcher copies the result back into the SDK, which exchanges it for tokens with the PKCE
   secret.
4. The SDK caches the tokens and refreshes them when they expire.
5. The SDK exchanges the sign-in for short-lived, scoped AWS credentials through a Cognito identity
   pool (see [ADR-0017](0017-identity-pool-data-access.md)).

Exactly what the researcher copies back (the full redirect URL or a code shown on a page) depends on
which redirects Cognito's managed login accepts. A sign-in test settled it (see the update below).

### Consequences

- Good: one sign-in mechanism for laptops, SSH sessions and notebooks.
- Good: it serves the SDK principle of one-call sign-in.
- Bad: one manual copy-and-paste step at each full sign-in. Cognito's refresh grant avoids it until the
  refresh token expires.
- Bad: the SDK stores tokens on the researcher's disk, so the token file's permissions matter.

### Provisional: sign-in test

The test belonged to the initial implementation.

- **Question:** Cognito PKCE copy-paste login in the SDK.
- **Deciding metric:** sign-in succeeds from an SSH session with no browser; tokens refresh after
  expiry; the stored tokens have safe file permissions.
- **If it fails:** if Cognito's managed login rejects the out-of-band redirect, the mechanism changes,
  and the decision for a copy-paste login URL stands.

## Pros and Cons of the Options

### Copy-paste login URL

- Good: needs only Cognito's documented authorization-code flow with PKCE; works with no local browser.
- Bad: a manual paste step; depends on the redirect Cognito's managed login accepts.

### Device-code grant

- Good: the familiar `aws sso login` experience: open a URL anywhere, enter a short code.
- Bad: Cognito user pools document no device-code grant. The platform would have to build one.

### Local loopback redirect only

- Good: no manual paste on a laptop; the browser hands the code straight to the SDK.
- Bad: not chosen; it needs a browser on the same machine as the SDK, which an SSH session to a remote
  server does not have.

## More Information

- Related: [ADR-0004](0004-cognito-researchers-identity-center-admins.md),
  [ADR-0017](0017-identity-pool-data-access.md).

## Update (2026-10): sign-in test passed

The sign-in test passed, and the Status moved from Provisional to Accepted. The service checks ran
against a sandbox account.

- **The redirect is accepted.** Cognito's managed login accepts `redirect_uri=http://localhost:8765/callback`:
  the authorize request answered 302 to `/login`, not `redirect_mismatch`. What the researcher pastes back is
  therefore the full redirect address; the SDK also accepts the bare code and checks `state`.
- **The copy-paste exchange works.** A tester signed in at the printed address on another device and pasted
  the redirect address; the code exchanged for ID, access and refresh tokens with the PKCE verifier.
- **Refresh.** The refresh-token grant returned a new access token carrying `cognito:groups` =
  `researchers`. The forced refresh of the AWS credentials gave a new AccessKeyId.
- **Token file.** The cache is mode 0600 inside a directory of mode 0700.
- **Identity pool.** The refreshed ID token exchanged for identity-pool credentials that resolve to
  ResearcherRole.
- **Guided acceptance.** `make acceptance` runs this sign-in as its step 2 (see [Quality requirements](../10-quality.md#first-hour-script-q9)).

Still unmeasured, and not a reason to hold the Status: the `sdk` client's OAuth refresh after real token
expiry (the forced refresh used a stand-in).

## Current state

The SDK signs in on any machine by printing a login URL, then taking back the pasted redirect address (authorization code with PKCE). The reason is that it needs no browser on the machine that runs the SDK. It holds: `sdk/src/asteroidea/_auth.py` implements the copy-paste flow with a token cache and refresh. Nothing replaced it.
