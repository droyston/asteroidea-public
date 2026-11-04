"""Ingest and Api pins (arm64, memory, timeout, routes, authorizer, IAM)."""

import fnmatch
import json
import re
from pathlib import Path
from typing import Any, Final

import pytest
from aws_cdk import App, Environment
from aws_cdk import aws_iam as iam
from aws_cdk.assertions import Match, Template

from asteroidea_infra import reextract_flow, storage
from asteroidea_infra.app import ServicesStack
from asteroidea_infra.data_stack import DataStack
from asteroidea_infra.functions import EXTRACT_TABLES
from asteroidea_infra.nag import apply_nag
from asteroidea_infra.synth_asserts import actions, policy_statements
from asteroidea_ingest import artifacts, imports_api, timing
from asteroidea_ingest.routes import ROUTES

pytestmark = pytest.mark.synth

ENV = Environment(account="000000000000", region="us-east-1")
# The literal route oracle, written by hand.
ROUTES_ORACLE: Final = Path(__file__).parent / "fixtures" / "routes.txt"
INGEST_SRC: Final = Path(__file__).resolve().parents[2] / "ingest" / "src"
# Written out, not imported: the tables the summary builder queries.
SUMMARY_TABLES_ORACLE: Final = frozenset(
    {
        "assets",
        "subjects",
        "sessions",
        "asset_headers",
        "series",
        "units",
        "unit_spikes",
        "electrodes",
        "intervals",
        "events",
    }
)


@pytest.fixture(scope="module")
def stacks() -> dict[str, Template]:
    """Build the real app (sandbox-test client on) and return nested templates."""
    app = App(context={"sandbox-test": "true"})
    data = DataStack(app, "DataStack", env=ENV)
    services = ServicesStack(app, "ServicesStack", env=ENV, data=data)
    apply_nag(app)
    app.synth()  # cdk-nag stays clean with Identity, Ingest and Api wired in
    return {
        "ingest": Template.from_stack(services.ingest),
        "api": Template.from_stack(services.api),
    }


COMMIT_FN = "CommitFunction"
IMPORT_FN = "ImportFunction"
EXTRACT_FN = "ExtractFunction"
LFP_FN = "LfpBuildFunction"
LFP_QUEUES = ("asteroidea-lfp-build", "asteroidea-lfp-build-dlq")


def _function(template: Template, function: str) -> dict[str, Any]:
    (found,) = [
        dict(f)
        for lid, f in template.find_resources("AWS::Lambda::Function").items()
        if lid.startswith(function)
    ]
    return found


def _resource_text(statement: dict[str, Any]) -> str:
    return str(statement["Resource"])


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]


def _by_action(
    template: Template, action: str, within: str = "", function: str = ""
) -> dict[str, Any]:
    (found,) = [
        s
        for s in policy_statements(template, function)
        if action in actions(s) and within in _resource_text(s)
    ]
    return found


class TestIngest:
    def test_commit_function_shape(self, stacks: dict[str, Template]) -> None:
        props = _function(stacks["ingest"], COMMIT_FN)["Properties"]
        assert props["PackageType"] == "Image"
        assert props["Architectures"] == ["arm64"]
        assert props["MemorySize"] == 2048
        assert props["Timeout"] == 120

    def test_the_commit_is_not_invoked_async_any_more(
        self, stacks: dict[str, Template]
    ) -> None:
        """The commit queue feeds the function; the earlier async retries went."""
        stacks["ingest"].resource_count_is("AWS::Lambda::EventInvokeConfig", 0)

    def test_environment_names_table_and_bucket(
        self, stacks: dict[str, Template]
    ) -> None:
        stacks["ingest"].has_resource_properties(
            "AWS::Lambda::Function",
            {
                "Environment": {
                    "Variables": {
                        "SESSIONS_TABLE": Match.any_value(),
                        "RAW_BUCKET": Match.any_value(),
                    }
                }
            },
        )

    def test_least_privilege(self, stacks: dict[str, Template]) -> None:
        t = stacks["ingest"]
        for s in policy_statements(t):
            assert all("*" not in a for a in actions(s)), s
        assert actions(_by_action(t, "dynamodb:GetItem", function=COMMIT_FN)) == {
            "dynamodb:GetItem",
            "dynamodb:UpdateItem",
        }
        write = _by_action(
            t, "s3:PutObject", "raw/*", COMMIT_FN
        )  # raw writes, rollback
        assert "s3:DeleteObjectVersion" in actions(write)
        assert "raw/*" in _resource_text(write)
        assert "landing" not in _resource_text(write)
        read = _by_action(t, "s3:GetObjectVersion", function=COMMIT_FN)
        assert "landing/*" in _resource_text(read)
        assert "raw/*" not in _resource_text(read)
        glue = _by_action(t, "glue:UpdateTable", function=COMMIT_FN)
        assert actions(glue) == {
            "glue:GetDatabase",
            "glue:GetTable",
            "glue:UpdateTable",
        }
        assert "assets" in _resource_text(glue)
        assert "datasets" in _resource_text(glue)


class TestCommitReclaimHead:
    def test_commit_role_can_head_raw_objects_for_adoption(
        self, stacks: dict[str, Template]
    ) -> None:
        """A resume HEADs the raw key; a missing key must answer 404, not 403."""
        t = stacks["ingest"]
        read = _by_action(t, "s3:GetObject", "raw/*", COMMIT_FN)
        assert actions(read) == {"s3:GetObject"}
        (listing,) = [
            st
            for st in policy_statements(t, COMMIT_FN)
            if actions(st) == {"s3:ListBucket"} and "Condition" in st
        ]
        assert listing["Condition"] == {
            "StringLike": {"s3:prefix": ["landing/*", "raw/*"]}
        }


class TestCommitWarehouseList:
    def test_commit_role_can_list_the_warehouse_so_a_missing_key_is_a_404(
        self, stacks: dict[str, Template]
    ) -> None:
        """PyIceberg HeadObjects the metadata key it is about to write.

        Without s3:ListBucket on the warehouse bucket S3 answers 403 and the append
        fails (AccessDenied during HeadObject on the first commit).
        """
        listing = [
            s
            for s in policy_statements(stacks["ingest"], COMMIT_FN)
            if "s3:ListBucket" in actions(s)
        ]
        unconditioned = [s for s in listing if "Condition" not in s]
        assert len(listing) == 2  # landing (conditioned) and warehouse
        assert len(unconditioned) == 1


