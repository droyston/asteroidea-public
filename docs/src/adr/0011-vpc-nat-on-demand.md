# ADR-0011: Classic public/private VPC; NAT gateways added on demand

Status: Accepted
Date: 2026-10-22

## Context and Problem Statement

The platform base needs a network that later workloads can join: a container task, a database, or a
Lambda that must reach a private resource. But every component (Lambda,
S3, DynamoDB, Athena, Cognito, CloudFront, API Gateway) runs without a VPC, and a Lambda outside a
VPC already has internet access. A NAT
gateway costs $0.045 per hour plus $0.045 per GB, about $33 a month each before data.
Two NAT gateways in an empty network would be the whole idle cost of an otherwise near-zero-idle
design. The question is how to build the classic network shape now without paying for NAT before
anything uses it, and without a later change that replaces the subnets.

## Considered Options

- Classic VPC over 2 AZs with explicit `PUBLIC` and `PRIVATE_WITH_EGRESS` subnets, 0 NAT gateways
  behind a context flag, plus S3 and DynamoDB gateway endpoints.
- Classic VPC with one NAT gateway per AZ from day one.
- A VPC with 0 NAT using CDK's default subnet layout for that case.
- A lean VPC (isolated subnets and endpoints only), or no VPC at all.

## Decision Outcome

Chosen option: "Classic VPC with explicit `PUBLIC` and `PRIVATE_WITH_EGRESS` subnets and 0 NAT
gateways", because it builds the classic network shape while keeping the idle cost at zero
until a workload needs egress.

The subnet configuration must be written out explicitly. When `natGateways` is 0 and no subnet
configuration is given, CDK uses `DEFAULT_SUBNETS_NO_NAT`, which is a `PUBLIC` subnet plus a
`PRIVATE_ISOLATED` subnet. CDK names the default subnets by
type: `Isolated` for `PRIVATE_ISOLATED` and `Private` for `PRIVATE_WITH_EGRESS`. Turning NAT
on later would switch the default to `PRIVATE_WITH_EGRESS`, rename the subnet group, and so change
the subnets' construct paths. CloudFormation would then replace the subnets, and everything placed in
them, at the moment the first in-VPC workload arrives. Declaring `PRIVATE_WITH_EGRESS` from day one avoids
that. CDK accepts it with 0 NAT gateways: its NAT-count check throws only when private subnets are
configured with no NAT and none of them is `PRIVATE_WITH_EGRESS`.

### Consequences

- Good: about $0 idle network cost with NAT off; S3 and DynamoDB gateway endpoints are free.
- Good: enabling NAT later is one context-flag change that adds NAT gateways and routes, not a subnet
  replacement.
- Bad: until NAT is on, the `PRIVATE_WITH_EGRESS` subnets have no internet egress despite their
  name. A workload placed there reaches only S3 and DynamoDB through the gateway endpoints. A Lambda
  attached to the VPC loses internet access unless NAT or endpoints are added. So Lambdas run outside the VPC unless they need it.
- Bad: Athena and other AWS APIs would need interface endpoints (about $7.30 a month per AZ each) or
  NAT for an in-VPC workload.
- When NAT is enabled: about $33 a month per NAT gateway before data.
- No measurement is outstanding for this decision, so it is Accepted. A synth test
  asserts `PRIVATE_WITH_EGRESS` subnets with 0 NAT gateways.

## Pros and Cons of the Options

### Explicit public + egress subnets, 0 NAT (chosen)

- Good: classic shape now, zero idle cost, no replacement when NAT arrives.
- Bad: the subnet type name promises egress that does not exist until NAT is on; the deployment view
  says so.

### One NAT gateway per AZ from day one

- Rejected: an empty network would be the whole idle cost. At 2 AZs that is about $66 a month for no tenant.

### 0 NAT with CDK's default subnets (`PRIVATE_ISOLATED`)

- Rejected: enabling NAT later changes the default subnet type and name, which replaces the subnets
  (see the CDK source behaviour above).

### Lean VPC without the classic shape, or no VPC

- Rejected: the classic public/private shape was preferred.

## More Information

- Deployment: [Deployment view](../07-deployment.md) (BaseStack).
- Related: [ADR-0002](0002-stateful-stateless-stack-split.md) (BaseStack holds the network).
- The idle-cost target that this decision serves is in [Quality requirements](../10-quality.md).

## Update (2026-10): no VPC

The Status and the decision are unchanged. BaseStack and the VPC were left out. No network
is created, so the synth test of `PRIVATE_WITH_EGRESS` subnets with 0 NAT gateways (mentioned under Consequences) was not
written. Every Lambda runs outside a VPC and reaches S3, DynamoDB, Glue and Athena over the internet
path that a Lambda has by default, so no NAT gateway was needed. BaseStack, the VPC, the explicit subnet
configuration and that synth test are planned but not built. The chosen option stands as written.

The stack code holds no VPC.

## Current state

The decision was a classic public/private VPC with no NAT gateways until a workload needs egress. The reason is zero idle cost. It does not hold as built: the CDK app defines no VPC, and no Lambda runs inside one. The platform needed no network layer, since every function reaches AWS services over their public endpoints. Nothing replaced it. The record stays as a design for a future in-VPC workload.
