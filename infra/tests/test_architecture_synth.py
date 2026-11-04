"""The architecture model against the synthesized stacks.

Each model edge an IAM statement backs has a matching grant in its caller's role; each
queue-to-consumer edge has an event source mapping; the state machine edges are tasks
in its definition. The reverse holds too: every write or send grant from a function
role to a resource the diagram shows has a model edge, or an entry in `ELIDED` that
says why the diagram folds it away.
"""

import json
import re
from typing import Any, Final

import pytest
from aws_cdk import App, Environment
from aws_cdk.assertions import Template

from asteroidea_infra import architecture, athena, functions, schema
from asteroidea_infra.app import ServicesStack
from asteroidea_infra.data_stack import DataStack
from asteroidea_infra.nag import apply_nag
from asteroidea_infra.synth_asserts import actions, flat, policy_statements

pytestmark = pytest.mark.synth

ENV = Environment(account="000000000000", region="us-east-1")

WRITE_VERBS: Final = ("Put", "Delete", "Update", "Send", "Invoke")
READ_VERBS: Final = ("Get", "Query", "Scan", "BatchGet", "List")

# What a node's ARN looks like in a statement's flattened resource.
PATTERNS: Final = {
    "SiteBucket": "WebSiteBucket",
    "RawBucket": "RawBucket",
    "ArtifactsBucket": "ArtifactsBucket",
    "DerivedBucket": "DerivedBucket",
    "WarehouseBucket": "WarehouseBucket",
    "AthenaResultsBucket": "AthenaResultsBucket",
    "SessionsTable": "SessionsTable",
    "OwnersTable": "DatasetOwnersTable",
    "LabelsTable": "LabelsTable",
    "ImportRunsTable": "ImportRunsTable",
    "CommitQueue": "CommitQueue",
    "ExtractQueue": "ExtractQueue",
    "LfpBuildQueue": "LfpBuildQueue",
    "GlueDatabase": f"database/{schema.DATABASE}",
    "AthenaWorkgroup": f"workgroup/{athena.WORKGROUP}",
    "OpenNeuroBucket": "openneuro.org",
    **{s.construct_id: s.construct_id for s in functions.FUNCTIONS},
}

# Write grants the diagram folds away, by (caller, resource), with the reason.
ATHENA_RESULTS: Final = (
    "an Athena caller's athena/ writes are drawn once, from the workgroup"
)
CONSUMER: Final = "a consumer deletes and re-times its own messages: its queue read"
ICEBERG: Final = "the Iceberg commit's glue:UpdateTable folds into its iceberg/ write"
ELIDED: Final = {
    ("CatalogFunction", "AthenaResultsBucket"): ATHENA_RESULTS,
    ("ComputeFunction", "AthenaResultsBucket"): ATHENA_RESULTS,
    ("LabelsFunction", "AthenaResultsBucket"): ATHENA_RESULTS,
    ("LfpBuildFunction", "AthenaResultsBucket"): ATHENA_RESULTS,
    ("CommitFunction", "CommitQueue"): CONSUMER,
    ("ExtractFunction", "ExtractQueue"): CONSUMER,
    ("LfpBuildFunction", "LfpBuildQueue"): (
        CONSUMER + "; it also re-queues one message per session, noted in its box"
    ),
    ("CommitFunction", "GlueDatabase"): ICEBERG,
    ("ExtractFunction", "GlueDatabase"): ICEBERG,
}

STATE_MACHINE: Final = "ReextractStateMachine"
API_IDS: Final = {s.construct_id for s in functions.API_FUNCTIONS}


@pytest.fixture(scope="module")
def stacks() -> dict[str, Template]:
    """Build the real app with default context and return the three templates."""
    app = App()
    data = DataStack(app, "DataStack", env=ENV)
    services = ServicesStack(app, "ServicesStack", env=ENV, data=data)
    apply_nag(app)
    app.synth()
    return {
        "data": Template.from_stack(data),
        "ingest": Template.from_stack(services.ingest),
        "api": Template.from_stack(services.api),
    }