class TestApi:
    def test_routes_all_use_the_jwt_authorizer(
        self, stacks: dict[str, Template]
    ) -> None:
        """The template's route keys, the route table and the literal oracle agree."""
        t = stacks["api"]
        routes = t.find_resources("AWS::ApiGatewayV2::Route")
        template = {r["Properties"]["RouteKey"] for r in routes.values()}
        table = {r.key for r in ROUTES}
        oracle = set(ROUTES_ORACLE.read_text().splitlines())
        for name, keys in (("the table", table), ("the template", template)):
            assert not keys - oracle, (
                f"in {name} but not in fixtures/routes.txt: {sorted(keys - oracle)}"
            )
            assert not oracle - keys, (
                f"in fixtures/routes.txt but not in {name}: {sorted(oracle - keys)}"
            )
        for route in routes.values():
            assert route["Properties"]["AuthorizationType"] == "JWT"
            assert "AuthorizerId" in route["Properties"]

    def test_uploads_route_is_on_the_jwt_authorizer(
        self, stacks: dict[str, Template]
    ) -> None:
        t = stacks["api"]
        (authorizer,) = t.find_resources("AWS::ApiGatewayV2::Authorizer")
        (route,) = [
            r
            for r in t.find_resources("AWS::ApiGatewayV2::Route").values()
            if r["Properties"]["RouteKey"] == "GET /uploads"
        ]
        assert route["Properties"]["AuthorizationType"] == "JWT"
        assert route["Properties"]["AuthorizerId"] == {"Ref": authorizer}

    def test_get_started_route_is_on_the_jwt_authorizer(
        self, stacks: dict[str, Template]
    ) -> None:
        t = stacks["api"]
        (authorizer,) = t.find_resources("AWS::ApiGatewayV2::Authorizer")
        (route,) = [
            r
            for r in t.find_resources("AWS::ApiGatewayV2::Route").values()
            if r["Properties"]["RouteKey"] == "GET /get-started"
        ]
        assert route["Properties"]["AuthorizationType"] == "JWT"
        assert route["Properties"]["AuthorizerId"] == {"Ref": authorizer}

    def test_artifact_routes_are_on_the_jwt_authorizer_and_platform_function(
        self, stacks: dict[str, Template]
    ) -> None:
        t = stacks["api"]
        (authorizer,) = t.find_resources("AWS::ApiGatewayV2::Authorizer")
        wanted = {
            "POST /artifacts",
            "POST /artifacts/{id}/finalize",
            "GET /artifacts",
            "GET /artifacts/{id}",
            "PATCH /artifacts/{id}",
            "POST /artifacts/{id}/withdraw",
            "DELETE /artifacts/{id}",
        }
        routes = [
            r
            for r in t.find_resources("AWS::ApiGatewayV2::Route").values()
            if r["Properties"]["RouteKey"] in wanted
        ]
        assert len(routes) == len(wanted)
        targets = {r["Properties"]["Target"]["Fn::Join"][1][1]["Ref"] for r in routes}
        for route in routes:
            assert route["Properties"]["AuthorizationType"] == "JWT"
            assert route["Properties"]["AuthorizerId"] == {"Ref": authorizer}
        assert len(targets) == 1  # one integration, shared with GET /get-started

    def test_platform_function_shape_and_environment(
        self, stacks: dict[str, Template]
    ) -> None:
        env = _function(stacks["api"], "PlatformFunction")["Properties"]["Environment"][
            "Variables"
        ]
        assert _function(stacks["api"], "PlatformFunction")["Properties"][
            "Handler"
        ] == ("asteroidea_ingest.get_started_api.handler")
        assert env["PLATFORM_SDK_CALLBACK"] == "http://localhost:8765/callback"
        assert env["PLATFORM_REGION"] == "us-east-1"
        assert "SITE_BUCKET" in env
        assert env["WEB_URL"] == {"Fn::ImportValue": "asteroidea-WebUrl"}
        assert "ARTIFACTS_BUCKET" in env
        assert "SdkClientId" in str(env["PLATFORM_SDK_CLIENT_ID"])
        assert "WebClientId" not in str(env)

    def test_platform_role_s3_is_handout_read_and_artifacts_only(
        self, stacks: dict[str, Template]
    ) -> None:
        """PlatformRole's S3: GetObject on `handout/*`; artifacts only are added."""
        s3 = [
            s
            for s in policy_statements(stacks["api"], "PlatformFunction")
            if any(a.startswith("s3:") for a in actions(s))
        ]
        assert len(s3) == 3
        assert all(s["Effect"] == "Allow" for s in s3)
        (handout,) = [s for s in s3 if "/handout/*" in _resource_text(s)]
        assert actions(handout) == {"s3:GetObject"}
        assert "site/" not in _resource_text(handout)
        (objects,) = [s for s in s3 if "artifacts/*" in _resource_text(s)]
        assert actions(objects) == {"s3:PutObject", "s3:GetObject", "s3:DeleteObject"}
        (listing,) = [s for s in s3 if actions(s) == {"s3:ListBucket"}]
        assert "ArtifactsBucket" in _resource_text(listing)
        assert "/" not in _resource_text(listing).split("ArtifactsBucket")[-1]

    def test_platform_role_reads_writes_and_deletes_every_artifacts_key(
        self, stacks: dict[str, Template]
    ) -> None:
        """Every key `artifacts.py` writes is under a Put, Get and Delete grant.

        The keys come from the module's own key builders (record, data, and the
        by-id, by-input and by-time index). The S3 fakes could not see a missing
        grant; this reads the synthesized policy.
        """
        record = {"author_sub": "sub", "artifact_id": "aid", "name": "f.png"}
        keys = {
            "data": artifacts.data_key(record),
            "record": artifacts.data_key(record).split("/data/")[0]
            + "/provenance.json",
            "by-id": f"{artifacts.INDEX_BY_ID}aid",
            "by-input": f"{artifacts.by_input_prefix('asset-1')}aid",
            "by-time": artifacts.by_time_key(1, "aid"),
        }
        wanted = {"s3:PutObject", "s3:GetObject", "s3:DeleteObject"}
        patterns = [
            part.removeprefix("/")
            for s in policy_statements(stacks["api"], "PlatformFunction")
            if wanted <= actions(s)
            for resource in _as_list(s["Resource"])
            if "ArtifactsBucket" in str(resource)
            for part in resource["Fn::Join"][1]
            if isinstance(part, str) and part.startswith("/")
        ]
        ungranted = {
            name: key
            for name, key in keys.items()
            if not any(fnmatch.fnmatchcase(key, p) for p in patterns)
        }
        assert not ungranted, f"no Put/Get/Delete grant for {ungranted}; {patterns}"

    def test_platform_role_may_only_put_notes(
        self, stacks: dict[str, Template]
    ) -> None:
        """PutItem on the notes table and no other DynamoDB grant."""
        env = _function(stacks["api"], "PlatformFunction")["Properties"]["Environment"][
            "Variables"
        ]
        assert "NotesTable" in str(env["NOTES_TABLE"])
        ddb = [
            s
            for s in policy_statements(stacks["api"], "PlatformFunction")
            if any(a.startswith("dynamodb:") for a in actions(s))
        ]
        assert len(ddb) == 1
        (put,) = ddb
        assert put["Effect"] == "Allow"
        assert actions(put) == {"dynamodb:PutItem"}
        assert "NotesTable" in _resource_text(put)
        assert "/index/" not in _resource_text(put)

    def test_catalog_routes_are_on_the_jwt_authorizer(
        self, stacks: dict[str, Template]
    ) -> None:
        t = stacks["api"]
        (authorizer,) = t.find_resources("AWS::ApiGatewayV2::Authorizer")
        routes = [
            r
            for r in t.find_resources("AWS::ApiGatewayV2::Route").values()
            if r["Properties"]["RouteKey"].startswith("GET /catalog/")
        ]
        # The download route is added; then studies, links, session-summary
        # (served by ComputeFunction) and the batch asset lookup.
        assert len(routes) == 11
        for route in routes:
            assert route["Properties"]["AuthorizationType"] == "JWT"
            assert route["Properties"]["AuthorizerId"] == {"Ref": authorizer}

    def test_catalog_function_timeout_env_and_role_name(
        self, stacks: dict[str, Template]
    ) -> None:
        fn = _function(stacks["api"], "CatalogFunction")["Properties"]
        assert fn["Handler"] == "asteroidea_ingest.catalog_api.handler"
        assert fn["Timeout"] == 20
        assert fn["Architectures"] == ["arm64"]
        env = dict(fn["Environment"]["Variables"])
        assert env.pop("RAW_BUCKET")  # the bucket the download route presigns
        # The bucket whose summary/<dataset>/_dataset.json the studies reads
        assert "DerivedBucket" in json.dumps(env.pop("DERIVED_BUCKET"))
        assert env == {"ATHENA_WORKGROUP": "asteroidea", "GLUE_DATABASE": "asteroidea"}
        stacks["api"].has_resource_properties(
            "AWS::IAM::Role", {"RoleName": "asteroidea-catalog-role"}
        )

    def test_catalog_role_statements_are_exactly_the_five_1_set(
        self, stacks: dict[str, Template]
    ) -> None:
        statements = policy_statements(stacks["api"], "CatalogFunction")
        assert len(statements) == 9  # raw/* and the summaries are added
        glue = [s for s in statements if any(a.startswith("glue:") for a in actions(s))]
        assert len(glue) == 1
        assert actions(glue[0]) == {"glue:GetDatabase", "glue:GetTable"}
        text = _resource_text(glue[0])
        assert ":catalog'" in text  # the Glue catalog ARN
        assert text.count("table/") == 13
        for table in (
            "assets",
            "datasets",
            "asset_headers",
            "subjects",
            "sessions",
            "series",
            "electrodes",
            "units",
            "intervals",
            "events",
            "subject_links",
            "studies",
            "study_datasets",
        ):
            assert f"table/asteroidea/{table}'" in text
        athena = [
            s for s in statements if any(a.startswith("athena:") for a in actions(s))
        ]
        assert len(athena) == 1
        assert actions(athena[0]) == {
            "athena:StartQueryExecution",
            "athena:GetQueryExecution",
            "athena:GetQueryResults",
            "athena:StopQueryExecution",
        }
        assert "workgroup/asteroidea" in _resource_text(athena[0])
        s3 = [s for s in statements if any(a.startswith("s3:") for a in actions(s))]
        by_resource = {_resource_text(s): actions(s) for s in s3}
        assert len(s3) == 7  # raw/* and the summary get and list are added
        assert {"s3:GetObject"} in by_resource.values()
        assert any(
            a
            == {
                "s3:PutObject",
                "s3:GetObject",
                "s3:AbortMultipartUpload",
                "s3:ListMultipartUploadParts",
            }
            and "/athena/*" in r
            for r, a in by_resource.items()
        )
        locations = [
            r
            for r, a in by_resource.items()
            if a == {"s3:ListBucket", "s3:GetBucketLocation"}
        ]
        assert len(locations) == 2  # warehouse and results buckets
        assert any("AthenaResultsBucket" in r for r in locations)

    def test_catalog_role_reads_raw_objects_only_under_raw_prefix(
        self, stacks: dict[str, Template]
    ) -> None:
        """The download route presigns `raw/*`; nothing else of the bucket."""
        raw = [
            s
            for s in policy_statements(stacks["api"], "CatalogFunction")
            if "RawBucket" in _resource_text(s)
        ]
        assert len(raw) == 1
        assert sorted(raw[0]["Action"]) == [
            "s3:GetObject",
            "s3:GetObjectVersion",
        ]
        assert "/raw/*" in _resource_text(raw[0])
        assert "landing/" not in _resource_text(raw[0])
        for s in policy_statements(stacks["api"], "CatalogFunction"):
            assert "landing/" not in _resource_text(s)

    def test_catalog_role_reads_only_the_dataset_summaries_of_the_derived_bucket(
        self, stacks: dict[str, Template]
    ) -> None:
        """GetObject on summary/*/_dataset.json; ListBucket on summary/*."""
        derived = [
            s
            for s in policy_statements(stacks["api"], "CatalogFunction")
            if "DerivedBucket" in _resource_text(s)
        ]
        assert derived == [
            {
                "Action": "s3:GetObject",
                "Effect": "Allow",
                "Resource": {
                    "Fn::Join": ["", [DERIVED_ARN, "/summary/*/_dataset.json"]]
                },
            },
            {
                "Action": "s3:ListBucket",
                "Condition": {"StringLike": {"s3:prefix": ["summary/*"]}},
                "Effect": "Allow",
                "Resource": DERIVED_ARN,
            },
        ]

    def test_compute_routes_are_on_the_jwt_authorizer_and_compute_function(
        self, stacks: dict[str, Template]
    ) -> None:
        t = stacks["api"]
        (authorizer,) = t.find_resources("AWS::ApiGatewayV2::Authorizer")
        routes = [
            r
            for r in t.find_resources("AWS::ApiGatewayV2::Route").values()
            if r["Properties"]["RouteKey"].startswith("GET /compute/")
        ]
        assert sorted(r["Properties"]["RouteKey"] for r in routes) == [
            "GET /compute/events",
            "GET /compute/overview",
            "GET /compute/rates",
            "GET /compute/spectrogram",
            "GET /compute/tuning",
        ]
        for route in routes:
            assert route["Properties"]["AuthorizationType"] == "JWT"
            assert route["Properties"]["AuthorizerId"] == {"Ref": authorizer}
            assert "ComputeIntegration" in str(route["Properties"]["Target"])

    def test_compute_function_shape(self, stacks: dict[str, Template]) -> None:
        fn = _function(stacks["api"], "ComputeFunction")["Properties"]
        assert fn["PackageType"] == "Image"
        assert fn["Architectures"] == ["arm64"]
        assert fn["MemorySize"] == 2048
        assert fn["Timeout"] == 28  # under the 30 s HTTP API limit
        env = dict(fn["Environment"]["Variables"])
        assert env.pop("RAW_BUCKET")
        assert env.pop("DERIVED_BUCKET")  # the LFP routes
        assert env.pop("LABELS_TABLE")  # the label aligns
        assert env == {"ATHENA_WORKGROUP": "asteroidea", "GLUE_DATABASE": "asteroidea"}
        stacks["api"].has_resource_properties(
            "AWS::IAM::Role", {"RoleName": "asteroidea-compute-role"}
        )

    def test_compute_role_statements_are_exactly_the_s7_grants(
        self, stacks: dict[str, Template]
    ) -> None:
        statements = policy_statements(stacks["api"], "ComputeFunction")
        glue = [s for s in statements if any(a.startswith("glue:") for a in actions(s))]
        assert len(glue) == 1
        assert actions(glue[0]) == {"glue:GetDatabase", "glue:GetTable"}
        text = _resource_text(glue[0])
        assert text.count("table/") == 6
        for table in (
            "assets",
            "series",
            "units",
            "unit_spikes",
            "intervals",
            "events",
        ):
            assert f"table/asteroidea/{table}'" in text
        for table in (
            "datasets",
            "subjects",
            "sessions",
            "electrodes",
            "asset_headers",
        ):
            assert f"table/asteroidea/{table}'" not in text
        raw = [s for s in statements if "RawBucket" in _resource_text(s)]
        assert len(raw) == 1
        assert sorted(raw[0]["Action"]) == ["s3:GetObject", "s3:GetObjectVersion"]
        assert "/raw/*" in _resource_text(raw[0])
        # The label aligns read LabelsTable, read-only.
        table = [
            s for s in statements if any(a.startswith("dynamodb:") for a in actions(s))
        ]
        assert len(table) == 1
        assert actions(table[0]) == {"dynamodb:GetItem", "dynamodb:Query"}
        assert "LabelsTable" in _resource_text(table[0])
        for s in statements:
            assert "landing/" not in _resource_text(s)
            assert not any(a.startswith("sqs:") for a in actions(s))

    def test_labels_routes_are_on_the_jwt_authorizer_and_labels_function(
        self, stacks: dict[str, Template]
    ) -> None:
        t = stacks["api"]
        (authorizer,) = t.find_resources("AWS::ApiGatewayV2::Authorizer")
        routes = [
            r
            for r in t.find_resources("AWS::ApiGatewayV2::Route").values()
            if "/files/" in r["Properties"]["RouteKey"]
        ]
        assert sorted(r["Properties"]["RouteKey"] for r in routes) == [
            "DELETE /files/{asset_id}/labels/{label_id}",
            "GET /files/{asset_id}/labels",
            "PATCH /files/{asset_id}/labels/{label_id}",
            "POST /files/{asset_id}/labels",
        ]
        for route in routes:
            assert route["Properties"]["AuthorizationType"] == "JWT"
            assert route["Properties"]["AuthorizerId"] == {"Ref": authorizer}
            assert "LabelsIntegration" in str(route["Properties"]["Target"])

    def test_labels_function_shape(self, stacks: dict[str, Template]) -> None:
        props = _function(stacks["api"], "LabelsFunction")["Properties"]
        assert props["Handler"] == "asteroidea_ingest.labels_api.handler"
        assert props["Runtime"] == "python3.14"
        assert props["Architectures"] == ["arm64"]
        assert props["Timeout"] == 20  # over the 18 s Athena budget, under 30 s
        env = dict(props["Environment"]["Variables"])
        assert env.pop("LABELS_TABLE")
        assert env == {"ATHENA_WORKGROUP": "asteroidea", "GLUE_DATABASE": "asteroidea"}
        stacks["api"].has_resource_properties(
            "AWS::IAM::Role", {"RoleName": "asteroidea-labels-role"}
        )

    def test_labels_role_statements_are_exactly_the_documented_grants(
        self, stacks: dict[str, Template]
    ) -> None:
        """Athena/Glue read of four tables and three `$snapshots`; LabelsTable."""
        statements = policy_statements(stacks["api"], "LabelsFunction")
        glue = [s for s in statements if any(a.startswith("glue:") for a in actions(s))]
        assert len(glue) == 1
        assert actions(glue[0]) == {"glue:GetDatabase", "glue:GetTable"}
        text = _resource_text(glue[0])
        granted = {"assets", "intervals", "events", "asset_headers"}
        granted |= {f"{t}$snapshots" for t in ("intervals", "events", "asset_headers")}
        assert text.count("table/") == len(granted)
        for table in granted:
            assert f"table/asteroidea/{table}'" in text
        for table in ("datasets", "subjects", "sessions", "series", "units"):
            assert f"table/asteroidea/{table}'" not in text
        # The provenance read is Glue GetTable (granted above, per table, no
        # wildcard) plus S3 GetObject of the Iceberg metadata JSON under the warehouse's
        # `iceberg/` prefix, which the Athena read already grants (nothing added).
        assert "'*'" not in text
        metadata = _by_action(
            stacks["api"], "s3:GetObject", "iceberg/*", "LabelsFunction"
        )
        assert actions(metadata) == {"s3:GetObject"}
        assert "WarehouseBucket" in _resource_text(metadata)
        athena = [
            s for s in statements if any(a.startswith("athena:") for a in actions(s))
        ]
        assert len(athena) == 1
        assert actions(athena[0]) == {
            "athena:StartQueryExecution",
            "athena:GetQueryExecution",
            "athena:GetQueryResults",
            "athena:StopQueryExecution",
        }
        dynamo = [
            s for s in statements if any(a.startswith("dynamodb:") for a in actions(s))
        ]
        assert len(dynamo) == 1
        assert actions(dynamo[0]) == {
            "dynamodb:GetItem",
            "dynamodb:PutItem",
            "dynamodb:UpdateItem",
            "dynamodb:DeleteItem",
            "dynamodb:Query",
        }
        assert "LabelsTable" in _resource_text(dynamo[0])
        assert "/index/" not in _resource_text(dynamo[0])
        for s in statements:
            assert "RawBucket" not in _resource_text(s)  # no raw read: no presign
            assert "landing/" not in _resource_text(s)
            assert not any(a.startswith(("sqs:", "lambda:")) for a in actions(s))

    def test_only_the_labels_role_touches_the_labels_table(
        self, stacks: dict[str, Template]
    ) -> None:
        """Only LabelsRole writes the table; ComputeRole reads it."""
        for prefix in ("SessionFunction", "CatalogFunction"):
            for s in policy_statements(stacks["api"], prefix):
                assert "LabelsTable" not in _resource_text(s), prefix
        for s in policy_statements(stacks["api"], "ComputeFunction"):
            if "LabelsTable" in _resource_text(s):
                assert actions(s) == {"dynamodb:GetItem", "dynamodb:Query"}

    def test_cors_allows_the_web_origin_to_call_get_with_a_bearer_token(
        self, stacks: dict[str, Template]
    ) -> None:
        stacks["api"].has_resource_properties(
            "AWS::ApiGatewayV2::Api",
            {
                "CorsConfiguration": {
                    "AllowOrigins": [
                        "http://localhost:5173",
                        {"Fn::ImportValue": "asteroidea-WebUrl"},
                    ],
                    "AllowMethods": ["GET", "POST", "PATCH", "DELETE"],
                    "AllowHeaders": ["authorization", "content-type"],
                    "MaxAge": 86400,
                }
            },
        )

    def test_authorizer_is_jwt_on_the_pool_and_client(
        self, stacks: dict[str, Template]
    ) -> None:
        stacks["api"].has_resource_properties(
            "AWS::ApiGatewayV2::Authorizer",
            {
                "AuthorizerType": "JWT",
                "IdentitySource": ["$request.header.Authorization"],
                "JwtConfiguration": {
                    "Issuer": Match.any_value(),
                    "Audience": Match.any_value(),
                },
            },
        )

    def test_authorizer_audience_is_every_client_on_the_pool_issuer(
        self, stacks: dict[str, Template]
    ) -> None:
        """sdk, web and sandbox-test: a token from a client outside it gets a 401."""
        (auth,) = stacks["api"].find_resources("AWS::ApiGatewayV2::Authorizer").values()
        config = auth["Properties"]["JwtConfiguration"]
        assert len(config["Audience"]) == 3
        assert "cognito-idp" in str(config["Issuer"])

    def test_session_function_gets_the_owners_table_name(
        self, stacks: dict[str, Template]
    ) -> None:
        env = _function(stacks["api"], "SessionFunction")["Properties"]["Environment"]
        assert set(env["Variables"]) == {
            "SESSIONS_TABLE",
            "OWNERS_TABLE",
            "RAW_BUCKET",
            "COMMIT_QUEUE_URL",
        }

    def test_session_function_shape(self, stacks: dict[str, Template]) -> None:
        stacks["api"].has_resource_properties(
            "AWS::Lambda::Function",
            {
                "Runtime": "python3.14",
                "Architectures": ["arm64"],
                "Handler": "asteroidea_ingest.uploads_api.handler",
                "Timeout": 10,
            },
        )

    def test_least_privilege(self, stacks: dict[str, Template]) -> None:
        t = stacks["api"]
        for s in policy_statements(t):
            assert all("*" not in a for a in actions(s)), s
        assert actions(_by_action(t, "dynamodb:PutItem", "SessionsTable")) == {
            "dynamodb:GetItem",
            "dynamodb:PutItem",
            "dynamodb:UpdateItem",
        }
        # The owners table gets read and claim, and nothing else.
        owners = _by_action(t, "dynamodb:PutItem", "DatasetOwnersTable")
        assert actions(owners) == {"dynamodb:GetItem", "dynamodb:PutItem"}
        assert "SessionsTable" not in _resource_text(owners)
        assert "/index/" not in _resource_text(owners)
        presign = _by_action(
            t, "s3:PutObject", "landing/*"
        )  # the presigned landing PUT
        assert actions(presign) == {"s3:PutObject"}
        assert "landing/*" in _resource_text(presign)
        assert "raw/*" not in _resource_text(presign)
        read = _by_action(t, "s3:GetObject", "raw/*", "SessionFunction")
        assert "raw/*" in _resource_text(read)
        assert "landing" not in _resource_text(read)
        query = _by_action(t, "dynamodb:Query", function="SessionFunction")
        assert actions(query) == {"dynamodb:Query"}
        assert "/index/by_owner" in _resource_text(
            query
        )  # the index ARN, not the table
        table_wide = _by_action(t, "dynamodb:PutItem", "SessionsTable")
        assert "/index/" not in _resource_text(table_wide)
        # ImportsRole alone may invoke (ImportPlanFunction).
        invokers = [
            lid
            for lid, policy in t.find_resources("AWS::IAM::Policy").items()
            if any(
                "lambda:InvokeFunction" in actions(st)
                for st in policy["Properties"]["PolicyDocument"]["Statement"]
            )
        ]
        assert len(invokers) == 1
        assert invokers[0].startswith("ImportsFunction")
        send = _by_action(t, "sqs:SendMessage")  # finalize queues the commit
        assert actions(send) == {"sqs:SendMessage"}
        assert "CommitQueue" in _resource_text(send)


