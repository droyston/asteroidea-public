"""The re-extract fan-out state machine.

`make reextract` starts one execution per run. Input: ``{run_id, assets}``, each asset
the old FIFO message body (``session_id`` = the tracking id, the asset fields) plus
``run_id``. A Map state runs, per asset and at most ``MAX_CONCURRENCY`` at a time:

1. ``Derive``: DeriveFunction (``derive.handler``) writes the asset's rows to
   the derived store under ``rows/`` and returns the pointer body, outcome DERIVED, or
   outcome ERROR after it set the tracking item FAILED itself.
2. On DERIVED, ``SendPointer`` puts ``{asset_id, run_id, manifest_key}`` on the extract
   FIFO in group ``catalog`` with dedup id ``<tracking id>:p``. The FIFO's one handler
   commits the rows serially. On ERROR nothing is sent.

A Lambda or SQS error on one item is caught: ``RecordFailed`` sets that tracking item
FAILED (error class and cause, ``tables_replaced`` empty) when it is still QUEUED, and
the Map goes on with the other items. Even a lost ``RecordFailed`` write ends in a Pass
state, so no item fails the Map; the CLI's 30-minute poll then reports that asset FAIL.
"""

from typing import TYPE_CHECKING, Final

from aws_cdk import Acknowledgment, Duration, RemovalPolicy, Validations
from aws_cdk import aws_logs as logs
from aws_cdk import aws_stepfunctions as sfn
from aws_cdk import aws_stepfunctions_tasks as tasks

from asteroidea_ingest import queues, sessions

if TYPE_CHECKING:
    from aws_cdk import aws_dynamodb as dynamodb
    from aws_cdk import aws_lambda as lambda_
    from aws_cdk import aws_sqs as sqs
    from constructs import Construct

# Bounded fan-out. The extract FIFO commits one asset at a time, so more
# parallel derives than the commit drains only grow the FIFO backlog that each
# tracking lease must cover. Four derives in flight keep the commit fed when a derive
# takes up to four times a commit, and hold at most four of the account's Lambda
# concurrency (a new sandbox account may have 10 in all) beside the API functions.
MAX_CONCURRENCY: Final = 4
STATE_MACHINE_NAME: Final = "asteroidea-reextract"
LOG_RETENTION: Final = logs.RetentionDays.ONE_MONTH
# One run lists a dataset's assets; the CLI polls each item for 30 minutes at most.
EXECUTION_TIMEOUT: Final = Duration.hours(6)
DERIVED: Final = "DERIVED"
POINTER_DEDUP_SUFFIX: Final = ":p"
THROTTLE_ERRORS: Final = ("Lambda.TooManyRequestsException",)
STAR_ACK_ID: Final = "AwsSolutions::AwsSolutions-IAM5[Resource::*]"
STAR_ACK_REASON: Final = (
    "The re-extract state machine's log delivery (logs:CreateLogDelivery and its "
    "siblings, cdk-nag SF1) and X-Ray tracing (xray:PutTraceSegments and its "
    "siblings, SF2) accept only Resource '*'. Scoped to this role."
)


def _record_failed(scope: Construct, table: dynamodb.ITable) -> sfn.IChainable:
    """Set the tracking item FAILED from the caught error; a lost write still passes."""
    value = tasks.DynamoAttributeValue
    record = tasks.DynamoUpdateItem(
        scope,
        "RecordFailed",
        table=table,
        key={"session_id": value.from_string(sfn.JsonPath.string_at("$.session_id"))},
        update_expression="SET #state = :failed, #error = :error, #tables = :none",
        condition_expression="attribute_exists(#sid) AND #state = :queued",
        expression_attribute_names={
            "#sid": "session_id",
            "#state": sessions.REEXTRACT_STATE,
            "#error": "reextract_error",
            "#tables": "tables_replaced",
        },
        expression_attribute_values={
            ":failed": value.from_string(sessions.REEXTRACT_FAILED),
            ":queued": value.from_string(sessions.REEXTRACT_QUEUED),
            ":error": value.from_map(
                {
                    "class": value.from_string(sfn.JsonPath.string_at("$.error.Error")),
                    "message": value.from_string(
                        sfn.JsonPath.string_at("$.error.Cause")
                    ),
                    "tables_replaced": value.from_list([]),
                }
            ),
            ":none": value.from_list([]),
        },
        result_path=sfn.JsonPath.DISCARD,
    )
    record.add_catch(
        sfn.Pass(scope, "RecordFailedLost"), errors=["States.ALL"], result_path="$.lost"
    )
    return record


def reextract_state_machine(
    scope: Construct,
    *,
    derive_function: lambda_.IFunction,
    extract_queue: sqs.IQueue,
    sessions_table: dynamodb.ITable,
) -> sfn.StateMachine:
    """Return the standard state machine: Map(Derive -> SendPointer), logged, traced."""
    record_failed = _record_failed(scope, sessions_table)
    derive = tasks.LambdaInvoke(
        scope,
        "Derive",
        lambda_function=derive_function,
        payload_response_only=True,
        result_path="$.derive",
    )
    derive.add_retry(
        errors=list(THROTTLE_ERRORS),
        interval=Duration.seconds(5),
        max_attempts=6,
        backoff_rate=2,
    )
    derive.add_catch(record_failed, errors=["States.ALL"], result_path="$.error")
    send_pointer = tasks.SqsSendMessage(
        scope,
        "SendPointer",
        queue=extract_queue,
        message_body=sfn.TaskInput.from_object(
            {
                "asset_id": sfn.JsonPath.string_at("$.derive.asset_id"),
                "run_id": sfn.JsonPath.string_at("$.derive.run_id"),
                "manifest_key": sfn.JsonPath.string_at("$.derive.manifest_key"),
            }
        ),
        message_group_id=queues.GROUP,
        message_deduplication_id=sfn.JsonPath.format(
            "{}" + POINTER_DEDUP_SUFFIX, sfn.JsonPath.string_at("$.session_id")
        ),
        result_path=sfn.JsonPath.DISCARD,
    )
    send_pointer.add_catch(record_failed, errors=["States.ALL"], result_path="$.error")
    outcome = (
        sfn.Choice(scope, "Derived?")
        .when(sfn.Condition.string_equals("$.derive.outcome", DERIVED), send_pointer)
        .otherwise(sfn.Pass(scope, "DeriveRecordedFailure"))
    )
    fan_out = sfn.Map(
        scope,
        "PerAsset",
        items_path="$.assets",
        max_concurrency=MAX_CONCURRENCY,
        result_path=sfn.JsonPath.DISCARD,
    )
    fan_out.item_processor(derive.next(outcome))
    log_group = logs.LogGroup(
        scope,
        "ReextractLogs",
        retention=LOG_RETENTION,
        removal_policy=RemovalPolicy.DESTROY,
    )
    machine = sfn.StateMachine(
        scope,
        "ReextractStateMachine",
        state_machine_name=STATE_MACHINE_NAME,
        state_machine_type=sfn.StateMachineType.STANDARD,
        definition_body=sfn.DefinitionBody.from_chainable(fan_out),
        timeout=EXECUTION_TIMEOUT,
        tracing_enabled=True,  # cdk-nag SF2
        logs=sfn.LogOptions(  # cdk-nag SF1: every event, with execution data
            destination=log_group, level=sfn.LogLevel.ALL, include_execution_data=True
        ),
    )
    # Scoped to this role only, so a '*' resource anywhere else still fails cdk-nag.
    Validations.of(machine.role).acknowledge(
        Acknowledgment(id=STAR_ACK_ID, reason=STAR_ACK_REASON)
    )
    return machine
