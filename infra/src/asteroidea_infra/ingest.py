"""Ingest nested stack: the writer queues, six functions and the reextract flow.

The functions come from the
registry (`functions.INGEST_FUNCTIONS`); the queues, `DeriveFunctionRef` and the
re-extract state machine stay here.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from aws_cdk import Duration, NestedStack
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_sqs as sqs

from asteroidea_infra import reextract_flow
from asteroidea_infra.functions import INGEST_FUNCTIONS, build_function
from asteroidea_ingest import timing

if TYPE_CHECKING:
    from aws_cdk import aws_dynamodb as dynamodb
    from aws_cdk import aws_s3 as s3
    from constructs import Construct

# Visibilities come from `asteroidea_ingest.timing`: the leases and
# `make reclaim` read the same constants.
COMMIT_VISIBILITY_SECONDS = timing.COMMIT_VISIBILITY_SECONDS
EXTRACT_VISIBILITY_SECONDS = timing.EXTRACT_VISIBILITY_SECONDS
MAX_RECEIVE_COUNT = 2
QUEUE_RETENTION_DAYS = 4
DLQ_RETENTION_DAYS = 14
COMMIT_QUEUE_NAME = "asteroidea-commit.fifo"
EXTRACT_QUEUE_NAME = "asteroidea-extract.fifo"
# The LFP build queue: a standard queue and DLQ, not the extract
# FIFO, because LfpBuildFunction writes derived objects only.
LFP_BUILD_VISIBILITY_SECONDS = timing.LFP_BUILD_VISIBILITY_SECONDS
LFP_BUILD_QUEUE_NAME = "asteroidea-lfp-build"


def _fifo_queue(
    scope: Construct, construct_id: str, name: str, visibility_seconds: int
) -> tuple[sqs.Queue, sqs.Queue]:
    """Return a FIFO queue and its FIFO DLQ: SSE-SQS, TLS only, no content dedup."""
    dlq = sqs.Queue(
        scope,
        f"{construct_id}Dlq",
        queue_name=name.replace(".fifo", "-dlq.fifo"),
        fifo=True,
        content_based_deduplication=False,
        encryption=sqs.QueueEncryption.SQS_MANAGED,
        enforce_ssl=True,
        retention_period=Duration.days(DLQ_RETENTION_DAYS),
    )
    queue = sqs.Queue(
        scope,
        construct_id,
        queue_name=name,
        fifo=True,
        content_based_deduplication=False,
        encryption=sqs.QueueEncryption.SQS_MANAGED,
        enforce_ssl=True,
        retention_period=Duration.days(QUEUE_RETENTION_DAYS),
        visibility_timeout=Duration.seconds(visibility_seconds),
        dead_letter_queue=sqs.DeadLetterQueue(
            max_receive_count=MAX_RECEIVE_COUNT, queue=dlq
        ),
    )
    return queue, dlq


def _standard_queue(
    scope: Construct, construct_id: str, name: str, visibility_seconds: int
) -> tuple[sqs.Queue, sqs.Queue]:
    """Return a standard queue and its DLQ: SSE-SQS, TLS only, same redrive."""
    dlq = sqs.Queue(
        scope,
        f"{construct_id}Dlq",
        queue_name=f"{name}-dlq",
        encryption=sqs.QueueEncryption.SQS_MANAGED,
        enforce_ssl=True,
        retention_period=Duration.days(DLQ_RETENTION_DAYS),
    )
    queue = sqs.Queue(
        scope,
        construct_id,
        queue_name=name,
        encryption=sqs.QueueEncryption.SQS_MANAGED,
        enforce_ssl=True,
        retention_period=Duration.days(QUEUE_RETENTION_DAYS),
        visibility_timeout=Duration.seconds(visibility_seconds),
        dead_letter_queue=sqs.DeadLetterQueue(
            max_receive_count=MAX_RECEIVE_COUNT, queue=dlq
        ),
    )
    return queue, dlq


@dataclass(frozen=True)
class IngestBuckets:
    """The buckets the Ingest stack takes from DataStack."""

    raw: s3.IBucket
    warehouse: s3.IBucket
    derived: s3.IBucket  # LfpBuildRole writes lfp/* and summary/*, DeriveRole rows/*
    results: s3.IBucket  # Athena results: LfpBuildRole's summary reads


@dataclass(frozen=True)
class IngestTables:
    """The DynamoDB tables the Ingest stack takes from DataStack."""

    sessions: dynamodb.ITable
    owners: dynamodb.ITable
    import_runs: dynamodb.ITable  # ImportPlanFunction writes runs


class IngestStack(NestedStack):
    """The writer queues, the six ingest functions and the re-extract state machine."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        buckets: IngestBuckets,
        tables: IngestTables,
    ) -> None:
        """Create the queues, each registry row in order, then the reextract flow."""
        super().__init__(scope, construct_id)
        self.buckets = buckets
        self.tables = tables
        self.commit_queue, self.commit_dlq = _fifo_queue(
            self, "CommitQueue", COMMIT_QUEUE_NAME, COMMIT_VISIBILITY_SECONDS
        )
        self.extract_queue, self.extract_dlq = _fifo_queue(
            self, "ExtractQueue", EXTRACT_QUEUE_NAME, EXTRACT_VISIBILITY_SECONDS
        )
        self.lfp_build_queue, self.lfp_build_dlq = _standard_queue(
            self,
            "LfpBuildQueue",
            LFP_BUILD_QUEUE_NAME,
            LFP_BUILD_VISIBILITY_SECONDS,
        )
        # Filled row by row: a later row's env or statements read an earlier function.
        self.functions: dict[str, lambda_.Function] = {}
        for spec in INGEST_FUNCTIONS:
            self.functions[spec.construct_id] = build_function(self, spec)
        self.commit_function = self.functions["CommitFunction"]
        self.import_function = self.functions["ImportFunction"]
        self.import_plan_function = self.functions["ImportPlanFunction"]
        self.extract_function = self.functions["ExtractFunction"]
        self.lfp_build_function = self.functions["LfpBuildFunction"]
        self.derive_function = self.functions["DeriveFunction"]
        # aws-cdk-lib 2.274's IFunction protocol names `grant_invoke(identity)`, the
        # concrete Function `grantee` (pyproject's ty override note); a reference by
        # ARN is typed IFunction, so no ty override is widened for this module.
        derive_ref = lambda_.Function.from_function_attributes(
            self,
            "DeriveFunctionRef",
            function_arn=self.derive_function.function_arn,
            same_environment=True,
        )
        self.reextract_state_machine = reextract_flow.reextract_state_machine(
            self,
            derive_function=derive_ref,
            extract_queue=self.extract_queue,
            sessions_table=tables.sessions,
        )