def _queues(template: Template) -> dict[str, dict[str, Any]]:
    """Return queue name -> properties for every SQS queue in the template."""
    return {
        q["Properties"]["QueueName"]: q["Properties"]
        for q in template.find_resources("AWS::SQS::Queue").values()
    }


def _logical_id(template: Template, queue_name: str) -> str:
    (found,) = [
        lid
        for lid, q in template.find_resources("AWS::SQS::Queue").items()
        if q["Properties"]["QueueName"] == queue_name
    ]
    return found


class TestWriterQueues:
    """The single writer (FIFO single writer)."""

    NAMES = (
        "asteroidea-commit.fifo",
        "asteroidea-extract.fifo",
        "asteroidea-commit-dlq.fifo",
        "asteroidea-extract-dlq.fifo",
    )

    def test_every_queue_is_fifo_sse_sqs_without_content_dedup(
        self, stacks: dict[str, Template]
    ) -> None:
        queues = _queues(stacks["ingest"])
        # The LFP build queues are standard, pinned in TestLfpBuild.
        assert set(queues) == {*self.NAMES, *LFP_QUEUES}
        for name in self.NAMES:
            props = queues[name]
            assert props["FifoQueue"] is True, name
            assert props["ContentBasedDeduplication"] is False, name
            assert props["SqsManagedSseEnabled"] is True, name

    def test_retention_and_redrive(self, stacks: dict[str, Template]) -> None:
        t = stacks["ingest"]
        queues = _queues(t)
        for main in ("commit", "extract"):
            props = queues[f"asteroidea-{main}.fifo"]
            assert props["MessageRetentionPeriod"] == 4 * 86400
            redrive = props["RedrivePolicy"]
            assert redrive["maxReceiveCount"] == 2
            dlq = _logical_id(t, f"asteroidea-{main}-dlq.fifo")
            assert redrive["deadLetterTargetArn"] == {"Fn::GetAtt": [dlq, "Arn"]}
            assert queues[f"asteroidea-{main}-dlq.fifo"]["MessageRetentionPeriod"] == (
                14 * 86400
            )

    def test_visibility_is_at_least_six_times_the_consumer_timeout(
        self, stacks: dict[str, Template]
    ) -> None:
        queues = _queues(stacks["ingest"])
        function = _function(stacks["ingest"], COMMIT_FN)
        commit_timeout = function["Properties"]["Timeout"]
        commit_vis = queues["asteroidea-commit.fifo"]["VisibilityTimeout"]
        assert commit_vis == 720
        assert commit_vis >= 6 * commit_timeout
        extract_vis = queues["asteroidea-extract.fifo"]["VisibilityTimeout"]
        assert extract_vis == 720
        assert extract_vis >= 6 * timing.EXTRACT_TIMEOUT_SECONDS

    def test_deployed_timeouts_and_visibilities_satisfy_every_lease(
        self, stacks: dict[str, Template]
    ) -> None:
        """The lease rule from the synthesized values: timeout < lease < visibility."""
        t = stacks["ingest"]
        queues = _queues(t)
        commit_t = _function(t, COMMIT_FN)["Properties"]["Timeout"]
        extract_t = _function(t, EXTRACT_FN)["Properties"]["Timeout"]
        commit_v = queues["asteroidea-commit.fifo"]["VisibilityTimeout"]
        extract_v = queues["asteroidea-extract.fifo"]["VisibilityTimeout"]
        lfp_t = _function(t, LFP_FN)["Properties"]["Timeout"]
        lfp_v = queues["asteroidea-lfp-build"]["VisibilityTimeout"]
        assert (commit_t, extract_t, lfp_t) == (120, 120, 900)
        for name, lease, _, _ in timing.LEASES:
            if name.startswith("commit"):
                timeout, visibility = commit_t, commit_v
            elif name.startswith("lfp"):  # its own queue and row
                timeout, visibility = lfp_t, lfp_v
            else:
                timeout, visibility = extract_t, extract_v
            assert timeout < lease < visibility, name

    def test_one_event_source_mapping_per_writer_with_batch_size_one(
        self, stacks: dict[str, Template]
    ) -> None:
        t = stacks["ingest"]
        mappings = list(t.find_resources("AWS::Lambda::EventSourceMapping").values())
        # Commit queue and extract queue (ExtractFunction's mapping), and the
        # LFP build queue (not a catalog writer).
        assert len(mappings) == 3
        assert {m["Properties"]["BatchSize"] for m in mappings} == {1}
        sources = {m["Properties"]["EventSourceArn"]["Fn::GetAtt"][0] for m in mappings}
        assert sources == {
            _logical_id(t, "asteroidea-commit.fifo"),
            _logical_id(t, "asteroidea-extract.fifo"),
            _logical_id(t, "asteroidea-lfp-build"),
        }

    def test_every_queue_refuses_requests_without_tls(
        self, stacks: dict[str, Template]
    ) -> None:
        t = stacks["ingest"]
        denied: set[str] = set()
        for policy in t.find_resources("AWS::SQS::QueuePolicy").values():
            for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
                if statement["Effect"] == "Deny" and statement["Condition"] == {
                    "Bool": {"aws:SecureTransport": "false"}
                }:
                    denied.update(str(q) for q in policy["Properties"]["Queues"])
        for name in (*self.NAMES, *LFP_QUEUES):
            assert _logical_id(t, name) in " ".join(denied), name

    def test_commit_role_sends_only_to_the_extract_queue(
        self, stacks: dict[str, Template]
    ) -> None:
        t = stacks["ingest"]
        send = _by_action(t, "sqs:SendMessage", function=COMMIT_FN)
        assert actions(send) == {"sqs:SendMessage"}
        assert send["Resource"] == {
            "Fn::GetAtt": [_logical_id(t, "asteroidea-extract.fifo"), "Arn"]
        }

    def test_commit_environment_names_the_extract_queue(
        self, stacks: dict[str, Template]
    ) -> None:
        t = stacks["ingest"]
        function = _function(t, COMMIT_FN)
        env = function["Properties"]["Environment"]["Variables"]
        assert env["EXTRACT_QUEUE_URL"] == {
            "Ref": _logical_id(t, "asteroidea-extract.fifo")
        }


