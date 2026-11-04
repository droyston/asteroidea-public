"""DynamoDB's rule that every expression placeholder is used (fixture helper).

The service rejects a call whose `ExpressionAttributeNames`
or `ExpressionAttributeValues` holds a key that no expression of the call mentions.
botocore's Stubber does not check it, so a test with `ANY` for the names passed while
the deployed call failed (seen live with ImportPlanFunction). `install`
makes a boto3 client check every call; `check` serves the hand-written fakes.
"""

import re
from typing import TYPE_CHECKING, Any

from botocore.exceptions import ClientError

if TYPE_CHECKING:
    from collections.abc import Mapping

_EXPRESSIONS = (
    "UpdateExpression",
    "ConditionExpression",
    "KeyConditionExpression",
    "FilterExpression",
    "ProjectionExpression",
)
_NAME = re.compile(r"#\w+")
_VALUE = re.compile(r":\w+")


def check(params: Mapping[str, Any], operation: str = "DynamoDB") -> None:
    """Raise the service's ValidationException when a placeholder is unused."""
    text = " ".join(str(params.get(k, "")) for k in _EXPRESSIONS)
    for field, pattern in (
        ("ExpressionAttributeNames", _NAME),
        ("ExpressionAttributeValues", _VALUE),
    ):
        declared = set(params.get(field) or {})
        unused = sorted(declared - set(pattern.findall(text)))
        if unused:
            message = (
                "1 validation error detected: Value provided "
                f"in {field} unused in expressions: keys: {{{', '.join(unused)}}}"
            )
            raise ClientError(
                {"Error": {"Code": "ValidationException", "Message": message}},
                operation,
            )


def install(client: Any) -> Any:
    """Make every call of a DynamoDB `client` run `check`; return the client."""
    client.meta.events.register(
        "before-parameter-build.dynamodb.*",
        lambda params, model, **_: check(params, model.name),
    )
    return client