def _role(stacks: dict[str, Template], caller: str) -> list[dict[str, Any]]:
    """Return the allowed statements of the role `caller` acts as."""
    if caller == "ResearcherRole":
        found = policy_statements(stacks["data"], "IdentityResearcherRole")
    elif caller == STATE_MACHINE:
        found = policy_statements(stacks["ingest"], "ReextractStateMachineRole")
    else:
        template = stacks["api" if caller in API_IDS else "ingest"]
        found = policy_statements(template, f"{caller}ServiceRoleDefaultPolicy")
    assert found, caller
    return [s for s in found if s["Effect"] == "Allow"]


def _text(statement: dict[str, Any]) -> str:
    return json.dumps(flat(statement["Resource"]))


def _verbs(statement: dict[str, Any], verbs: tuple[str, ...]) -> list[str]:
    return [a for a in actions(statement) if a.split(":")[1].startswith(verbs)]


def _reaches(text: str, node: str, port: str) -> bool:
    return PATTERNS[node] in text and (not port or f"/{port}/" in text)


def _iam_edges() -> list[architecture.Edge]:
    """Return the model edges an IAM statement backs, by their basis."""
    return [
        e
        for e in architecture.EDGES
        if re.search(r"_statements :: ", e.basis)
        or (e.src == "ResearcherRole" and e.basis.startswith("identity.py :: "))
        or e.src == STATE_MACHINE
    ]


def test_every_iam_edge_has_its_grant(stacks: dict[str, Template]) -> None:
    edges = _iam_edges()
    assert len(edges) > 40
    missing = []
    for e in edges:
        verbs = WRITE_VERBS if e.verb == architecture.WRITE else READ_VERBS
        if not any(
            _reaches(_text(s), e.dst, e.port) and _verbs(s, verbs)
            for s in _role(stacks, e.src)
        ):
            missing.append(f"{e.src} {e.verb}s {e.dst} {e.port}: {e.basis}")
    assert missing == []


def test_every_queue_consumer_has_its_event_source(stacks: dict[str, Template]) -> None:
    all_nodes = architecture.nodes()
    consumers = [
        (e.src, e.dst)
        for e in architecture.EDGES
        if e.verb == architecture.READ and all_nodes[e.dst].kind == architecture.QUEUE
    ]
    assert len(consumers) == 3
    mappings = [
        (m["Properties"]["FunctionName"]["Ref"], m["Properties"]["EventSourceArn"])
        for m in stacks["ingest"]
        .find_resources("AWS::Lambda::EventSourceMapping")
        .values()
    ]
    missing = [
        (fn, q)
        for fn, q in consumers
        if not any(
            ref.startswith(fn) and arn["Fn::GetAtt"][0].startswith(q)
            for ref, arn in mappings
        )
    ]
    assert missing == []


def test_the_state_machine_edges_are_its_tasks(stacks: dict[str, Template]) -> None:
    (machine,) = (
        stacks["ingest"].find_resources("AWS::StepFunctions::StateMachine").values()
    )
    definition = str(flat(machine["Properties"]["DefinitionString"]))
    edges = [e for e in architecture.EDGES if e.src == STATE_MACHINE]
    assert len(edges) == 3
    for e in edges:
        state = e.basis.split(":: ")[1].split(" (")[0]
        assert f'"{state}"' in definition, e
        assert PATTERNS[e.dst] in definition, e


def test_every_write_grant_is_drawn_or_elided(stacks: dict[str, Template]) -> None:
    all_nodes = architecture.nodes()
    writes = {
        (e.src, e.dst, e.port)
        for e in architecture.EDGES
        if e.verb == architecture.WRITE
    }
    callers = [s.construct_id for s in functions.FUNCTIONS] + [STATE_MACHINE]
    undrawn = set()
    for caller in callers:
        for s in _role(stacks, caller):
            if not _verbs(s, WRITE_VERBS):
                continue
            text = _text(s)
            for node, pattern in PATTERNS.items():
                if pattern not in text or (caller, node) in ELIDED:
                    continue
                ports = [p.key for p in all_nodes[node].ports if f"/{p.key}/" in text]
                if not any((caller, node, p) in writes for p in ports or [""]):
                    undrawn.add(
                        f"{caller} {sorted(_verbs(s, WRITE_VERBS))} {node} {ports}"
                    )
    assert sorted(undrawn) == []
    # And no elision is stale: each names a grant that exists.
    stale = [
        pair
        for pair in ELIDED
        if not any(
            PATTERNS[pair[1]] in _text(s) and _verbs(s, WRITE_VERBS)
            for s in _role(stacks, pair[0])
        )
    ]
    assert stale == []
