"""Helpers the synth tests share: read IAM statements out of a template.

They sit in the package, not in `infra/tests/`, because the suite runs with
`--import-mode=importlib`, under which a test module cannot import a sibling module
or the conftest. Only tests import this module.
"""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from aws_cdk.assertions import Template

# A parsed CloudFormation value: any JSON shape.
type Node = Any


def flat(node: Node) -> Node:
    """Resolve `Fn::Join` and the partition in an ARN so it compares as plain text."""
    if isinstance(node, dict) and "Fn::Join" in node:
        sep, parts = node["Fn::Join"]
        return sep.join(str(flat(p)) for p in parts)
    if node == {"Ref": "AWS::Partition"}:
        return "aws"
    if isinstance(node, list):
        return [flat(n) for n in node]
    return node


def policy_statements(
    template: Template, prefix: str = "", *, exactly_one: bool = False
) -> list[dict[str, Any]]:
    """Return the statements of the role policies whose logical id starts with `prefix`.

    With `exactly_one`, the template must hold a single `AWS::IAM::Policy` (the unpack
    fails otherwise) and `prefix` is ignored.
    """
    policies = template.find_resources("AWS::IAM::Policy")
    if exactly_one:
        (only,) = policies.values()
        return list(only["Properties"]["PolicyDocument"]["Statement"])
    out: list[dict[str, Any]] = []
    for lid, policy in policies.items():
        if lid.startswith(prefix):
            out.extend(policy["Properties"]["PolicyDocument"]["Statement"])
    return out


def actions(statement: dict[str, Any]) -> set[str]:
    """Return a statement's `Action` as a set, whether it is one string or a list."""
    found = statement["Action"]
    return {found} if isinstance(found, str) else set(found)
