"""Fixed role names for the two raw-bucket writers."""

from typing import TYPE_CHECKING

from aws_cdk import aws_iam as iam

if TYPE_CHECKING:
    from aws_cdk import aws_lambda as lambda_


def name_execution_role(function: lambda_.Function, name: str) -> None:
    """Give the function's own execution role the fixed physical name `name`.

    The raw bucket policy lives in DataStack and must name CommitRole and
    SessionApiRole. A reference to the roles would make DataStack depend on
    ServicesStack, and ServicesStack already depends on DataStack for the buckets.
    A fixed name lets the policy hold the ARN as a string.
    """
    node = function.role.node.default_child if function.role is not None else None
    if not isinstance(node, iam.CfnRole):
        msg = f"{function.node.id} has no CfnRole execution role to name"
        raise TypeError(msg)
    node.role_name = name