def _environment(context: dict[str, str], name: str) -> dict[str, Any]:
    app = App(context=context)
    data = DataStack(app, "DataStack", env=ENV)
    services = ServicesStack(app, "ServicesStack", env=ENV, data=data)
    template = Template.from_stack(services.ingest)
    function = _function(template, name)
    return dict(function["Properties"]["Environment"]["Variables"])


@pytest.mark.parametrize("name", [COMMIT_FN, EXTRACT_FN])
def test_cold_nonce_context_changes_the_environment_only_when_set(name: str) -> None:
    """`-c cold-nonce=n` forces a cold start; unset changes nothing."""
    assert "COLD_NONCE" not in _environment({}, name)
    assert _environment({"cold-nonce": "7"}, name)["COLD_NONCE"] == "7"


def test_commit_role_sets_and_reads_retention_and_nothing_bypasses_it(
    stacks: dict[str, Template],
) -> None:
    """Retention on `raw/*`; no statement anywhere grants bypass."""
    t = stacks["ingest"]
    retention = _by_action(t, "s3:PutObjectRetention", "raw/*", COMMIT_FN)
    assert actions(retention) == {"s3:PutObjectRetention", "s3:GetObjectRetention"}
    assert "landing" not in _resource_text(retention)
    for template in stacks.values():
        text = json.dumps(template.to_json())
        assert "BypassGovernanceRetention" not in text


