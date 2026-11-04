"""The per-template budget.

The plain test is the red proof on a planted template: at 80 resources the real-template
check cannot fail, so a planted template shows the check fires. The synth-marked test
guards the real templates.
"""

from typing import Any

import pytest
from aws_cdk import App, Environment
from aws_cdk.assertions import Template

from asteroidea_infra import stack_budget
from asteroidea_infra.app import ServicesStack
from asteroidea_infra.data_stack import DataStack
from asteroidea_infra.stack_budget import over_budget


def planted() -> dict[str, Any]:
    """Return a template over all four budgets (301, 151, 151, 800,000+ bytes)."""
    return {
        "Resources": {
            f"R{i}": {"Type": "AWS::SQS::Queue", "Properties": {"Pad": "x" * 2700}}
            for i in range(301)
        },
        "Outputs": {f"O{i}": {"Value": "v"} for i in range(151)},
        "Parameters": {f"P{i}": {"Type": "String"} for i in range(151)},
    }


def test_planted_template_gets_four_refusals() -> None:
    template = planted()
    assert stack_budget.template_bytes(template) > stack_budget.MAX_BYTES
    refusals = over_budget("Api", template)
    assert len(refusals) == 4
    assert refusals[:3] == [
        "Api: 301 resources, budget 300",
        "Api: 151 outputs, budget 150",
        "Api: 151 parameters, budget 150",
    ]
    assert refusals[3].startswith("Api: ")
    assert refusals[3].endswith(" bytes, budget 800000")


def test_a_template_at_the_budget_passes() -> None:
    template = {
        "Resources": {f"R{i}": {"Type": "AWS::SQS::Queue"} for i in range(300)},
        "Outputs": {f"O{i}": {"Value": "v"} for i in range(150)},
        "Parameters": {f"P{i}": {"Type": "String"} for i in range(150)},
    }
    assert over_budget("Api", template) == []


def test_an_empty_template_passes() -> None:
    assert over_budget("Data", {}) == []


@pytest.mark.synth
def test_real_templates_are_within_budget() -> None:
    env = Environment(account="000000000000", region="us-east-1")
    app = App(context={"sandbox-test": "true"})
    data = DataStack(app, "DataStack", env=env)
    services = ServicesStack(app, "ServicesStack", env=env, data=data)

    templates = {
        "Data": Template.from_stack(data).to_json(),
        "Services": Template.from_stack(services).to_json(),
        "Api": Template.from_stack(services.api).to_json(),
        "Ingest": Template.from_stack(services.ingest).to_json(),
    }
    assert {name: over_budget(name, dict(t)) for name, t in templates.items()} == {
        name: [] for name in templates
    }
