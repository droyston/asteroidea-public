# ADR-0002: Stateful/stateless stack split (BaseStack, DataStack, ServicesStack with NestedStacks)

Status: Provisional
Date: 2026-10-22

## Context and Problem Statement

asteroidea's AWS infrastructure will be written with the AWS CDK in Python. CDK groups resources into
stacks, and each stack deploys as one CloudFormation stack. A NestedStack is synthesized to its own
template and deployed as a resource of its parent stack; references between parent and nested stack
become CloudFormation parameters and outputs. The platform needs nested stacks linked to a single root
stack: shared resources in the root, and one nested stack per major subsystem.

Research found real limits in nested stacks:

- A nested stack is not listed by `cdk list` and cannot be deployed alone with `cdk deploy`.
- Security-posture changes are shown before deploy for top-level stacks only, not for nested stacks.
- A reported CDK issue says `cdk diff --security-only` misses IAM roles added in nested stacks. No source read says that `cdk diff` shows changes inside nested stacks.
- `cdk deploy --parameters` does not reach nested stacks.

The question is how to shape the stacks so that the data, which must never be lost, is protected and
deployable on its own, while keeping a per-subsystem structure.

## Considered Options

- Stateful/stateless split: three top-level stacks, with the services stack's subsystems as NestedStacks
- One root stack with NestedStacks only (the initial shape)
- Top-level stacks only, with no NestedStacks

## Decision Outcome

Chosen option: "Stateful/stateless split", because it keeps every stateful resource in its own
termination-protected top-level stack, while the stateless services keep the nested-stack structure.

The three stacks, as the deployment view lays them out:

- **BaseStack:** the network (a VPC over two Availability Zones) and shared resources.
- **DataStack**, termination-protected: the raw-data bucket, the quarantine bucket, Iceberg table
  storage, the upload-session table, the Cognito user pool and identity pool, and a cost alarm.
- **ServicesStack:** NestedStacks for Ingest, Api and Web.

### Consequences

- Good: a services change deploys in a different stack from the stateful resources, so its rollback
  does not touch them, and DataStack's termination protection guards it against an accidental stack
  deletion.
- Good: each top-level stack can be listed, diffed and deployed on its own.
- Bad: the nested-stack limits above apply to everything inside ServicesStack. Ingest, Api and Web
  cannot be deployed alone; they deploy only as part of ServicesStack.
- Bad: IAM and other security changes inside Ingest, Api or Web may not appear in the pre-deploy
  security preview. A suggested workaround is `cdk deploy --method=prepare-change-set` and reading the
  nested change sets in the CloudFormation console.
- Bad: rollback coupling. Because the nested stacks deploy as one ServicesStack, a broken Web change
  could roll back a good Ingest change; this is not yet measured.
- Bad: references between top-level stacks become CloudFormation exports. CDK documents a mechanism
  for avoiding the "deadly embrace", in which an export cannot be removed while another stack still
  uses it.
- Neutral: each template holds up to 500 resources, and one deployment operation across a nested
  hierarchy holds up to 2,500. Neither limit is near at MVP scale.

### Provisional: nested-stack visibility and rollback coupling

The split itself is settled. What remains to measure is whether nested stacks hide changes and couple
rollbacks badly enough to matter.

- **Question:** NestedStack change visibility and rollback coupling.
- **Deciding metric:** `cdk diff` shows the resources inside nested stacks; the IAM preview covers
  them; a failing Web nested stack does not roll back Ingest.
- **If it fails:** if changes are not shown or the stacks are coupled, the tension is reported and
  nothing is restructured without a new decision.
- **When:** not yet measured.

## Pros and Cons of the Options

### Stateful/stateless split

- Good: protects data from service rollbacks, and keeps per-subsystem structure for the services.
- Bad: inherits the nested-stack visibility and rollback limits inside ServicesStack.

### One root stack with NestedStacks only

- Good: matches the initial shape exactly.
- Bad: rejected because nested stacks cannot be deployed alone, and stateful data would roll back
  with the services.

### Top-level stacks only

- Good: every stack can be listed, diffed, previewed and deployed alone; this is the recommended
  shape, grouped in a CDK `Stage`.
- Bad: not chosen; the split was chosen instead.

## More Information

- Related: [ADR-0005](0005-deidentified-only-quarantine.md) (buckets in DataStack),
  [ADR-0007](0007-s3-cloudfront-spa.md) (the Web nested stack),
  [ADR-0011](0011-vpc-nat-on-demand.md) (the network in BaseStack).

## Update (2026-10): first deployed shape

The first deployed shape had two of the three stacks (the VPC was dropped). The status and the
decision are unchanged:

- **DataStack**, termination-protected: the raw, warehouse and Athena-results buckets, the sessions table
  with its `by_owner` index, the Glue database and the Athena workgroup. The quarantine bucket and a cost
  alarm are not in it (the only budget is the management-account budget).
- **ServicesStack** with three nested stacks: Identity, Ingest and Api. There is no Web nested stack; the web
  app runs on a localhost dev server, and hosting came later.
- **BaseStack and the VPC are not built yet.** Every Lambda runs outside a VPC.
- **One placement differs from this ADR.** The Cognito user pool, clients and identity pool are in the
  Identity nested stack of ServicesStack with removal policy DESTROY, not in DataStack as the text above
  says. `make destroy-services` therefore deletes the user pool and every researcher account. A later
  change moves them to DataStack with removal policy RETAIN, as this ADR places them.
- **The nested-stack measurement did not run.** Change visibility and rollback coupling are still
  unmeasured; the Status stays Provisional.

## Update (2026-10): identity moves to DataStack

The Cognito user pool, its domain prefix `asteroidea-id-<account>`, the identity pool and ResearcherRole moved from ServicesStack to DataStack, with the pool on `RemovalPolicy.RETAIN`, as this ADR said from the start. `make destroy-services` no longer deletes researcher accounts. DataStack exports the identity values as `asteroidea-<OutputName>`; ServicesStack imports them and re-outputs every earlier output name. The pool was recreated under a new domain prefix (a new pool, not a migration). Status stays Provisional: the nested-stack measurement still has not run.

## Update (2026-10): web hosting in DataStack

The Web placement changed: the Web construct (`WebHosting`: site bucket, CloudFront distribution) is built in DataStack, not in a ServicesStack nested stack. The hosted URL is fixed by the distribution, and replacing it would break every invitation and callback, so it sits in the termination-protected stack. DataStack also gained the artifacts bucket and the dataset-owners table. A DataStack-first deploy and a lock check precede ServicesStack (see the [Deployment view](../07-deployment.md)). Status stays Provisional.

## Current state

The decision was three top-level stacks (BaseStack, DataStack, ServicesStack) with nested stacks inside ServicesStack. The reason is that stateful resources sit in a termination-protected stack that a services rollback cannot touch. It holds in part. DataStack and ServicesStack exist, and ServicesStack holds nested stacks. BaseStack was never built, because the platform has no VPC. Nothing replaced it; treat the two-stack shape as the current design.