def test_commit_function_carries_the_retention_setting() -> None:
    """Default 1; `-c raw-retention-days=7` gives 7; below 1 is refused."""
    assert _environment({}, COMMIT_FN)["RAW_RETENTION_DAYS"] == "1"
    assert (
        _environment({"raw-retention-days": "7"}, COMMIT_FN)["RAW_RETENTION_DAYS"]
        == "7"
    )
    with pytest.raises(ValueError, match="raw-retention-days"):
        _environment({"raw-retention-days": "0"}, COMMIT_FN)


class TestImport:
    """ImportFunction and ImportRole."""

    def test_import_function_shape(self, stacks: dict[str, Template]) -> None:
        props = _function(stacks["ingest"], IMPORT_FN)["Properties"]
        assert props["PackageType"] == "Image"
        assert props["Architectures"] == ["arm64"]
        assert props["MemorySize"] == 1024
        assert props["Timeout"] == 900
        assert props["ImageConfig"] == {
            "Command": ["asteroidea_ingest.import_file.handler"]
        }
        commit = _function(stacks["ingest"], COMMIT_FN)["Properties"]
        assert props["Code"] == commit["Code"]  # one image, CMD override only
        env = props["Environment"]["Variables"]
        assert set(env) == {
            "SESSIONS_TABLE",
            "OWNERS_TABLE",
            "RAW_BUCKET",
            "COMMIT_QUEUE_URL",
        }

    def test_import_role_statements_are_exactly_pinned(
        self, stacks: dict[str, Template]
    ) -> None:
        """Pinned exactly: any added or missing statement fails."""
        t = stacks["ingest"]
        raw_arn = {
            "Fn::ImportValue": (
                "DataStack:ExportsOutputFnGetAttRawBucket0C3EE094ArnD2F95F99"
            )
        }
        queue = {"Fn::GetAtt": [_logical_id(t, "asteroidea-commit.fifo"), "Arn"]}
        table = {
            "Fn::ImportValue": (
                "DataStack:ExportsOutputFnGetAttSessionsTable7C302024Arn24FF8012"
            )
        }

        owners = {
            "Fn::ImportValue": (
                "DataStack:ExportsOutputFnGetAttDatasetOwnersTable9D0BA8A1Arn4A11628C"
            )
        }

        def objects(prefix: str) -> dict[str, Any]:
            return {"Fn::Join": ["", [raw_arn, f"/{prefix}"]]}

        expected = [
            {
                "Action": "s3:GetObject",
                "Effect": "Allow",
                "Resource": "arn:aws:s3:::openneuro.org/*",
            },
            {
                "Action": "s3:PutObject",
                "Effect": "Allow",
                "Resource": objects("landing/*"),
            },
            {"Action": "s3:GetObject", "Effect": "Allow", "Resource": objects("raw/*")},
            {
                "Action": "s3:ListBucket",
                "Condition": {"StringLike": {"s3:prefix": ["landing/*", "raw/*"]}},
                "Effect": "Allow",
                "Resource": raw_arn,
            },
            {
                "Action": [
                    "dynamodb:PutItem",
                    "dynamodb:GetItem",
                    "dynamodb:UpdateItem",
                ],
                "Effect": "Allow",
                "Resource": table,
            },
            {
                "Action": ["dynamodb:GetItem", "dynamodb:PutItem"],
                "Effect": "Allow",
                "Resource": owners,
            },
            {"Action": "sqs:SendMessage", "Effect": "Allow", "Resource": queue},
        ]
        assert policy_statements(t, IMPORT_FN) == expected

    def test_no_dandiarchive_grant_and_no_tagging(
        self, stacks: dict[str, Template]
    ) -> None:
        text = str(policy_statements(stacks["ingest"], IMPORT_FN))
        assert "dandiarchive" not in text  # DANDI over anonymous HTTPS
        assert "Tagging" not in text  # TaggingDirective REPLACE needs none


IMPORT_PLAN_FN = "ImportPlanFunction"


class TestImports:
    """ImportsFunction, ImportsRole and the /imports routes."""

    def test_the_handler_queries_the_index_the_table_defines(self) -> None:
        assert imports_api.RUNS_CALLER_INDEX == storage.RUNS_CALLER_INDEX

    def test_routes_are_on_the_jwt_authorizer_and_imports_function(
        self, stacks: dict[str, Template]
    ) -> None:
        t = stacks["api"]
        (authorizer,) = t.find_resources("AWS::ApiGatewayV2::Authorizer")
        routes = [
            r
            for r in t.find_resources("AWS::ApiGatewayV2::Route").values()
            if "/imports" in r["Properties"]["RouteKey"]
        ]
        assert sorted(r["Properties"]["RouteKey"] for r in routes) == [
            "GET /imports",
            "GET /imports/{run_id}",
            "POST /imports",
        ]
        for route in routes:
            assert route["Properties"]["AuthorizationType"] == "JWT"
            assert route["Properties"]["AuthorizerId"] == {"Ref": authorizer}
            assert "ImportsIntegration" in str(route["Properties"]["Target"])

    def test_imports_function_shape(self, stacks: dict[str, Template]) -> None:
        props = _function(stacks["api"], "ImportsFunction")["Properties"]
        assert props["Handler"] == "asteroidea_ingest.imports_api.handler"
        assert props["Runtime"] == "python3.14"
        assert props["Architectures"] == ["arm64"]
        assert props["Timeout"] == 10  # three DynamoDB calls and one async invoke
        env = props["Environment"]["Variables"]
        assert set(env) == {"RUNS_TABLE", "SESSIONS_TABLE", "IMPORT_PLAN_FUNCTION_NAME"}
        stacks["api"].has_resource_properties(
            "AWS::IAM::Role", {"RoleName": "asteroidea-imports-role"}
        )

    def test_imports_role_statements_are_exactly_the_documented_grants(
        self, stacks: dict[str, Template]
    ) -> None:
        """Pinned exactly: any added or missing statement fails."""
        runs = {
            "Fn::ImportValue": (
                "DataStack:ExportsOutputFnGetAttImportRunsTable530F2D0DArn6331C955"
            )
        }
        sessions_arn = {
            "Fn::ImportValue": (
                "DataStack:ExportsOutputFnGetAttSessionsTable7C302024Arn24FF8012"
            )
        }
        planner = (
            "referencetoServicesStackIngestNestedStackIngestNestedStackResource"
            "37D4D433OutputsServicesStackIngestImportPlanFunctionD13ABA49Arn"
        )
        assert policy_statements(stacks["api"], "ImportsFunction") == [
            {
                "Action": "lambda:InvokeFunction",
                "Effect": "Allow",
                "Resource": {"Ref": planner},
            },
            {
                "Action": ["dynamodb:GetItem", "dynamodb:PutItem"],
                "Effect": "Allow",
                "Resource": runs,
            },
            {
                "Action": "dynamodb:Query",
                "Effect": "Allow",
                "Resource": {"Fn::Join": ["", [runs, "/index/by_caller"]]},
            },
            {
                "Action": "dynamodb:BatchGetItem",
                "Effect": "Allow",
                "Resource": sessions_arn,
            },
        ]

    def test_no_other_api_role_touches_the_runs_table(
        self, stacks: dict[str, Template]
    ) -> None:
        for prefix in (
            "SessionFunction",
            "CatalogFunction",
            "ComputeFunction",
            "LabelsFunction",
            "PlatformFunction",
        ):
            for s in policy_statements(stacks["api"], prefix):
                assert "ImportRunsTable" not in _resource_text(s), prefix
                assert "lambda:" not in str(actions(s)), prefix


