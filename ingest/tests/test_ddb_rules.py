"""The fixture rule that DynamoDB rejects unused expression placeholders."""

import pytest
from botocore.exceptions import ClientError

from . import ddb_rules


def test_a_name_no_expression_uses_is_rejected() -> None:
    params = {
        "UpdateExpression": "SET #a = :a",
        "ConditionExpression": "#b = :a",
        "ExpressionAttributeNames": {"#a": "a", "#b": "b", "#run_id": "run_id"},
        "ExpressionAttributeValues": {":a": {"S": "x"}},
    }
    with pytest.raises(ClientError, match=r"ExpressionAttributeNames unused.*#run_id"):
        ddb_rules.check(params, "UpdateItem")


def test_a_value_no_expression_uses_is_rejected() -> None:
    params = {
        "KeyConditionExpression": "owner_sub = :o",
        "ExpressionAttributeValues": {":o": {"S": "x"}, ":extra": {"S": "y"}},
    }
    with pytest.raises(ClientError, match=r"ExpressionAttributeValues unused.*:extra"):
        ddb_rules.check(params, "Query")


def test_placeholders_used_in_any_expression_pass() -> None:
    ddb_rules.check(
        {
            "UpdateExpression": "SET #a = :a",
            "ConditionExpression": "attribute_not_exists(#k)",
            "FilterExpression": "#f = :f",
            "ProjectionExpression": "#p",
            "ExpressionAttributeNames": {"#a": "a", "#k": "k", "#f": "f", "#p": "p"},
            "ExpressionAttributeValues": {":a": 1, ":f": 2},
        }
    )
