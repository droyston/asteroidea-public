"""Shared synth fixture: the DataStack template, built once per session."""

import pytest
from aws_cdk import App, Environment
from aws_cdk.assertions import Template

from asteroidea_infra.data_stack import DataStack


@pytest.fixture(scope="session")
def data_template() -> Template:
    app = App()
    stack = DataStack(
        app,
        "DataStack",
        env=Environment(account="000000000000", region="us-east-1"),
    )
    return Template.from_stack(stack)