class TestImportPlan:
    """ImportPlanFunction and its role."""

    def test_import_plan_function_shape(self, stacks: dict[str, Template]) -> None:
        t = stacks["ingest"]
        props = _function(t, IMPORT_PLAN_FN)["Properties"]
        assert props["PackageType"] == "Image"
        assert props["Architectures"] == ["arm64"]
        assert props["MemorySize"] == 1024
        assert props["Timeout"] == timing.IMPORT_PLAN_TIMEOUT_SECONDS == 300
        assert props["ImageConfig"] == {
            "Command": ["asteroidea_ingest.import_plan.handler"]
        }
        commit = _function(t, COMMIT_FN)["Properties"]
        assert props["Code"] == commit["Code"]  # the commit image, CMD override only
        env = props["Environment"]["Variables"]
        assert set(env) == {"RUNS_TABLE", "IMPORT_FUNCTION_NAME"}
        assert env["IMPORT_FUNCTION_NAME"]["Ref"].startswith(IMPORT_FN)
        assert "VpcConfig" not in props  # outbound HTTPS to the archives

    def test_import_plan_role_statements_are_exactly_pinned(
        self, stacks: dict[str, Template]
    ) -> None:
        """Pinned exactly: any added or missing statement fails."""
        t = stacks["ingest"]
        (import_fn,) = [
            lid
            for lid in t.find_resources("AWS::Lambda::Function")
            if lid.startswith(IMPORT_FN)
        ]
        runs = {
            "Fn::ImportValue": (
                "DataStack:ExportsOutputFnGetAttImportRunsTable530F2D0DArn6331C955"
            )
        }
        warehouse = {
            "Fn::ImportValue": (
                "DataStack:ExportsOutputFnGetAttWarehouseBucket0E783396ArnD66BFFC9"
            )
        }

        def glue(resource: str) -> dict[str, Any]:
            return {
                "Fn::Join": [
                    "",
                    [
                        "arn:",
                        {"Ref": "AWS::Partition"},
                        f":glue:us-east-1:000000000000:{resource}",
                    ],
                ]
            }

        expected = [
            {
                "Action": "lambda:InvokeFunction",
                "Effect": "Allow",
                "Resource": {"Fn::GetAtt": [import_fn, "Arn"]},
            },
            {"Action": "dynamodb:UpdateItem", "Effect": "Allow", "Resource": runs},
            {
                "Action": "glue:GetTable",
                "Effect": "Allow",
                "Resource": [
                    glue("catalog"),
                    glue("database/asteroidea"),
                    glue("table/asteroidea/assets"),
                ],
            },
            {
                "Action": "s3:GetObject",
                "Effect": "Allow",
                "Resource": {"Fn::Join": ["", [warehouse, "/iceberg/*"]]},
            },
        ]
        assert policy_statements(t, IMPORT_PLAN_FN) == expected


class TestExtract:
    """ExtractFunction and ExtractRole."""

    def test_extract_function_shape(self, stacks: dict[str, Template]) -> None:
        t = stacks["ingest"]
        props = _function(t, EXTRACT_FN)["Properties"]
        assert props["PackageType"] == "Image"
        assert props["Architectures"] == ["arm64"]
        assert props["MemorySize"] == 2048
        assert props["Timeout"] == 120
        assert "ImageConfig" not in props  # the image's own CMD
        env = props["Environment"]["Variables"]
        # LFP_BUILD_QUEUE_URL: the build trigger; DERIVED_BUCKET: the
        # rows-by-pointer bundles.
        assert set(env) == {
            "SESSIONS_TABLE",
            "RAW_BUCKET",
            "LFP_BUILD_QUEUE_URL",
            "DERIVED_BUCKET",
        }
        assert env["LFP_BUILD_QUEUE_URL"] == {
            "Ref": _logical_id(t, "asteroidea-lfp-build")
        }
        commit = _function(t, COMMIT_FN)["Properties"]
        assert (
            props["Code"] != commit["Code"]
        )  # its own image (ingest/Dockerfile.extract)

    def test_the_extract_queue_feeds_the_extract_function(
        self, stacks: dict[str, Template]
    ) -> None:
        t = stacks["ingest"]
        extract_queue = _logical_id(t, "asteroidea-extract.fifo")
        (mapping,) = [
            m["Properties"]
            for m in t.find_resources("AWS::Lambda::EventSourceMapping").values()
            if m["Properties"]["EventSourceArn"]["Fn::GetAtt"][0] == extract_queue
        ]
        assert mapping["BatchSize"] == 1
        assert mapping["FunctionName"]["Ref"].startswith(EXTRACT_FN)

    def test_extract_role_statements_are_exactly_pinned(
        self, stacks: dict[str, Template]
    ) -> None:
        """Pinned exactly, Glue ARNs one by one: any widening or addition fails."""
        t = stacks["ingest"]
        raw_arn = {
            "Fn::ImportValue": (
                "DataStack:ExportsOutputFnGetAttRawBucket0C3EE094ArnD2F95F99"
            )
        }
        warehouse_arn = {
            "Fn::ImportValue": (
                "DataStack:ExportsOutputFnGetAttWarehouseBucket0E783396ArnD66BFFC9"
            )
        }
        table = {
            "Fn::ImportValue": (
                "DataStack:ExportsOutputFnGetAttSessionsTable7C302024Arn24FF8012"
            )
        }
        queue = {"Fn::GetAtt": [_logical_id(t, "asteroidea-extract.fifo"), "Arn"]}

        def glue(resource: str) -> dict[str, Any]:
            return {
                "Fn::Join": [
                    "",
                    [
                        "arn:",
                        {"Ref": "AWS::Partition"},
                        f":glue:us-east-1:000000000000:{resource}",
                    ],
                ]
            }

        expected = [
            {
                "Action": [
                    "sqs:ReceiveMessage",
                    "sqs:ChangeMessageVisibility",
                    "sqs:GetQueueUrl",
                    "sqs:DeleteMessage",
                    "sqs:GetQueueAttributes",
                ],
                "Effect": "Allow",
                "Resource": queue,
            },
            {
                "Action": ["s3:GetObject", "s3:GetObjectVersion"],
                "Effect": "Allow",
                "Resource": {"Fn::Join": ["", [raw_arn, "/raw/*"]]},
            },
            {
                "Action": "s3:ListBucket",
                "Condition": {"StringLike": {"s3:prefix": ["raw/*"]}},
                "Effect": "Allow",
                "Resource": raw_arn,
            },
            {
                "Action": ["glue:GetDatabase", "glue:GetTable", "glue:UpdateTable"],
                "Effect": "Allow",
                "Resource": [
                    glue("catalog"),
                    glue("database/asteroidea"),
                    *(glue(f"table/asteroidea/{name}") for name in EXTRACT_TABLES),
                ],
            },
            {
                "Action": ["s3:PutObject", "s3:GetObject"],
                "Effect": "Allow",
                "Resource": {"Fn::Join": ["", [warehouse_arn, "/iceberg/*"]]},
            },
            {"Action": "s3:ListBucket", "Effect": "Allow", "Resource": warehouse_arn},
            {
                "Action": ["dynamodb:GetItem", "dynamodb:UpdateItem"],
                "Effect": "Allow",
                "Resource": table,
            },
            {  # one LFP build message per NWB extraction
                "Action": "sqs:SendMessage",
                "Effect": "Allow",
                "Resource": {
                    "Fn::GetAtt": [_logical_id(t, "asteroidea-lfp-build"), "Arn"]
                },
            },
            {  # a rows-by-pointer message's manifest and bundle (rows/ only)
                "Action": "s3:GetObject",
                "Effect": "Allow",
                "Resource": {"Fn::Join": ["", [DERIVED_ARN, "/rows/*"]]},
            },
            {
                "Action": "s3:ListBucket",
                "Condition": {"StringLike": {"s3:prefix": ["rows/*"]}},
                "Effect": "Allow",
                "Resource": DERIVED_ARN,
            },
        ]
        assert policy_statements(t, EXTRACT_FN) == expected

    def test_the_nine_extraction_tables_are_named_and_distinct(self) -> None:
        assert sorted(EXTRACT_TABLES) == sorted(
            [
                "subjects",
                "sessions",
                "asset_headers",
                "series",
                "electrodes",
                "units",
                "intervals",
                "unit_spikes",
                "events",
            ]
        )

    def test_extract_role_writes_no_raw_object_and_has_no_wildcard_glue(
        self, stacks: dict[str, Template]
    ) -> None:
        text = str(policy_statements(stacks["ingest"], EXTRACT_FN))
        assert "s3:PutObject" in text  # the warehouse only
        for statement in policy_statements(stacks["ingest"], EXTRACT_FN):
            if "glue:GetTable" in actions(statement):
                assert "*" not in _resource_text(statement)
            if "s3:PutObject" in actions(statement):
                assert "raw" not in _resource_text(statement).lower()


DERIVED_ARN: Final = {
    "Fn::ImportValue": (
        "DataStack:ExportsOutputFnGetAttDerivedBucket81C7992CArnF73A8B22"
    )
}


