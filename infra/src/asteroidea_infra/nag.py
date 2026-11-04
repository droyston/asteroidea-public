"""cdk-nag 3 wiring: the AwsSolutions pack plus the acknowledgement list."""

from aws_cdk import Acknowledgment, App, Stack, Validations
from cdk_nag import AwsSolutionsChecks

_IAM5 = "AwsSolutions::AwsSolutions-IAM5[Resource::DataStack:ExportsOutputFnGetAttX"
_RAW = "RawBucket0C3EE094ArnD2F95F99"
_WAREHOUSE = "WarehouseBucket0E783396ArnD66BFFC9"
_ARTIFACTS = "ArtifactsBucket2AAC5544ArnD57FAE19"
_SITE = "WebSiteBucket1E6F6F45ArnD070D382"
_DERIVED = "DerivedBucket81C7992CArnF73A8B22"
_IAM5_LOCAL = "AwsSolutions::AwsSolutions-IAM5[Resource::<"
_IAM4 = "AwsSolutions::AwsSolutions-IAM4[Policy::arn:<AWS::Partition>:iam::aws:policy"

# (stack id, rule id, reason). Rule ids carry the pack prefix, e.g.
# "AwsSolutions::AwsSolutions-S1".
ACKNOWLEDGEMENTS: tuple[tuple[str, str, str], ...] = (
    (
        "DataStack",
        "AwsSolutions::AwsSolutions-S1",
        (
            "Out of scope for the single-researcher sandbox: S3 server access logs "
            "need a log bucket and have no reader. Revisit with the audit work."
        ),
    ),
    (
        "DataStack",
        "AwsSolutions::AwsSolutions-CFR1",
        ("No geo restriction: research users sign in from anywhere."),
    ),
    (
        "DataStack",
        "AwsSolutions::AwsSolutions-CFR2",
        (
            "No WAF: not in scope for this deployment; the app is behind Cognito "
            "sign-in and serves static files."
        ),
    ),
    (
        "DataStack",
        "AwsSolutions::AwsSolutions-CFR3",
        (
            "No CloudFront access logs: no reader and no log bucket in the sandbox "
            "budget. Revisit with the audit work."
        ),
    ),
    (
        "DataStack",
        "AwsSolutions::AwsSolutions-CFR4",
        (
            "The CloudFront default domain cannot set a TLS minimum; a custom domain "
            "would."
        ),
    ),
    (
        "ServicesStack",
        f"{_IAM4}/service-role/AWSLambdaBasicExecutionRole]",
        (
            "Lambda log delivery: AWSLambdaBasicExecutionRole is the minimum "
            "managed policy that lets the function write its CloudWatch Logs."
        ),
    ),
    (
        "ServicesStack",
        f"{_IAM5.removesuffix('X')}{_RAW}/landing/*]",
        (
            "CommitRole reads the verified landing version: object wildcard is "
            "limited to the landing/ prefix."
        ),
    ),
    (
        "ServicesStack",
        f"{_IAM5.removesuffix('X')}{_RAW}/raw/*]",
        (
            "Object keys under raw/ are dataset paths chosen at run time: CommitRole "
            "writes (and rolls back) them, SessionApiRole reads them, and "
            "CatalogRole presigns GetObject and GetObjectVersion on them."
        ),
    ),
    (
        "ServicesStack",
        f"{_IAM5.removesuffix('X')}{_SITE}/handout/*]",
        (
            "The handout file names (wheel, notebook, manifest) change per release: "
            "PlatformRole reads and presigns them, limited to the handout/ prefix of "
            "the site bucket."
        ),
    ),
    (
        "ServicesStack",
        f"{_IAM5.removesuffix('X')}{_ARTIFACTS}/artifacts/*]",
        (
            "Artifact keys are artifacts/<sub>/<artifact_id>/...: ids are generated "
            "per save, so PlatformRole put/get/delete is limited to the artifacts/ "
            "prefix of the artifacts bucket."
        ),
    ),
    (
        "ServicesStack",
        f"{_IAM5.removesuffix('X')}{_ARTIFACTS}/index/*]",
        (
            "Index keys are index/by-id/, index/by-input/ and index/by-time/ followed "
            "by ids generated per save: PlatformRole, their only writer, puts, gets "
            "and deletes them, limited to the index/ prefix of the artifacts bucket."
        ),
    ),
    (
        "ServicesStack",
        f"{_IAM5.removesuffix('X')}{_DERIVED}/lfp/*]",
        (
            "Derived LFP keys are lfp/<asset_id>/<s3_version_id>/<series>/...: asset "
            "versions and series paths are known only at run time, so LfpBuildRole's "
            "put and get and ComputeRole's get are limited to the lfp/ prefix of the "
            "derived bucket."
        ),
    ),
    (
        "ServicesStack",
        f"{_IAM5.removesuffix('X')}{_DERIVED}/summary/*]",
        (
            "Summary keys are summary/<dataset>/_dataset.json and "
            "summary/<dataset>/<subject>/<session>/<digest>/...: datasets, sessions "
            "and digests are known only at run time, so LfpBuildRole, the prefix's "
            "only writer, puts and gets are limited to the summary/ prefix of the "
            "derived bucket."
        ),
    ),
    (
        "ServicesStack",
        f"{_IAM5.removesuffix('X')}{_DERIVED}/summary/*/_dataset.json]",
        (
            "Dataset summaries are summary/<dataset>/_dataset.json and datasets are "
            "known only at run time: CatalogRole's read-only get is limited to that "
            "one file name under the summary/ prefix of the derived bucket."
        ),
    ),
    (
        "ServicesStack",
        f"{_IAM5.removesuffix('X')}{_DERIVED}/rows/*]",
        (
            "Re-extract row bundles are rows/<asset_id>/<run_id>/...: asset and run "
            "ids are known only at run time, so DeriveRole's put and ExtractRole's "
            "get are limited to the rows/ prefix of the derived bucket."
        ),
    ),
    (
        "ServicesStack",
        "AwsSolutions::AwsSolutions-IAM5[Resource::<DeriveFunctionE8D37858.Arn>:*]",
        (
            "The re-extract state machine invokes one function, DeriveFunction; CDK's "
            "LambdaInvoke grant adds <arn>:* for that function's own versions and "
            "aliases only."
        ),
    ),
    (
        "ServicesStack",
        "AwsSolutions::AwsSolutions-IAM5[Resource::arn:aws:s3:::openneuro.org/*]",
        (
            "ImportRole copies curator-chosen files of public OpenNeuro snapshots: "
            "object keys are dataset paths under the public openneuro.org bucket, "
            "read-only."
        ),
    ),
    (
        "ServicesStack",
        f"{_IAM5.removesuffix('X')}{_WAREHOUSE}/iceberg/*]",
        (
            "Iceberg writes new data and metadata file names per commit: CommitRole "
            "put/get is limited to the iceberg/ prefix; ImportPlanRole only reads "
            "it, for the already-imported check, and the Athena readers "
            "(CatalogRole, ComputeRole, LabelsRole, LfpBuildRole) only read it."
        ),
    ),
    (
        "ServicesStack",
        f"{_IAM5.removesuffix('X')}AthenaResultsBucket879938FAArn609FA1B4/athena/*]",
        (
            "Athena writes result files with generated names: the put/get of "
            "CatalogRole, ComputeRole, LabelsRole and LfpBuildRole is limited to the "
            "athena/ prefix of the results bucket."
        ),
    ),
    (
        "DataStack",
        f"{_IAM5_LOCAL}AthenaResultsBucket879938FA.Arn>/athena/*]",
        (
            "Athena writes result files with generated names: ResearcherRole put/get "
            "is limited to the athena/ prefix of the results bucket."
        ),
    ),
    (
        "DataStack",
        f"{_IAM5_LOCAL}RawBucket0C3EE094.Arn>/raw/*]",
        (
            "Object keys under raw/ are dataset paths chosen at run time: "
            "ResearcherRole reads them (the role is in DataStack)."
        ),
    ),
    (
        "DataStack",
        f"{_IAM5_LOCAL}WarehouseBucket0E783396.Arn>/iceberg/*]",
        (
            "Iceberg data and metadata file names are generated per commit: "
            "ResearcherRole reads are limited to the iceberg/ prefix."
        ),
    ),
    (
        "DataStack",
        "AwsSolutions::AwsSolutions-COG2",
        (
            "Out of scope for the sandbox: single-researcher sign-in by copy-paste "
            "URL; MFA is not enabled and is a separate identity decision."
        ),
    ),
    (
        "DataStack",
        "AwsSolutions::AwsSolutions-COG8",
        (
            "The Plus feature plan costs per monthly active user; the Essentials plan "
            "is enough for managed login in the sandbox."
        ),
    ),
    (
        "ServicesStack",
        "AwsSolutions::AwsSolutions-APIG1",
        (
            "Out of scope for the sandbox: HTTP API access logs need a log group and "
            "have no reader in a single-researcher sandbox. Revisit with the audit "
            "work."
        ),
    ),
    (
        "ServicesStack",
        "Annotation::@aws-cdk/core:crossStackReferencesDefaultStrong",
        (
            "Strong cross-stack references (the CDK default) are wanted: they stop "
            "DataStack deleting an export that ServicesStack still uses."
        ),
    ),
)


Acknowledgements = tuple[tuple[str, str, str], ...]


def apply_nag(app: App, acknowledgements: Acknowledgements | None = None) -> None:
    """Attach the AwsSolutions pack to `app` and apply the acknowledgements.

    `acknowledgements` defaults to the module list; test fixtures pass `()`.
    """
    chosen = ACKNOWLEDGEMENTS if acknowledgements is None else acknowledgements
    Validations.of(app).add_plugins(AwsSolutionsChecks(app))
    stacks = {s.node.id: s for s in app.node.children if isinstance(s, Stack)}
    for stack_id, rule_id, reason in chosen:
        Validations.of(stacks[stack_id]).acknowledge(
            Acknowledgment(id=rule_id, reason=reason),
        )
