# Set up your own sandbox account: guarded setup and runbook

A template for an AWS sandbox that asteroidea deploys into, behind two guardrails: a service control policy
(SCP) that confines the sandbox account, and a monthly budget that applies a stop SCP when spend crosses a
limit. Replace every `<placeholder>` with your own value before use.

The JSON files in this directory are templates for the policies and budget you deploy. They are data, not a
Python package. Do not reformat them.

| File | What it is |
|---|---|
| `guardrails.json` | SCP `AsteroideaGuardrails` |
| `budget-stop.json` | SCP `AsteroideaBudgetStop` (the stop policy) |
| `action-role-trust.json`, `action-role-policy.json` | Trust and permissions of `AsteroideaBudgetsActionRole` |
| `budget.json`, `notifications.json` | Budget `asteroidea-sandbox-monthly` and its alerts |

## Ids to fill in

| Placeholder | Meaning |
|---|---|
| `<org-id>` | Your AWS Organization id |
| `<management-account-id>` | The management (payer) account |
| `<sandbox-account-id>` | The member account `asteroidea-sandbox` that holds the stacks |
| `<scp-id>` | The id of the `AsteroideaBudgetStop` SCP after you create it (unattached) |
| `<alert-email>` | Where budget alerts go |
| `<admin-user>` | The Identity Center user who gets the developer permission set |

Create these once, in the management account: an organization, an OU `Sandbox`, the member account
`asteroidea-sandbox`, the SCP `AsteroideaGuardrails` attached to the OU, and the SCP `AsteroideaBudgetStop`
left unattached.

## Identity Center

- Enable an organization instance of IAM Identity Center in your primary region (us-east-1). Do not add
  replica regions.
- Create a permission set `AsteroideaSandboxDeveloper` (AdministratorAccess, PT8H) and assign it only to
  `<admin-user>` on `<sandbox-account-id>`. Assign no permission set on the management account.
- Keep an `asteroidea-sandbox` AWS CLI profile that signs in through this permission set. The live make
  targets read the profile name and region from the sandbox env file (`~/.config/asteroidea/sandbox.env`).

## Raw bucket

- Only the two ingest roles can write or delete in the raw bucket; hand cleanup needs a policy change. The policy is in `storage.deny_raw_writes_except`.


### Removing a file that must not be kept (PHI removal)

Every raw object version is locked in governance mode for `RAW_RETENTION_DAYS` (default and sandbox value 1).
The bucket policy refuses every delete except CommitRole's, and no role holds
`s3:BypassGovernanceRetention`. So a delete needs a reviewed change, in this order. Run it on one file at a
time.


1. **Name the file.** Take the `dataset`, `path`, `s3_key` and `s3_version_id` from the `assets` row (the SDK's
   `asteroidea._query.query(auth, dataset=...)` rows carry them). No command lists the rows to remove as one
   report yet; read them by hand.
2. **Add a temporary remediation role.** Edit `infra/src/asteroidea_infra/data_stack.py` and `storage.py`:
   create one role that your SSO user can assume, give it `s3:DeleteObjectVersion` and
   `s3:BypassGovernanceRetention` on `raw/*`, and add its ARN to the `deleter` list of
   `deny_raw_writes_except` (that statement uses `ArnNotEquals`, so a second ARN is allowed). There is no
   switch or flag for this today; it is a code change on a branch, reviewed like any other. Check it with
   `make diff STACKS=DataStack SANDBOX_TEST=1`, then `make deploy STACKS=DataStack SANDBOX_TEST=1`.
3. **Delete the version with the bypass header**, as that role:

       aws s3api delete-object --bucket <raw bucket> --key <s3_key> --version-id <s3_version_id> \
         --bypass-governance-retention --profile <remediation profile>

   Confirm with `aws s3api list-object-versions --bucket <raw bucket> --prefix <s3_key>`. The landing copy
   expires by itself (3 days); delete it too when it is still there.
4. **Remove the file's catalog rows.** Delete its row in `assets` and its row in `asset_headers` (key
   `asset_id`). There is no command for this yet: the repo has no catalog delete, and `make catalog-init`
   never drops anything. Write the delete against the Iceberg tables with PyIceberg as an administrator, and keep the
   script with the incident notes.
5. **Expire the Iceberg snapshots** that still hold the rows, so time travel cannot read them. There is no
   command for this yet (PyIceberg's snapshot expiry, run by an administrator). Also remove orphaned data files.
6. **List the artifacts built from the file.** Each artifact's `provenance.json` has a `sources` list that
   names assets. There is no command that lists the
   artifacts whose `sources` name an asset yet; until there is, scan the `provenance.json` files by hand and
   tell their owners.
7. **Remove the temporary role.** Revert step 2 and run `make deploy STACKS=DataStack SANDBOX_TEST=1` a second
   time. Confirm with `aws s3api get-bucket-policy` that the delete list is CommitRole alone again.

## Budget and stop

- Budget `asteroidea-sandbox-monthly`: $10 monthly, filtered to LINKED_ACCOUNT `<sandbox-account-id>`. Alerts at
  ACTUAL 3, 8 and 10 and FORECASTED 10, to `<alert-email>`.
- Automatic budget action: APPLY_SCP_POLICY at ACTUAL $10 with `<scp-id>`, run by role
  `AsteroideaBudgetsActionRole`. Its status should read STANDBY.

## CDK and local config

- Bootstrap CDK in the sandbox account with the default AdministratorAccess execution policy.
- `~/.aws/config` defaults to the developer role and holds no root profile.

## Checks after setup

- `aws sts get-caller-identity` shows the `AsteroideaSandboxDeveloper` assumed role.
- Budgets, `sso-admin`, `iam create-user`, organization calls and DynamoDB outside us-east-1 are denied by
  `AsteroideaGuardrails`; S3 list and DynamoDB in us-east-1 work.
- Stop drill: attach `AsteroideaBudgetStop` to the OU; `create-bucket` is then denied while `list-buckets`
  and `delete-bucket` work. Detach it afterwards; the action returns to STANDBY.

## Break-glass

Root cannot assume IAM roles, so the OrganizationAccountAccessRole exemption does not help it. The
break-glass is: root detaches the SCP. Sign in with `aws login` into a temporary profile, and remove the
profile afterwards.

## Known limits

- The budget action has not been observed to fire on real spend.
- Budget data lags 8-12 h, so the stop can fire that late after spend crosses $10.
- A full `cdk destroy` under the stop SCP is untested.

## Steps run by hand

These need root of the management account (`<management-account-id>`); run them by hand. Check each
command against `aws organizations help` before use.

Re-authenticate as root to change the budget or the SCPs:

    aws login --profile asteroidea-root-tmp
    # sign in as the management account ROOT user in the browser; confirm:
    aws sts get-caller-identity --profile asteroidea-root-tmp

Detach the stop (after a stop fires, or to end a drill):

    aws organizations detach-policy --policy-id <scp-id> --target-id <sandbox-ou-id> \
      --profile asteroidea-root-tmp

Then remove the `asteroidea-root-tmp` profile from `~/.aws/config` and confirm the budget action is back
in STANDBY.

## Budget placement

The only budget is the management-account budget `asteroidea-sandbox-monthly`. The sandbox SCP denies
`budgets:*`, so the stack cannot deploy one.

## Destroying ServicesStack

`make destroy-services` destroys ServicesStack only. The Cognito user pool, its clients and the identity pool
are in the Identity construct of DataStack (`data_stack.py :: DataStack`, `Identity(`) and the user pool has
removal policy RETAIN (`identity.py :: Identity`, `RemovalPolicy.RETAIN`), so researcher accounts survive the
command.