class TestLfpBuild:
    """LfpBuildFunction, its queue, DLQ, role and the derived store."""

    def test_function_shape(self, stacks: dict[str, Template]) -> None:
        props = _function(stacks["ingest"], LFP_FN)["Properties"]
        assert props["PackageType"] == "Image"
        assert props["Architectures"] == ["arm64"]
        assert props["MemorySize"] == 1024  # the measured peak plus BlockFile's cache
        assert props["Timeout"] == timing.LFP_BUILD_TIMEOUT_SECONDS == 900
        assert props["ImageConfig"] == {
            "Command": ["asteroidea_ingest.lfp_build.handler"]
        }
        env = dict(props["Environment"]["Variables"])
        assert env.pop("RAW_BUCKET")
        assert env.pop("DERIVED_BUCKET")
        # The summary kinds' Athena read and the session fan-out.
        assert env == {
            "ATHENA_WORKGROUP": "asteroidea",
            "GLUE_DATABASE": "asteroidea",
            "LFP_BUILD_QUEUE_URL": {
                "Ref": _logical_id(stacks["ingest"], "asteroidea-lfp-build")
            },
        }
        stacks["ingest"].has_resource_properties(
            "AWS::IAM::Role", {"RoleName": "asteroidea-lfp-build-role"}
        )

    def test_it_runs_the_compute_image(self, stacks: dict[str, Template]) -> None:
        """Same image asset as ComputeFunction (`ingest/Dockerfile.compute`)."""
        lfp = _function(stacks["ingest"], LFP_FN)["Properties"]["Code"]["ImageUri"]
        compute = _function(stacks["api"], "ComputeFunction")["Properties"]["Code"]
        extract = _function(stacks["ingest"], EXTRACT_FN)["Properties"]["Code"]
        assert lfp == compute["ImageUri"]
        assert lfp != extract["ImageUri"]

    def test_its_queue_is_standard_with_a_dlq_and_six_times_the_timeout(
        self, stacks: dict[str, Template]
    ) -> None:
        t = stacks["ingest"]
        queues = _queues(t)
        main, dlq = (queues[name] for name in LFP_QUEUES)
        for props in (main, dlq):
            assert "FifoQueue" not in props  # not the extract FIFO
            assert props["SqsManagedSseEnabled"] is True
        assert main["VisibilityTimeout"] == 5400 == 6 * 900
        assert main["MessageRetentionPeriod"] == 4 * 86400
        assert dlq["MessageRetentionPeriod"] == 14 * 86400
        assert main["RedrivePolicy"] == {
            "deadLetterTargetArn": {
                "Fn::GetAtt": [_logical_id(t, "asteroidea-lfp-build-dlq"), "Arn"]
            },
            "maxReceiveCount": 2,
        }
        (mapping,) = [
            m["Properties"]
            for m in t.find_resources("AWS::Lambda::EventSourceMapping").values()
            if m["Properties"]["EventSourceArn"]["Fn::GetAtt"][0]
            == _logical_id(t, "asteroidea-lfp-build")
        ]
        assert mapping["BatchSize"] == 1
        assert mapping["FunctionName"]["Ref"].startswith(LFP_FN)

    def test_role_statements_are_exactly_pinned(
        self, stacks: dict[str, Template]
    ) -> None:
        t = stacks["ingest"]
        raw_arn = {
            "Fn::ImportValue": (
                "DataStack:ExportsOutputFnGetAttRawBucket0C3EE094ArnD2F95F99"
            )
        }
        queue = {"Fn::GetAtt": [_logical_id(t, "asteroidea-lfp-build"), "Arn"]}
        statements = policy_statements(t, LFP_FN)
        # The shared Athena read (identity.py) closes the list; the summary tables
        # are pinned in the next test, the rest equals ComputeRole's copy.
        query_read = statements[6:]
        compute = policy_statements(stacks["api"], "ComputeFunction")
        compute_read = [
            s for s in compute[:6] if not any(a.startswith("glue:") for a in actions(s))
        ]
        assert [
            s for s in query_read if not any(a.startswith("glue:") for a in actions(s))
        ] == compute_read
        assert len(query_read) == 6
        assert statements[:6] == [
            {
                "Action": [
                    "sqs:ReceiveMessage",
                    "sqs:ChangeMessageVisibility",
                    "sqs:GetQueueUrl",
                    "sqs:DeleteMessage",
                    "sqs:GetQueueAttributes",
                ],
                "Effect": "Allow",
                "Resource": queue,
            },
            {
                "Action": ["s3:GetObject", "s3:GetObjectVersion"],
                "Effect": "Allow",
                "Resource": {"Fn::Join": ["", [raw_arn, "/raw/*"]]},
            },
            {  # the derived store's lfp/ and summary/ writer
                "Action": "s3:PutObject",
                "Effect": "Allow",
                "Resource": [
                    {"Fn::Join": ["", [DERIVED_ARN, "/lfp/*"]]},
                    {"Fn::Join": ["", [DERIVED_ARN, "/summary/*"]]},
                ],
            },
            {  # LFP manifests and the stored summary (the conditional write)
                "Action": "s3:GetObject",
                "Effect": "Allow",
                "Resource": [
                    {"Fn::Join": ["", [DERIVED_ARN, "/lfp/*"]]},
                    {"Fn::Join": ["", [DERIVED_ARN, "/summary/*"]]},
                ],
            },
            {  # a missing key reads NoSuchKey, not AccessDenied
                "Action": "s3:ListBucket",
                "Condition": {"StringLike": {"s3:prefix": ["lfp/*", "summary/*"]}},
                "Effect": "Allow",
                "Resource": DERIVED_ARN,
            },
            {  # the per-session fan-out to its own queue
                "Action": "sqs:SendMessage",
                "Effect": "Allow",
                "Resource": queue,
            },
        ]

    def test_role_reads_every_table_the_summary_queries(
        self, stacks: dict[str, Template]
    ) -> None:
        """Each `FROM <table>` the summary builder runs is granted, no other."""
        sources = (
            INGEST_SRC / "asteroidea_ingest" / "dataset_summary.py",
            INGEST_SRC / "asteroidea_ingest" / "compute_api.py",
        )
        queried = {
            m.group(1)
            for path in sources
            for m in re.finditer(r"\bFROM\s+([a-z_]+)\b", path.read_text())
        }
        assert queried == SUMMARY_TABLES_ORACLE
        statements = policy_statements(stacks["ingest"], LFP_FN)
        (glue,) = [s for s in statements if "glue:GetTable" in actions(s)]
        assert actions(glue) == {"glue:GetDatabase", "glue:GetTable"}
        granted = set(re.findall(r"table/asteroidea/([a-z_$]+)", json.dumps(glue)))
        assert granted == SUMMARY_TABLES_ORACLE
        (athena,) = [
            s for s in statements if any(a.startswith("athena:") for a in actions(s))
        ]
        assert "workgroup/asteroidea" in _resource_text(athena)

    def test_compute_role_reads_the_derived_lfp_and_summary_prefixes_only(
        self, stacks: dict[str, Template]
    ) -> None:
        """Read and list only: LfpBuildRole stays the one writer of both."""
        statements = policy_statements(stacks["api"], "ComputeFunction")
        derived = [s for s in statements if "DerivedBucket" in _resource_text(s)]
        assert derived == [
            {
                "Action": "s3:GetObject",
                "Effect": "Allow",
                "Resource": [
                    {"Fn::Join": ["", [DERIVED_ARN, "/lfp/*"]]},
                    {"Fn::Join": ["", [DERIVED_ARN, "/summary/*"]]},
                ],
            },
            {
                "Action": "s3:ListBucket",
                "Condition": {"StringLike": {"s3:prefix": ["lfp/*", "summary/*"]}},
                "Effect": "Allow",
                "Resource": DERIVED_ARN,
            },
        ]

    def test_no_other_function_touches_the_derived_bucket(
        self, stacks: dict[str, Template]
    ) -> None:
        for name, template in stacks.items():
            for policy_id, policy in template.find_resources(
                "AWS::IAM::Policy"
            ).items():
                text = str(policy["Properties"]["PolicyDocument"])
                if "DerivedBucket" in text:
                    # It adds DeriveFunction (put rows/*), ExtractFunction
                    # (get rows/*) and CatalogFunction (get the dataset summaries);
                    # each is pinned exactly in its own test.
                    allowed = (
                        LFP_FN,
                        "ComputeFunction",
                        DERIVE_FN,
                        EXTRACT_FN,
                        "CatalogFunction",
                    )
                    assert policy_id.startswith(allowed), (
                        name,
                        policy_id,
                    )


DERIVE_FN: Final = "DeriveFunction"
SESSIONS_ARN: Final = {
    "Fn::ImportValue": "DataStack:ExportsOutputFnGetAttSessionsTable7C302024Arn24FF8012"
}


def _token(node: dict[str, Any]) -> str:
    """Name a CloudFormation token inside a definition string: `@<logical id>`."""
    if "Fn::GetAtt" in node:
        return "@" + ".".join(str(part) for part in node["Fn::GetAtt"])
    if "Ref" in node:
        return f"@{node['Ref']}"
    if "Fn::ImportValue" in node:
        return f"@import:{node['Fn::ImportValue']}"
    raise AssertionError(node)


def _machine(template: Template) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return the state machine's properties and its parsed definition."""
    (props,) = [
        m["Properties"]
        for m in template.find_resources("AWS::StepFunctions::StateMachine").values()
    ]
    parts = props["DefinitionString"]["Fn::Join"][1]
    text = "".join(p if isinstance(p, str) else _token(p) for p in parts)
    return props, json.loads(text)


class TestDerive:
    """DeriveFunction (the extract image) and its role."""

    def test_function_shape(self, stacks: dict[str, Template]) -> None:
        t = stacks["ingest"]
        props = _function(t, DERIVE_FN)["Properties"]
        assert props["PackageType"] == "Image"
        assert props["Architectures"] == ["arm64"]
        assert props["MemorySize"] == 2048  # as ExtractFunction: the same extractors
        assert props["Timeout"] == timing.DERIVE_TIMEOUT_SECONDS == 300
        assert props["ImageConfig"] == {
            "Command": ["asteroidea_ingest.extract.derive.handler"]
        }
        env = props["Environment"]["Variables"]
        assert set(env) == {"SESSIONS_TABLE", "RAW_BUCKET", "DERIVED_BUCKET"}
        t.has_resource_properties(
            "AWS::IAM::Role", {"RoleName": "asteroidea-derive-role"}
        )
        extract = _function(t, EXTRACT_FN)["Properties"]["Code"]
        assert props["Code"] == extract  # the extract image, no new image
        assert not [
            m
            for m in t.find_resources("AWS::Lambda::EventSourceMapping").values()
            if m["Properties"]["FunctionName"]["Ref"].startswith(DERIVE_FN)
        ]  # no queue: the state machine invokes it

    def test_role_statements_are_exactly_pinned(
        self, stacks: dict[str, Template]
    ) -> None:
        """No Glue, no catalog, no queue: raw read, rows/ put, the FAILED write."""
        raw_arn = {
            "Fn::ImportValue": (
                "DataStack:ExportsOutputFnGetAttRawBucket0C3EE094ArnD2F95F99"
            )
        }
        assert policy_statements(stacks["ingest"], DERIVE_FN) == [
            {
                "Action": ["s3:GetObject", "s3:GetObjectVersion"],
                "Effect": "Allow",
                "Resource": {"Fn::Join": ["", [raw_arn, "/raw/*"]]},
            },
            {
                "Action": "s3:ListBucket",
                "Condition": {"StringLike": {"s3:prefix": ["raw/*"]}},
                "Effect": "Allow",
                "Resource": raw_arn,
            },
            {
                "Action": "s3:PutObject",
                "Effect": "Allow",
                "Resource": {"Fn::Join": ["", [DERIVED_ARN, "/rows/*"]]},
            },
            {
                "Action": "dynamodb:UpdateItem",
                "Effect": "Allow",
                "Resource": SESSIONS_ARN,
            },
        ]


class TestReextractStateMachine:
    """Map(Derive -> SendPointer), bounded, per-item failures caught."""

    def test_standard_logged_traced_and_named(
        self, stacks: dict[str, Template]
    ) -> None:
        props, _ = _machine(stacks["ingest"])
        assert props["StateMachineName"] == "asteroidea-reextract"
        assert props["StateMachineType"] == "STANDARD"
        assert props["TracingConfiguration"] == {"Enabled": True}  # SF2
        logging = props["LoggingConfiguration"]
        assert logging["Level"] == "ALL"  # SF1
        assert logging["IncludeExecutionData"] is True
        (log_group,) = stacks["ingest"].find_resources("AWS::Logs::LogGroup").values()
        assert log_group["Properties"]["RetentionInDays"] == 30

    def test_one_bounded_map_over_the_assets(self, stacks: dict[str, Template]) -> None:
        _, definition = _machine(stacks["ingest"])
        assert definition["StartAt"] == "PerAsset"
        assert set(definition["States"]) == {"PerAsset"}
        fan_out = definition["States"]["PerAsset"]
        assert fan_out["Type"] == "Map"
        assert fan_out["ItemsPath"] == "$.assets"
        assert fan_out["MaxConcurrency"] == reextract_flow.MAX_CONCURRENCY == 4
        assert fan_out["ItemProcessor"]["ProcessorConfig"] == {"Mode": "INLINE"}
        assert definition["TimeoutSeconds"] == 6 * 3600

    def test_each_item_derives_then_sends_one_pointer(
        self, stacks: dict[str, Template]
    ) -> None:
        t = stacks["ingest"]
        _, definition = _machine(t)
        states = definition["States"]["PerAsset"]["ItemProcessor"]["States"]
        assert set(states) == {
            "Derive",
            "Derived?",
            "SendPointer",
            "DeriveRecordedFailure",
            "RecordFailed",
            "RecordFailedLost",
        }
        derive = states["Derive"]
        assert derive["Resource"].startswith(f"@{DERIVE_FN}")
        assert derive["ResultPath"] == "$.derive"
        assert derive["Next"] == "Derived?"
        choice = states["Derived?"]
        assert choice["Choices"] == [
            {
                "Variable": "$.derive.outcome",
                "StringEquals": "DERIVED",
                "Next": "SendPointer",
            }
        ]
        assert choice["Default"] == "DeriveRecordedFailure"  # ERROR: nothing sent
        send = states["SendPointer"]
        assert send["Resource"] == "arn:@AWS::Partition:states:::sqs:sendMessage"
        assert send["Parameters"] == {
            "QueueUrl": "@" + _logical_id(t, "asteroidea-extract.fifo"),
            "MessageBody": {
                "asset_id.$": "$.derive.asset_id",
                "run_id.$": "$.derive.run_id",
                "manifest_key.$": "$.derive.manifest_key",
            },
            "MessageGroupId": "catalog",  # queues.GROUP: one group
            "MessageDeduplicationId.$": "States.Format('{}:p', $.session_id)",
        }
        assert send["End"] is True

    def test_a_task_error_fails_the_item_not_the_map(
        self, stacks: dict[str, Template]
    ) -> None:
        _, definition = _machine(stacks["ingest"])
        states = definition["States"]["PerAsset"]["ItemProcessor"]["States"]
        catch_all = [
            {
                "ErrorEquals": ["States.ALL"],
                "ResultPath": "$.error",
                "Next": "RecordFailed",
            }
        ]
        assert states["Derive"]["Catch"] == catch_all
        assert states["SendPointer"]["Catch"] == catch_all
        throttle = states["Derive"]["Retry"][-1]
        assert throttle["ErrorEquals"] == ["Lambda.TooManyRequestsException"]
        record = states["RecordFailed"]
        assert record["Resource"] == "arn:@AWS::Partition:states:::dynamodb:updateItem"
        params = record["Parameters"]
        assert params["Key"] == {"session_id": {"S.$": "$.session_id"}}
        assert params["ConditionExpression"] == (
            "attribute_exists(#sid) AND #state = :queued"
        )
        assert params["ExpressionAttributeValues"][":failed"] == {"S": "FAILED"}
        assert params["ExpressionAttributeValues"][":error"] == {
            "M": {
                "class": {"S.$": "$.error.Error"},
                "message": {"S.$": "$.error.Cause"},
                "tables_replaced": {"L": []},
            }
        }
        assert record["Catch"] == [
            {
                "ErrorEquals": ["States.ALL"],
                "ResultPath": "$.lost",
                "Next": "RecordFailedLost",
            }
        ]
        for name in ("DeriveRecordedFailure", "RecordFailedLost"):
            assert states[name] == {"Type": "Pass", "End": True}

    def test_role_invokes_one_function_and_writes_one_queue_and_one_table(
        self, stacks: dict[str, Template]
    ) -> None:
        t = stacks["ingest"]
        statements = policy_statements(t, "ReextractStateMachineRole")
        derive_arn = {"Fn::GetAtt": [_lid(t, DERIVE_FN), "Arn"]}
        assert statements[:3] == [
            {
                "Action": "lambda:InvokeFunction",
                "Effect": "Allow",
                "Resource": [
                    derive_arn,
                    {"Fn::Join": ["", [derive_arn, ":*"]]},
                ],
            },
            {
                "Action": "sqs:SendMessage",
                "Effect": "Allow",
                "Resource": {
                    "Fn::GetAtt": [_logical_id(t, "asteroidea-extract.fifo"), "Arn"]
                },
            },
            {
                "Action": "dynamodb:UpdateItem",
                "Effect": "Allow",
                "Resource": {
                    "Fn::Join": [
                        "",
                        [
                            "arn:",
                            {"Ref": "AWS::Partition"},
                            ":dynamodb:us-east-1:000000000000:table/",
                            {
                                "Fn::ImportValue": (
                                    "DataStack:ExportsOutputRefSessionsTable"
                                    "7C3020246E1CCFDD"
                                )
                            },
                        ],
                    ]
                },
            },
        ]
        # The rest is log delivery (SF1) and X-Ray (SF2), which take only "*".
        rest = statements[3:]
        assert {a for s in rest for a in actions(s)} <= {
            "logs:CreateLogDelivery",
            "logs:GetLogDelivery",
            "logs:UpdateLogDelivery",
            "logs:DeleteLogDelivery",
            "logs:ListLogDeliveries",
            "logs:PutResourcePolicy",
            "logs:DescribeResourcePolicies",
            "logs:DescribeLogGroups",
            "xray:PutTraceSegments",
            "xray:PutTelemetryRecords",
            "xray:GetSamplingRules",
            "xray:GetSamplingTargets",
        }
        assert len(rest) == 2

    def test_the_machine_arn_is_a_services_output(self) -> None:
        app = App(context={"sandbox-test": "true"})
        data = DataStack(app, "DataStack", env=ENV)
        services = ServicesStack(app, "ServicesStack", env=ENV, data=data)
        outputs = Template.from_stack(services).to_json()["Outputs"]
        assert "ReextractStateMachineArn" in outputs


def _lid(template: Template, prefix: str) -> str:
    (found,) = [
        lid
        for lid in template.find_resources("AWS::Lambda::Function")
        if lid.startswith(prefix)
    ]
    return found


def test_the_star_acknowledgement_covers_the_state_machine_role_only() -> None:
    """A '*' resource on any other ServicesStack role still fails cdk-nag."""
    app = App(context={"sandbox-test": "true"})
    data = DataStack(app, "DataStack", env=ENV)
    services = ServicesStack(app, "ServicesStack", env=ENV, data=data)
    services.ingest.derive_function.add_to_role_policy(
        iam.PolicyStatement(actions=["s3:GetObject"], resources=["*"])
    )
    apply_nag(app)
    with pytest.raises(Exception, match=r"DeriveFunction.*\n.*IAM5\[Resource::\*\]"):
        app.synth()
