"""Architecture diagram generator: the SVGs in `docs/src/architecture/`.

The picture is simplified, not resource-exact. The model has one node per resource:
one per `functions.FUNCTIONS` row, the route table collapsed per function, the queue
each function consumes, the Iceberg table count from `schema`. The data-flow edges are
curated: each one names the code that backs it, an IAM statement in `functions.py` or
`identity.py`, a state machine task, an event source, a route, or a client call.
Dead-letter queues fold into a note on their queue.

The overview is a backbone over that model. Resources group by kind into one box each
(S3 buckets, DynamoDB, the Iceberg catalog, identity, the public archives, the API
functions), with a bullet per member; the ingest pipeline, CloudFront, the HTTP API,
logging and the callers stay single boxes. It draws the writes, sends, invokes, routes
and the queue and entry reads; every other read is a text line in its caller's box.
Edges between the same two boxes with the same verb collapse into one. The layout is
fixed: columns left to right (callers, edge and identity, API functions, DataStack
storage, ingest pipeline, admin and archives), rows in declared order. Edges are
orthogonal and run through the gaps between columns and between rows. The output is
plain, deterministic SVG with light and dark styles. This module imports no `aws_cdk`
itself; `functions` and `ingest` do, without an App. Run it with `make architecture`.
"""

import sys
import types
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final
from xml.sax.saxutils import escape

from asteroidea_infra import athena, functions, ingest, reextract_flow, schema
from asteroidea_infra.web_hosting import HANDOUT_PREFIX, SITE_PREFIX
from asteroidea_ingest import routes

if TYPE_CHECKING:
    from collections.abc import Iterable

REPO_ROOT: Final = Path(__file__).resolve().parents[3]
OUT_DIR: Final = REPO_ROOT / "docs" / "src" / "architecture"
OVERVIEW: Final = OUT_DIR / "overview.svg"
STALE: Final = "docs/src/architecture is stale: run make architecture"

# Node kinds.
ACTOR: Final = "actor"
EDGE_SERVICE: Final = "edge-service"
MONITORING: Final = "monitoring"
FUNCTION_ZIP: Final = "function-zip"
FUNCTION_IMAGE: Final = "function-image"
QUEUE: Final = "queue"
STATE_MACHINE: Final = "state-machine"
BUCKET: Final = "bucket"
TABLE: Final = "table"
CATALOG: Final = "catalog"
KIND_TAGS: Final = {
    ACTOR: "caller",
    EDGE_SERVICE: "edge and identity",
    MONITORING: "CloudWatch, X-Ray",
    FUNCTION_ZIP: "Lambda, zip",
    FUNCTION_IMAGE: "Lambda, image",
    QUEUE: "SQS",
    STATE_MACHINE: "Step Functions",
    BUCKET: "S3 bucket",
    TABLE: "DynamoDB",
    CATALOG: "Glue and Athena",
}
STORAGE_KINDS: Final = frozenset({BUCKET, TABLE, CATALOG})

# Stacks (the tinted strip on each node).
DATA: Final = "data"
API: Final = "api"
INGEST: Final = "ingest"
EXTERNAL: Final = "external"
STACK_TITLES: Final = {
    DATA: "DataStack (stateful)",
    API: "ServicesStack / Api",
    INGEST: "ServicesStack / Ingest",
    EXTERNAL: "outside the stacks",
}

# Edge verbs: a write (solid) or a read (dashed). Sends and invokes are writes.
READ: Final = "read"
WRITE: Final = "write"


@dataclass(frozen=True)
class Port:
    """A named part of a storage node, such as a bucket prefix."""

    key: str
    label: str


@dataclass(frozen=True)
class Node:
    """One resource or caller of the model."""

    key: str
    label: str
    kind: str
    stack: str
    detail: tuple[str, ...] = ()
    ports: tuple[Port, ...] = ()
    # Replaces the kind tag line, for the operator and the archives.
    tag: str = ""


@dataclass(frozen=True)
class Edge:
    """A data-flow edge from the caller that holds the grant to the resource."""

    src: str
    dst: str
    verb: str
    # Where the edge comes from in the code: an IAM statement, a route, a task.
    basis: str
    port: str = ""
    label: str = ""


@dataclass(frozen=True)
class Step:
    """One numbered step of a use case: a model edge, or free text with no AWS edge.

    A step names its edge by caller, resource and verb; free text leaves them "".
    """

    text: str
    src: str = ""
    dst: str = ""
    verb: str = ""


@dataclass(frozen=True)
class Flow:
    """A use case: an ordered list of steps over the curated edges."""

    name: str
    title: str
    steps: tuple[Step, ...]
    # The steps (numbered from 1) that start where no earlier step reached, and why.
    jumps: tuple[tuple[int, str], ...] = ()


# The three queue callables in `FunctionSpec.queue` read one attribute of the Ingest
# stack each; this maps that attribute to the queue's node.
QUEUE_ATTRIBUTES: Final = {
    "commit_queue": "CommitQueue",
    "extract_queue": "ExtractQueue",
    "lfp_build_queue": "LfpBuildQueue",
}
QUEUE_NAMES: Final = {
    "CommitQueue": (ingest.COMMIT_QUEUE_NAME, "FIFO, +DLQ"),
    "ExtractQueue": (ingest.EXTRACT_QUEUE_NAME, "FIFO, +DLQ"),
    "LfpBuildQueue": (ingest.LFP_BUILD_QUEUE_NAME, "standard, +DLQ"),
}


def queue_of(spec: functions.FunctionSpec[Any]) -> str:
    """Return the queue node `spec` consumes, or "" when it has no event source.

    The spec's queue callable runs against a stand-in stack whose queue attributes are
    the node keys, so an unmapped queue raises AttributeError.
    """
    if spec.queue is None:
        return ""
    return str(spec.queue(types.SimpleNamespace(**QUEUE_ATTRIBUTES)))


def route_lines(function: str) -> tuple[str, ...]:
    """Return the route summary of `function`: one line per first path segment."""
    rows = [r for r in routes.ROUTES if r.function == function]
    segments = Counter(r.path.split("/")[1] for r in rows)
    lines = [
        f"/{seg}/* ({n})"
        if n > 1
        else next(r.path for r in rows if r.path.split("/")[1] == seg)
        for seg, n in segments.items()
    ]
    groups = sorted({r.group for r in rows} - {routes.RESEARCHERS})
    return (*lines, *(f"group: {g}" for g in groups))


def _function_nodes() -> list[Node]:
    api_ids = {spec.construct_id for spec in functions.API_FUNCTIONS}
    nodes: list[Node] = []
    for spec in functions.FUNCTIONS:
        is_api = spec.construct_id in api_ids
        detail = route_lines(spec.construct_id) if is_api else ()
        if spec.construct_id == "LfpBuildFunction":
            detail = ("re-queues one message", "per session")
        nodes.append(
            Node(
                key=spec.construct_id,
                label=spec.construct_id.removesuffix("Function"),
                kind=FUNCTION_ZIP if spec.image is None else FUNCTION_IMAGE,
                stack=API if is_api else INGEST,
                detail=detail,
            )
        )
    return nodes


def _bucket(key: str, label: str, ports: Iterable[tuple[str, str]]) -> Node:
    return Node(key, label, BUCKET, DATA, ports=tuple(Port(k, t) for k, t in ports))


def _outside(key: str, label: str, detail: str, tag: str) -> Node:
    return Node(key, label, ACTOR, EXTERNAL, (detail,), tag=tag)


LFP: Final = f"{functions.DERIVED_LFP_PREFIX}/ {functions.DERIVED_SUMMARY_PREFIX}/"
ROWS: Final = f"{functions.DERIVED_ROWS_PREFIX}/"


def _curated_nodes() -> list[Node]:
    return [
        _outside("Uploader", "Researcher", "uploads files", "uploader"),
        _outside("Curator", "Curator CLI", "admin credentials", "operator"),
        _outside("DandiApi", "DANDI API", "HTTPS, anonymous", "archive"),
        _outside("OpenNeuroApi", "OpenNeuro API", "GraphQL, anonymous", "archive"),
        _outside("OpenNeuroBucket", "openneuro.org", "public S3 bucket", "archive"),
        _outside("Browser", "Browser", "web app", "consumer"),
        _outside("Researcher", "Researcher", "SDK, notebook", "consumer"),
        Node("CloudFront", "CloudFront", EDGE_SERVICE, DATA, ("the web app",)),
        Node("UserPool", "User pool", EDGE_SERVICE, DATA, ("Cognito",)),
        Node(
            "HttpApi",
            "HTTP API",
            EDGE_SERVICE,
            API,
            (f"{len(routes.ROUTES)} routes", "JWT authorizer"),
        ),
        Node("IdentityPool", "Identity pool", EDGE_SERVICE, DATA, ("Cognito",)),
        Node(
            "ResearcherRole", "ResearcherRole", EDGE_SERVICE, DATA, ("IAM, read-only",)
        ),
        # Only what the stacks hold: Lambda logs through the basic execution role,
        # the re-extract log group and tracing; no alarm, dashboard or CDN log.
        Node(
            "Logging",
            "Logs, monitoring",
            MONITORING,
            INGEST,
            (
                "Lambdas: CloudWatch",
                "Re-extract: log group",
                "logs ALL, X-Ray on",
                "no alarms, dashboards",
                "no CloudFront logs",
            ),
        ),
        _bucket(
            "SiteBucket", "Site", ((SITE_PREFIX, "site/"), (HANDOUT_PREFIX, "handout/"))
        ),
        _bucket("ArtifactsBucket", "Artifacts", (("artifacts", "artifacts/ index/"),)),
        Node("LabelsTable", "Labels", TABLE, DATA),
        Node("ImportRunsTable", "Import runs", TABLE, DATA),
        Node("SessionsTable", "Sessions", TABLE, DATA),
        Node("OwnersTable", "Dataset owners", TABLE, DATA),
        _bucket("RawBucket", "Raw", (("landing", "landing/"), ("raw", "raw/"))),
        _bucket(
            "DerivedBucket",
            "Derived",
            (
                (functions.DERIVED_LFP_PREFIX, LFP),
                (functions.DERIVED_ROWS_PREFIX, ROWS),
            ),
        ),
        _bucket(
            "WarehouseBucket",
            "Warehouse",
            ((schema.ICEBERG_PREFIX, f"{schema.ICEBERG_PREFIX}/"),),
        ),
        _bucket("AthenaResultsBucket", "Athena results", (("athena", "athena/"),)),
        Node(
            "GlueDatabase",
            "Glue database",
            CATALOG,
            DATA,
            (schema.DATABASE, f"{len(schema.TABLES)} Iceberg tables"),
        ),
        Node("AthenaWorkgroup", "Athena workgroup", CATALOG, DATA, (athena.WORKGROUP,)),
        *(
            Node(key, key.removesuffix("Queue") + " queue", QUEUE, INGEST, (n, d))
            for key, (n, d) in QUEUE_NAMES.items()
        ),
        Node(
            "ReextractStateMachine",
            "Re-extract",
            STATE_MACHINE,
            INGEST,
            (reextract_flow.STATE_MACHINE_NAME,),
        ),
    ]


def nodes() -> dict[str, Node]:
    """Return every model node by key: the derived nodes, then the curated ones."""
    return {n.key: n for n in (*_function_nodes(), *_curated_nodes())}


F: Final = "functions.py :: "
W, R = WRITE, READ
# The curated data-flow edges. An IAM edge reads `<helper> :: <actions>`.
EDGES: Final[tuple[Edge, ...]] = (
    # Callers and the edge.
    Edge("Browser", "CloudFront", R, "client: HTTPS"),
    Edge("CloudFront", "SiteBucket", R, "web_hosting.py :: WebHosting", SITE_PREFIX),
    Edge("Browser", "UserPool", W, "client: managed login"),
    Edge("Browser", "HttpApi", W, "client: fetch with the id token"),
    Edge("Researcher", "UserPool", W, "client: PKCE"),
    Edge("Researcher", "IdentityPool", R, "client: GetCredentialsForIdentity"),
    Edge("Researcher", "HttpApi", W, "client: SDK calls"),
    Edge(
        "IdentityPool",
        "ResearcherRole",
        R,
        "identity.py :: Identity RoleAttachment",
        label="assume",
    ),
    Edge("ResearcherRole", "RawBucket", R, "identity.py :: s3:GetObject", "raw"),
    Edge("ResearcherRole", "AthenaWorkgroup", R, "identity.py :: query read"),
    Edge(
        "ResearcherRole",
        "WarehouseBucket",
        R,
        "identity.py :: query read s3:GetObject",
        schema.ICEBERG_PREFIX,
    ),
    # The routes: one edge per function.
    *(
        Edge("HttpApi", spec.construct_id, W, "routes.py :: ROUTES")
        for spec in functions.API_FUNCTIONS
    ),
    # SessionApiRole.
    Edge("SessionFunction", "SessionsTable", W, F + "session_statements :: PutItem"),
    Edge("SessionFunction", "OwnersTable", W, F + "session_statements :: PutItem"),
    Edge(
        "SessionFunction",
        "RawBucket",
        W,
        F + "session_statements :: PutObject",
        "landing",
    ),
    Edge(
        "SessionFunction", "RawBucket", R, F + "session_statements :: GetObject", "raw"
    ),
    Edge("SessionFunction", "CommitQueue", W, F + "session_statements :: SendMessage"),
    # PlatformRole.
    Edge(
        "PlatformFunction",
        "SiteBucket",
        R,
        F + "platform_statements :: GetObject",
        HANDOUT_PREFIX,
    ),
    Edge(
        "PlatformFunction",
        "ArtifactsBucket",
        W,
        F + "platform_statements :: PutObject",
        "artifacts",
    ),
    Edge(
        "PlatformFunction",
        "ArtifactsBucket",
        R,
        F + "platform_statements :: GetObject",
        "artifacts",
    ),
    # CatalogRole.
    Edge(
        "CatalogFunction", "AthenaWorkgroup", R, F + "catalog_statements :: query read"
    ),
    Edge(
        "CatalogFunction", "RawBucket", R, F + "catalog_statements :: GetObject", "raw"
    ),
    # ComputeRole.
    Edge(
        "ComputeFunction", "AthenaWorkgroup", R, F + "compute_statements :: query read"
    ),
    Edge(
        "ComputeFunction", "RawBucket", R, F + "compute_statements :: GetObject", "raw"
    ),
    Edge(
        "ComputeFunction",
        "DerivedBucket",
        R,
        F + "compute_statements :: GetObject",
        functions.DERIVED_LFP_PREFIX,
    ),
    Edge("ComputeFunction", "LabelsTable", R, F + "compute_statements :: Query"),
    # LabelsRole.
    Edge("LabelsFunction", "AthenaWorkgroup", R, F + "labels_statements :: query read"),
    Edge("LabelsFunction", "LabelsTable", W, F + "labels_statements :: PutItem"),
    # ImportsRole.
    Edge(
        "ImportsFunction",
        "ImportPlanFunction",
        W,
        F + "imports_statements :: InvokeFunction",
    ),
    Edge("ImportsFunction", "ImportRunsTable", W, F + "imports_statements :: PutItem"),
    Edge(
        "ImportsFunction", "SessionsTable", R, F + "imports_statements :: BatchGetItem"
    ),
    # ImportPlanRole.
    Edge(
        "ImportPlanFunction",
        "ImportFunction",
        W,
        F + "import_plan_statements :: InvokeFunction",
    ),
    Edge(
        "ImportPlanFunction",
        "ImportRunsTable",
        W,
        F + "import_plan_statements :: UpdateItem",
    ),
    Edge(
        "ImportPlanFunction",
        "WarehouseBucket",
        R,
        F + "import_plan_statements :: GetObject",
        schema.ICEBERG_PREFIX,
    ),
    Edge("ImportPlanFunction", "DandiApi", R, F + "import_plan_statements (docstring)"),
    Edge(
        "ImportPlanFunction",
        "OpenNeuroApi",
        R,
        F + "import_plan_statements (docstring)",
    ),
    # ImportRole.
    Edge("ImportFunction", "OpenNeuroBucket", R, F + "import_statements :: GetObject"),
    Edge("ImportFunction", "DandiApi", R, F + "OPENNEURO_OBJECTS (comment)"),
    Edge(
        "ImportFunction",
        "RawBucket",
        W,
        F + "import_statements :: PutObject",
        "landing",
    ),
    Edge("ImportFunction", "SessionsTable", W, F + "import_statements :: PutItem"),
    Edge("ImportFunction", "OwnersTable", W, F + "import_statements :: PutItem"),
    Edge("ImportFunction", "CommitQueue", W, F + "import_statements :: SendMessage"),
    # CommitRole.
    Edge("CommitFunction", "CommitQueue", R, "functions.py :: _commit_queue"),
    Edge("CommitFunction", "ExtractQueue", W, F + "commit_statements :: SendMessage"),
    Edge("CommitFunction", "SessionsTable", W, F + "commit_statements :: UpdateItem"),
    Edge(
        "CommitFunction",
        "RawBucket",
        R,
        F + "commit_statements :: GetObject",
        "landing",
    ),
    Edge("CommitFunction", "RawBucket", W, F + "commit_statements :: PutObject", "raw"),
    Edge(
        "CommitFunction",
        "WarehouseBucket",
        W,
        F + "commit_statements :: PutObject, glue:UpdateTable",
        schema.ICEBERG_PREFIX,
    ),
    # ExtractRole.
    Edge("ExtractFunction", "ExtractQueue", R, "functions.py :: _extract_queue"),
    Edge(
        "ExtractFunction", "RawBucket", R, F + "extract_statements :: GetObject", "raw"
    ),
    Edge(
        "ExtractFunction",
        "WarehouseBucket",
        W,
        F + "extract_statements :: PutObject, glue:UpdateTable",
        schema.ICEBERG_PREFIX,
    ),
    Edge("ExtractFunction", "SessionsTable", W, F + "extract_statements :: UpdateItem"),
    Edge(
        "ExtractFunction", "LfpBuildQueue", W, F + "extract_statements :: SendMessage"
    ),
    Edge(
        "ExtractFunction",
        "DerivedBucket",
        R,
        F + "extract_statements :: GetObject",
        functions.DERIVED_ROWS_PREFIX,
    ),
    # LfpBuildRole.
    Edge("LfpBuildFunction", "LfpBuildQueue", R, "functions.py :: _lfp_build_queue"),
    Edge(
        "LfpBuildFunction",
        "RawBucket",
        R,
        F + "lfp_build_statements :: GetObject",
        "raw",
    ),
    Edge(
        "LfpBuildFunction",
        "DerivedBucket",
        W,
        F + "lfp_build_statements :: PutObject",
        functions.DERIVED_LFP_PREFIX,
    ),
    Edge(
        "LfpBuildFunction",
        "AthenaWorkgroup",
        R,
        F + "lfp_build_statements :: query read",
    ),
    # DeriveRole.
    Edge("DeriveFunction", "RawBucket", R, F + "derive_statements :: GetObject", "raw"),
    Edge(
        "DeriveFunction",
        "DerivedBucket",
        W,
        F + "derive_statements :: PutObject",
        functions.DERIVED_ROWS_PREFIX,
    ),
    Edge("DeriveFunction", "SessionsTable", W, F + "derive_statements :: UpdateItem"),
    # The re-extract state machine's tasks.
    Edge(
        "ReextractStateMachine",
        "DeriveFunction",
        W,
        "reextract_flow.py :: Derive (LambdaInvoke)",
    ),
    Edge(
        "ReextractStateMachine",
        "ExtractQueue",
        W,
        "reextract_flow.py :: SendPointer (SqsSendMessage)",
    ),
    Edge(
        "ReextractStateMachine",
        "SessionsTable",
        W,
        "reextract_flow.py :: RecordFailed (DynamoUpdateItem)",
    ),
    # The query read every Athena caller holds: Glue, iceberg/ and athena/.
    Edge("AthenaWorkgroup", "GlueDatabase", R, "identity.py :: query_read_statements"),
    Edge(
        "AthenaWorkgroup",
        "WarehouseBucket",
        R,
        "identity.py :: query_read_statements",
        schema.ICEBERG_PREFIX,
    ),
    Edge(
        "AthenaWorkgroup",
        "AthenaResultsBucket",
        W,
        "identity.py :: query_read_statements",
        "athena",
    ),
    # The admin commands, with the administrator's credentials.
    Edge("Curator", "ImportFunction", W, "curate.py :: invoke", label="make import"),
    Edge(
        "Curator",
        "ReextractStateMachine",
        W,
        "reextract.py :: start_execution",
        label="make reextract",
    ),
    # The researcher as an uploader: the same client, drawn upstream.
    Edge("Uploader", "UserPool", W, "client: PKCE"),
    Edge("Uploader", "HttpApi", W, "client: POST /sessions, finalize"),
)


# ---------------------------------------------------------------- the overview's boxes

# Columns, left to right.
CALLERS, ENTRY, API_COL, STORAGE, PIPELINE, ADMIN = range(6)
COLUMN_TITLES: Final = (
    "Callers",
    "Edge and identity",
    STACK_TITLES[API],
    STACK_TITLES[DATA],
    STACK_TITLES[INGEST],
    "Admin and archives",
)
# The columns that get a dashed stack box.
STACK_COLUMNS: Final = (API_COL, STORAGE, PIPELINE)

API_GROUP: Final = "ApiFunctions"


@dataclass(frozen=True)
class GroupSpec:
    """One grouped box of the overview: its members are bullets."""

    key: str
    label: str
    kind: str
    column: int
    stack: str
    tag: str


GROUPS: Final = (
    GroupSpec("Identity", "Identity", EDGE_SERVICE, ENTRY, DATA, "Cognito and IAM"),
    GroupSpec(API_GROUP, "API functions", FUNCTION_ZIP, API_COL, API, "Lambda"),
    GroupSpec("Dynamo", "DynamoDB", TABLE, STORAGE, DATA, "tables"),
    GroupSpec("Buckets", "S3 buckets", BUCKET, STORAGE, DATA, "buckets and prefixes"),
    GroupSpec("Catalog", "Iceberg catalog", CATALOG, STORAGE, DATA, "Glue and Athena"),
    GroupSpec("Archives", "Public archives", ACTOR, ADMIN, EXTERNAL, "anonymous reads"),
)


def group_members() -> dict[str, tuple[str, ...]]:
    """Return each group's member nodes, in bullet order.

    The API group's bullets come from `functions.API_FUNCTIONS`, so a new API function
    shows up without an edit here.
    """
    return {
        "Identity": ("UserPool", "IdentityPool", "ResearcherRole"),
        API_GROUP: tuple(spec.construct_id for spec in functions.API_FUNCTIONS),
        "Dynamo": ("SessionsTable", "OwnersTable", "ImportRunsTable", "LabelsTable"),
        "Buckets": (
            "SiteBucket",
            "ArtifactsBucket",
            "RawBucket",
            "WarehouseBucket",
            "DerivedBucket",
            "AthenaResultsBucket",
        ),
        "Catalog": ("AthenaWorkgroup", "GlueDatabase"),
        "Archives": ("DandiApi", "OpenNeuroApi", "OpenNeuroBucket"),
    }


# The declared row order of each column: the layout reads it top to bottom. A key
# that is not a group is a single box.
ORDER: Final = (
    ("Browser", "Researcher", "Uploader"),
    ("CloudFront", "Identity", "HttpApi", "Logging"),
    (API_GROUP,),
    ("Dynamo", "Buckets", "Catalog"),
    (
        "ImportPlanFunction",
        "ImportFunction",
        "CommitQueue",
        "CommitFunction",
        "ExtractQueue",
        "ExtractFunction",
        "LfpBuildQueue",
        "LfpBuildFunction",
        "ReextractStateMachine",
        "DeriveFunction",
    ),
    ("Archives", "Curator"),
)
# A box whose middle lines up with another box's, when its column allows.
ANCHORS: Final = {
    "HttpApi": API_GROUP,
    "Browser": "CloudFront",
    "Researcher": "Identity",
    "Uploader": "HttpApi",
}


def where() -> dict[str, tuple[str, str]]:
    """Return the overview box of each model node, and its bullet ("" for a box)."""
    groups = {g.key for g in GROUPS}
    out = {k: (k, "") for column in ORDER for k in column if k not in groups}
    for group, members in group_members().items():
        out |= {m: (group, m) for m in members}
    return out


# The reads drawn although they are reads: each is the only link between its two boxes
# and is part of a main flow. Every other read is a line in its caller's box.
READ_EXCEPTIONS: Final = {
    ("CloudFront", "SiteBucket"): "the web app is served from site/",
    ("ImportPlanFunction", "DandiApi"): "the import plan lists the archive",
    ("ImportPlanFunction", "OpenNeuroApi"): "the import plan lists the archive",
    ("ImportFunction", "DandiApi"): "the import fetches the files",
    ("ImportFunction", "OpenNeuroBucket"): "the import fetches the files",
}

# Short names for the reads lines and edge labels.
SHORT: Final = {
    "AthenaWorkgroup": "Athena",
    "GlueDatabase": "Glue",
    "OwnersTable": "owners",
    "DandiApi": "DANDI",
    "OpenNeuroApi": "OpenNeuro",
    "OpenNeuroBucket": "S3 bucket",
}


def drawn(edge: Edge, all_nodes: dict[str, Node]) -> bool:
    """Return True when the overview draws `edge`; False puts it in a reads line.

    Drawn: every write (send, invoke, route), a caller's read of an entry, an assume,
    a consumer's read of its queue, and the named read exceptions; never an edge whose
    two ends share a box.
    """
    places = where()
    if places[edge.src][0] == places[edge.dst][0]:
        return False
    src, dst = all_nodes[edge.src].kind, all_nodes[edge.dst].kind
    return (
        edge.verb == WRITE
        or (src == ACTOR and dst == EDGE_SERVICE)
        or edge.label == "assume"
        or dst == QUEUE
        or (edge.src, edge.dst) in READ_EXCEPTIONS
    )


def token(edge: Edge, all_nodes: dict[str, Node]) -> str:
    """Return the short name of what `edge` reaches: its prefix, else its resource."""
    if edge.port:
        return next(p.label for p in all_nodes[edge.dst].ports if p.key == edge.port)
    return SHORT.get(edge.dst, all_nodes[edge.dst].label)


def note_prefix(edge: Edge) -> str:
    """Return the line prefix an undrawn edge is listed under: "reads", "assumes"."""
    return f"{edge.label or edge.verb}s"


def notes(all_nodes: dict[str, Node]) -> dict[str, dict[str, list[str]]]:
    """Return, per caller, the prefixes and short names of its undrawn edges."""
    out: dict[str, dict[str, list[str]]] = {}
    for edge in EDGES:
        if drawn(edge, all_nodes):
            continue
        names = out.setdefault(edge.src, {}).setdefault(note_prefix(edge), [])
        name = token(edge, all_nodes)
        if name not in names:
            names.append(name)
    return out


def wrap(prefix: str, names: list[str], room: float) -> list[str]:
    """Return `prefix: a, b, c` broken at commas into lines no wider than `room`."""
    lines: list[str] = []
    line = f"{prefix}:"
    for i, name in enumerate(names):
        piece = name + ("," if i < len(names) - 1 else "")
        candidate = f"{line} {piece}" if line else piece
        if text_width(candidate, SMALL) > room and line:
            lines.append(line)
            line = piece
        else:
            line = candidate
    return [*lines, line]


# A line of text in a box: its text and its class ("", "mono" or "muted").
type Line = tuple[str, str]


@dataclass(frozen=True)
class Member:
    """One bullet of a grouped box."""

    key: str
    title: str
    # Monospace, at the right end of the bullet's first line.
    tag: str
    lines: tuple[Line, ...]


@dataclass(frozen=True)
class Block:
    """One box of the overview: a single node, or a group with a bullet per member."""

    key: str
    label: str
    kind: str
    column: int
    stack: str
    tag: str
    lines: tuple[Line, ...] = ()
    members: tuple[Member, ...] = ()


WIDTH: Final = 1440
MARGIN: Final = 16
TOP: Final = 92
MIN_GAP: Final = 22
COLUMN_WIDTHS: Final = (104, 164, 190, 180, 170, 148)
# The gap after each column but the last.
GAP_WIDTHS: Final = (38, 60, 118, 118, 118)
FONT: Final = "system-ui, -apple-system, 'Segoe UI', Helvetica, Arial, sans-serif"
MONO: Final = "ui-monospace, Menlo, Consolas, monospace"
TITLE_SIZE: Final = 13
SMALL: Final = 11
LINE: Final = 14
PAD: Final = 8
INDENT: Final = 10
BULLET: Final = "\u2022"
# A reads list wraps onto at most this many lines.
MAX_NOTE_LINES: Final = 2
STRIP: Final = 5
PORT_ROW: Final = 20
PORT_SLOT: Final = 8
LANE: Final = 6
LANE_PAD: Final = 8
CLEAR: Final = 6
# Two stubs closer than NEAR px across a gap: the right-side one moves NUDGE px.
NEAR: Final = 2
NUDGE: Final = 4
# Average glyph advance as a share of the font size: a generous estimate, so a text
# that fits here fits in a real renderer.
SANS_ADVANCE: Final = 0.6
MONO_ADVANCE: Final = 0.62


def text_width(text: str, size: float, *, mono: bool = False) -> float:
    """Return a generous estimate of the rendered width of `text`."""
    return len(text) * size * (MONO_ADVANCE if mono else SANS_ADVANCE)


def _detail(node: Node) -> list[Line]:
    return [(d, "mono" if d.startswith("/") else "") for d in node.detail]


def _note_lines(
    key: str, found: dict[str, dict[str, list[str]]], room: float
) -> list[Line]:
    return [
        (line, "muted")
        for prefix, names in found.get(key, {}).items()
        for line in wrap(prefix, names, room)
    ]


def _member(node: Node, found: dict[str, dict[str, list[str]]], room: float) -> Member:
    lines = _detail(node)
    if node.ports:
        tag = " ".join(p.label for p in node.ports)
    elif node.kind in {FUNCTION_ZIP, FUNCTION_IMAGE}:
        tag = "zip" if node.kind == FUNCTION_ZIP else "image"
    elif node.kind == CATALOG:
        tag, lines = node.detail[0], lines[1:]
    else:
        tag = ""
    head = text_width(f"{BULLET} {node.label}", SMALL) + 8
    if tag and head + text_width(tag, SMALL, mono=True) > room + INDENT:
        lines, tag = [(tag, "mono"), *lines], ""
    return Member(
        node.key, node.label, tag, (*lines, *_note_lines(node.key, found, room))
    )


def blocks(all_nodes: dict[str, Node] | None = None) -> dict[str, Block]:
    """Return every overview box by key: the single boxes, then the groups."""
    all_nodes = all_nodes or nodes()
    found = notes(all_nodes)
    groups = {g.key for g in GROUPS}
    out: dict[str, Block] = {}
    for column, keys in enumerate(ORDER):
        room = COLUMN_WIDTHS[column] - 2 * PAD
        for key in keys:
            if key in groups:
                continue
            n = all_nodes[key]
            lines = (*_detail(n), *_note_lines(key, found, room))
            tag = n.tag or KIND_TAGS[n.kind]
            out[key] = Block(key, n.label, n.kind, column, n.stack, tag, lines)
    members = group_members()
    for g in GROUPS:
        room = COLUMN_WIDTHS[g.column] - 2 * PAD - INDENT
        bullets = tuple(_member(all_nodes[m], found, room) for m in members[g.key])
        out[g.key] = Block(
            g.key, g.label, g.kind, g.column, g.stack, g.tag, (), bullets
        )
    return out


# ---------------------------------------------------------------- drawn edges


@dataclass(frozen=True)
class Link:
    """One drawn edge: the model edges it carries between two overview boxes."""

    src: str
    dst: str
    verb: str
    edges: tuple[Edge, ...]
    # The bullet each end attaches to; "" for the box itself.
    src_member: str
    dst_member: str
    # The members it carries on an end that does not attach to one bullet.
    label: str


# Groups whose bullets each get their own edges: each API bullet is a route target.
SPLIT: Final = frozenset({API_GROUP})


def sides(src: str, dst: str, all_blocks: dict[str, Block]) -> tuple[str, str]:
    """Return the side of the caller's box and of the resource's box an edge uses.

    Different columns face each other; vertical neighbours in one column join bottom to
    top; any other pair in one column leaves on the side towards the middle.
    """
    a, b = all_blocks[src].column, all_blocks[dst].column
    if a < b:
        return "R", "L"
    if a > b:
        return "L", "R"
    keys = ORDER[a]
    i, j = keys.index(src), keys.index(dst)
    if abs(i - j) == 1:
        return ("B", "T") if i < j else ("T", "B")
    side = "L" if a == len(ORDER) - 1 else "R"
    return side, side


def _unique(items: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(items))


def links(
    all_nodes: dict[str, Node] | None = None,
    all_blocks: dict[str, Block] | None = None,
) -> tuple[Link, ...]:
    """Return the drawn edges: the drawn model edges, collapsed per pair of boxes."""
    all_nodes = all_nodes or nodes()
    all_blocks = all_blocks or blocks(all_nodes)
    places = where()
    grouped: dict[tuple[str, str, str, str, str], list[Edge]] = {}
    for edge in EDGES:
        if not drawn(edge, all_nodes):
            continue
        (sb, sm), (db, dm) = places[edge.src], places[edge.dst]
        key = (sb, sm if sb in SPLIT else "", db, dm if db in SPLIT else "", edge.verb)
        grouped.setdefault(key, []).append(edge)
    out: list[Link] = []
    for (sb, _, db, _, verb), carried in grouped.items():
        ends = sides(sb, db, all_blocks)
        names = _unique(e.label for e in carried if e.label)
        anchors: list[str] = []
        for end, side in enumerate(ends):
            members = _unique(places[e.dst if end else e.src][1] for e in carried)
            one = members[0] if len(members) == 1 and side in "LR" else ""
            anchors.append(one)
            if members[0] and not one:
                names += [
                    token(e, all_nodes) if end else SHORT.get(e.src, e.src)
                    for e in carried
                ]
        label = ", ".join(_unique(names))
        out.append(Link(sb, db, verb, tuple(carried), anchors[0], anchors[1], label))
    return tuple(out)


# ---------------------------------------------------------------- layout


@dataclass(frozen=True)
class Box:
    """A rectangle: left, top, width, height."""

    x: float
    y: float
    w: float
    h: float

    @property
    def right(self) -> float:
        """Return the right edge."""
        return self.x + self.w

    @property
    def bottom(self) -> float:
        """Return the bottom edge."""
        return self.y + self.h

    def overlaps(self, other: Box, margin: float = 0) -> bool:
        """Return True when the two rectangles, grown by `margin`, intersect."""
        return (
            self.x - margin < other.right
            and other.x - margin < self.right
            and self.y - margin < other.bottom
            and other.y - margin < self.bottom
        )


def column_x(column: int) -> float:
    """Return the left edge of `column`."""
    return MARGIN + sum(COLUMN_WIDTHS[:column]) + sum(GAP_WIDTHS[:column])


def gap_span(gap: int) -> tuple[float, float]:
    """Return the left and right edge of the gap after column `gap`."""
    left = column_x(gap) + COLUMN_WIDTHS[gap]
    return left, left + GAP_WIDTHS[gap]


def _end_counts(
    all_blocks: dict[str, Block], drawn_: Iterable[Link]
) -> Counter[tuple[str, str, str]]:
    """Return how many edge ends attach to each (box, bullet, side)."""
    counts: Counter[tuple[str, str, str]] = Counter()
    for link in drawn_:
        ends = sides(link.src, link.dst, all_blocks)
        for key, member, side in (
            (link.src, link.src_member, ends[0]),
            (link.dst, link.dst_member, ends[1]),
        ):
            if member:
                counts[key, member, side] += 1
    return counts


def _header(block: Block) -> float:
    return STRIP + PAD + LINE + LINE + len(block.lines) * LINE + PAD


def _member_height(
    block: str, member: Member, counts: Counter[tuple[str, str, str]]
) -> float:
    most = max(counts[block, member.key, side] for side in "LR")
    text = 6 + (1 + len(member.lines)) * LINE
    return max(PORT_ROW, text, (most + 1) * PORT_SLOT)


def block_height(block: Block, counts: Counter[tuple[str, str, str]]) -> float:
    """Return the box height: strip, title, kind tag, lines, then the bullet rows."""
    rows = sum(_member_height(block.key, m, counts) for m in block.members)
    return _header(block) + rows


def layout(
    all_blocks: dict[str, Block], drawn_: Iterable[Link] | None = None
) -> dict[str, Box]:
    """Return each box: columns justified to the tallest, anchors honoured."""
    counts = _end_counts(all_blocks, links() if drawn_ is None else drawn_)
    heights = {k: block_height(b, counts) for k, b in all_blocks.items()}
    tallest = max(
        sum(heights[k] for k in keys) + MIN_GAP * (len(keys) - 1) for keys in ORDER
    )
    boxes: dict[str, Box] = {}
    # Anchored columns go last, right to left, so their anchors are placed.
    for column in sorted(
        range(len(ORDER)),
        key=lambda c: (any(k in ANCHORS for k in ORDER[c]), -c),
    ):
        keys = ORDER[column]
        total = sum(heights[k] for k in keys)
        gap = (tallest - total) / (len(keys) - 1) if len(keys) > 1 else 0
        if any(k in ANCHORS for k in keys) or column == ADMIN:
            gap = MIN_GAP
        y = TOP
        for key in keys:
            anchor = ANCHORS.get(key, "")
            if anchor in boxes:
                target = boxes[anchor].y + boxes[anchor].h / 2 - heights[key] / 2
                y = max(y, target)
            boxes[key] = Box(column_x(column), y, COLUMN_WIDTHS[column], heights[key])
            y += heights[key] + gap
    return boxes


def spans(
    all_blocks: dict[str, Block], boxes: dict[str, Box], drawn_: Iterable[Link]
) -> dict[tuple[str, str], tuple[float, float]]:
    """Return the vertical span of each bullet row and of each box's header ("")."""
    counts = _end_counts(all_blocks, drawn_)
    out: dict[tuple[str, str], tuple[float, float]] = {}
    for key, block in all_blocks.items():
        box = boxes[key]
        y = box.y + _header(block)
        out[key, ""] = (box.y + STRIP + 2, y if block.members else box.bottom - 2)
        for member in block.members:
            h = _member_height(key, member, counts)
            out[key, member.key] = (y, y + h)
            y += h
    return out


# ---------------------------------------------------------------- routing


@dataclass(frozen=True)
class Route:
    """A drawn edge's polyline, from the caller's end to the resource's end."""

    link: Link
    points: tuple[tuple[float, float], ...]


@dataclass
class _Lanes:
    """Vertical lanes in the gaps and horizontal slots in the channels."""

    vertical: dict[tuple[int, int], list[tuple[float, float]]]
    horizontal: dict[float, list[tuple[float, float]]]

    def lane(self, gap: int, y0: float, y1: float, *, prefer_right: bool) -> float:
        """Return a free lane x in `gap` for the span y0..y1, and claim it."""
        left, right = gap_span(gap)
        count = int((right - left - 2 * LANE_PAD) // LANE) + 1
        order = range(count - 1, -1, -1) if prefer_right else range(count)
        lo, hi = min(y0, y1) - CLEAR, max(y0, y1) + CLEAR
        for i in order:
            taken = self.vertical.setdefault((gap, i), [])
            if all(hi <= a or b <= lo for a, b in taken):
                taken.append((lo, hi))
                return left + LANE_PAD + i * LANE
        msg = f"gap after column {gap} has no free lane for {y0:.0f}..{y1:.0f}"
        raise ValueError(msg)

    def slot(
        self, free: list[tuple[float, float]], want: float, x0: float, x1: float
    ) -> float:
        """Return a free channel y nearest `want` for the span x0..x1, and claim it."""
        candidates: list[float] = []
        for top, bottom in free:
            half = int((bottom - top) / 2 // LANE)
            mid = (top + bottom) / 2
            candidates += [mid + k * LANE for k in range(-half, half + 1)]
        for y in sorted(candidates, key=lambda c: (abs(c - want), c)):
            if all(
                abs(k - y) >= 1 or x1 + CLEAR <= a or b <= x0 - CLEAR
                for k, taken in self.horizontal.items()
                for a, b in taken
            ):
                self.horizontal.setdefault(round(y, 1), []).append((x0, x1))
                return y
        msg = f"no free channel between x {x0:.0f} and {x1:.0f}"
        raise ValueError(msg)


def _free_rows(
    boxes: dict[str, Box], columns: range, *, below: bool = False
) -> list[tuple[float, float]]:
    """Return the y spans free of boxes in every column of `columns`.

    With `below`, only the span under the lowest of them, down to the lowest box of
    the whole diagram: the channels' fallback when the spans between boxes are full.
    """
    blocked = sorted(
        (b.y - CLEAR, b.bottom + CLEAR)
        for k, b in boxes.items()
        if any(k in ORDER[c] for c in columns)
    )
    free: list[tuple[float, float]] = []
    y = TOP - MIN_GAP
    for top, bottom in blocked:
        if top > y:
            free.append((y, top))
        y = max(y, bottom)
    if below:
        floor = max(b.bottom for b in boxes.values())
        return [(y, floor)] if floor > y else []
    return free


@dataclass(frozen=True)
class _Scene:
    """The boxes, their bullet spans and the drawn edges, for the router."""

    blocks: dict[str, Block]
    boxes: dict[str, Box]
    spans: dict[tuple[str, str], tuple[float, float]]
    links: tuple[Link, ...]

    def ends(self, i: int) -> tuple[str, str]:
        """Return the sides edge `i` uses."""
        link = self.links[i]
        return sides(link.src, link.dst, self.blocks)

    def middle(self, key: str, member: str) -> float:
        """Return the y middle of a bullet row, or of the box for ""."""
        if member:
            top, bottom = self.spans[key, member]
            return (top + bottom) / 2
        return self.boxes[key].y + self.boxes[key].h / 2


def _anchor_points(scene: _Scene) -> dict[tuple[int, int], tuple[float, float]]:
    """Return each edge end's attachment point, spread along its side and bullet."""
    groups: dict[tuple[str, str, str], list[tuple[int, int, float]]] = {}
    for i, link in enumerate(scene.links):
        ends = scene.ends(i)
        pairs = (
            (link.src, link.src_member, link.dst, link.dst_member),
            (link.dst, link.dst_member, link.src, link.src_member),
        )
        for end, (key, member, other, other_member) in enumerate(pairs):
            if ends[end] in "LR":
                rank = scene.middle(other, other_member)
            else:
                rank = scene.boxes[other].x
            groups.setdefault((key, ends[end], member), []).append((i, end, rank))
    points: dict[tuple[int, int], tuple[float, float]] = {}
    for (key, side, member), members in sorted(groups.items()):
        box = scene.boxes[key]
        members.sort(key=lambda m: (m[2], m[0]))
        n = len(members)
        for j, (i, end, _) in enumerate(members):
            if side in "TB":
                x = box.x + box.w / 2 + (j - (n - 1) / 2) * 16
                points[i, end] = (x, box.y if side == "T" else box.bottom)
            else:
                top, bottom = scene.spans[key, member]
                y = top + (j + 1) * (bottom - top) / (n + 1)
                # Right-side ends sit 3 px lower, so two boxes facing each other
                # across a gap rarely put two stubs on one line.
                shift = 0 if side == "L" else 3
                points[i, end] = (box.x if side == "L" else box.right, y + shift)
    return _apart(scene, points)


def _apart(
    scene: _Scene, points: dict[tuple[int, int], tuple[float, float]]
) -> dict[tuple[int, int], tuple[float, float]]:
    """Move a right-side end down when a left-side end across its gap shares its y."""

    def column(i: int, end: int) -> int:
        link = scene.links[i]
        return scene.blocks[link.dst if end else link.src].column

    left = [
        (column(i, end), y)
        for (i, end), (_, y) in points.items()
        if scene.ends(i)[end] == "L"
    ]
    out = dict(points)
    for (i, end), (x, y) in points.items():
        if scene.ends(i)[end] == "R" and any(
            c == column(i, end) + 1 and abs(ly - y) < NEAR for c, ly in left
        ):
            out[i, end] = (x, y + NUDGE)
    return out


@dataclass(frozen=True)
class _Request:
    """One vertical run an edge needs in a gap: its y at the gap's two sides."""

    edge: int
    gap: int
    y_left: float
    y_right: float
    # "" for a run across the gap; "L" or "R" for a run that returns to one column.
    returns: str = ""


def _order(request: _Request) -> tuple[int, float, int]:
    """Return the lane order in a gap that keeps crossings few.

    Returning runs go first, nearest their column. Then runs going down, the later
    start first (leftmost), then runs going up, the earlier start first.
    """
    if request.returns:
        return (0, request.y_left, request.edge)
    if request.y_right >= request.y_left:
        return (1, -request.y_left, request.edge)
    return (2, request.y_left, request.edge)


def route(
    all_blocks: dict[str, Block],
    boxes: dict[str, Box],
    drawn_: tuple[Link, ...] | None = None,
) -> list[Route]:
    """Return every drawn edge's orthogonal polyline through the gaps.

    Channels across columns are claimed first, then each gap's lanes in `_order`.
    """
    drawn_ = links(all_blocks=all_blocks) if drawn_ is None else drawn_
    scene = _Scene(all_blocks, boxes, spans(all_blocks, boxes, drawn_), drawn_)
    points = _anchor_points(scene)
    lanes = _Lanes({}, {})
    _claim_stubs(scene, points, lanes)
    requests: list[_Request] = []
    channels: dict[int, float] = {}
    by_span = sorted(
        range(len(drawn_)), key=lambda i: (abs(points[i, 0][1] - points[i, 1][1]), i)
    )
    for i in by_span:
        requests += _requests(i, scene, points, (lanes, channels))
    xs: dict[tuple[int, int], float] = {}
    for request in sorted(requests, key=lambda r: (r.gap, _order(r))):
        x = lanes.lane(
            request.gap,
            request.y_left,
            request.y_right,
            prefer_right=request.returns == "L",
        )
        xs[request.edge, request.gap] = x
    return [
        Route(link, _points(i, scene, points, channels, xs))
        for i, link in enumerate(drawn_)
    ]


def _claim_stubs(
    scene: _Scene, points: dict[tuple[int, int], tuple[float, float]], lanes: _Lanes
) -> None:
    """Claim the gap beside each side end, so no channel runs along a stub's line."""
    for i, link in enumerate(scene.links):
        for end, (key, side) in enumerate(
            zip((link.src, link.dst), scene.ends(i), strict=True)
        ):
            column = scene.blocks[key].column
            gap = column if side == "R" else column - 1
            if side in "LR" and 0 <= gap < len(GAP_WIDTHS):
                y = points[i, end][1]
                lanes.horizontal.setdefault(round(y, 1), []).append(gap_span(gap))


def _requests(
    i: int,
    scene: _Scene,
    points: dict[tuple[int, int], tuple[float, float]],
    claims: tuple[_Lanes, dict[int, float]],
) -> list[_Request]:
    """Return the gap runs edge `i` needs; claim its channel when it crosses columns."""
    link = scene.links[i]
    (_, ys), (_, yt) = points[i, 0], points[i, 1]
    a, b = scene.blocks[link.src].column, scene.blocks[link.dst].column
    side = scene.ends(i)[0]
    if side in "TB":
        return []
    if a == b:
        gap = a if side == "R" else a - 1
        return [_Request(i, gap, ys, yt, "R" if side == "R" else "L")]
    lo, hi = min(a, b), max(a, b)
    y_lo, y_hi = (ys, yt) if a < b else (yt, ys)
    if hi - lo == 1:
        return [] if abs(ys - yt) < 1 else [_Request(i, lo, y_lo, y_hi)]
    lanes, channels = claims
    span = range(lo + 1, hi)
    x0, x1 = gap_span(lo)[0], gap_span(hi - 1)[1]
    try:
        y = lanes.slot(_free_rows(scene.boxes, span), (ys + yt) / 2, x0, x1)
    except ValueError:
        y = lanes.slot(_free_rows(scene.boxes, span, below=True), (ys + yt) / 2, x0, x1)
    channels[i] = y
    return [_Request(i, lo, y_lo, y), _Request(i, hi - 1, y, y_hi)]


def _points(
    i: int,
    scene: _Scene,
    points: dict[tuple[int, int], tuple[float, float]],
    channels: dict[int, float],
    xs: dict[tuple[int, int], float],
) -> tuple[tuple[float, float], ...]:
    """Return edge `i`'s polyline from its two ends and its claimed lanes."""
    link = scene.links[i]
    (x0, y0), (x1, y1) = points[i, 0], points[i, 1]
    lanes = sorted((gap, x) for (e, gap), x in xs.items() if e == i)
    if not lanes:
        return ((x0, y0), (x1, y0) if abs(x0 - x1) > 1 else (x0, y1))
    if len(lanes) == 1:
        x = lanes[0][1]
        return ((x0, y0), (x, y0), (x, y1), (x1, y1))
    a, b = scene.blocks[link.src].column, scene.blocks[link.dst].column
    near, far = (lanes[0][1], lanes[1][1]) if a < b else (lanes[1][1], lanes[0][1])
    y = channels[i]
    return ((x0, y0), (near, y0), (near, y), (far, y), (far, y1), (x1, y1))


def crossings(routes_: Iterable[Route]) -> int:
    """Return how many times two different edges' segments cross at a right angle."""
    segs = [
        (n, s)
        for n, r in enumerate(routes_)
        for s in zip(r.points, r.points[1:], strict=False)
    ]
    count = 0
    for i, (n, ((ax0, ay0), (ax1, ay1))) in enumerate(segs):
        for m, ((bx0, by0), (bx1, by1)) in segs[i + 1 :]:
            if n == m:
                continue
            if abs(ay0 - ay1) < 1 and abs(bx0 - bx1) < 1:
                hx, hy, vx, vy0, vy1 = (ax0, ax1), ay0, bx0, by0, by1
            elif abs(ax0 - ax1) < 1 and abs(by0 - by1) < 1:
                hx, hy, vx, vy0, vy1 = (bx0, bx1), by0, ax0, ay0, ay1
            else:
                continue
            if (
                min(hx) + 0.5 < vx < max(hx) - 0.5
                and min(vy0, vy1) + 0.5 < hy < max(vy0, vy1) - 0.5
            ):
                count += 1
    return count


# ---------------------------------------------------------------- labels


@dataclass(frozen=True)
class Label:
    """A label: its text, the box it occupies, and its edge (None for a title)."""

    text: str
    box: Box
    link: Link | None


def column_titles() -> list[Label]:
    """Return the column titles, which edge labels keep clear of."""
    return [
        Label(t, Box(column_x(c), TOP - 46, text_width(t, 12) + 4, 16), None)
        for c, t in enumerate(COLUMN_TITLES)
    ]


def place_labels(
    routes_: list[Route], boxes: dict[str, Box], titles: Iterable[Label] = ()
) -> list[Label]:
    """Return a spot on its own polyline for each edge with a label, clear of all boxes.

    A label with no free spot is left out; `problems` reports it.
    """
    taken = list(titles)
    placed: list[Label] = []
    for r in routes_:
        if not r.link.label:
            continue
        w = text_width(r.link.label, SMALL) + 4
        spot = next(
            (
                b
                for b in _label_candidates(r, w)
                if not any(b.overlaps(o, 2) for o in boxes.values())
                and not any(b.overlaps(p.box, 2) for p in (*taken, *placed))
            ),
            None,
        )
        if spot is not None:
            placed.append(Label(r.link.label, spot, r.link))
    return placed


def _label_candidates(r: Route, w: float) -> list[Box]:
    h = SMALL + 4
    out: list[Box] = []
    for (x0, y0), (x1, y1) in zip(r.points, r.points[1:], strict=False):
        for t in (0.5, 0.3, 0.7, 0.15, 0.85):
            x, y = x0 + (x1 - x0) * t, y0 + (y1 - y0) * t
            if abs(y1 - y0) < 1:
                out += [Box(x - w / 2, y - h - 1, w, h), Box(x - w / 2, y + 1, w, h)]
            else:
                out += [Box(x + 3, y - h / 2, w, h), Box(x - 3 - w, y - h / 2, w, h)]
        if abs(y1 - y0) < 1:
            # Flush with either end of a horizontal run, above or below it.
            lo, hi = min(x0, x1), max(x0, x1)
            for x in (hi - w - 4, lo + 4):
                out += [Box(x, y0 - h - 1, w, h), Box(x, y0 + 1, w, h)]
    return out


# ---------------------------------------------------------------- checks


def _segments(r: Route) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    return list(zip(r.points, r.points[1:], strict=False))


def _collinear(
    s: tuple[tuple[float, float], tuple[float, float]],
    t: tuple[tuple[float, float], tuple[float, float]],
) -> bool:
    """Return True when two axis-parallel segments lie on one line and overlap."""
    (ax0, ay0), (ax1, ay1) = s
    (bx0, by0), (bx1, by1) = t
    if abs(ay0 - ay1) < 1 and abs(by0 - by1) < 1 and abs(ay0 - by0) < 1:
        lo, hi = max(min(ax0, ax1), min(bx0, bx1)), min(max(ax0, ax1), max(bx0, bx1))
        return hi - lo > 1
    if abs(ax0 - ax1) < 1 and abs(bx0 - bx1) < 1 and abs(ax0 - bx0) < 1:
        lo, hi = max(min(ay0, ay1), min(by0, by1)), min(max(ay0, ay1), max(by0, by1))
        return hi - lo > 1
    return False


def _crosses_box(
    seg: tuple[tuple[float, float], tuple[float, float]], box: Box
) -> bool:
    (x0, y0), (x1, y1) = seg
    inner = Box(box.x + 1, box.y + 1, box.w - 2, box.h - 2)
    span = Box(min(x0, x1), min(y0, y1), abs(x1 - x0) or 0.01, abs(y1 - y0) or 0.01)
    return span.overlaps(inner)


def problems(
    boxes: dict[str, Box], routes_: list[Route], labels: list[Label]
) -> list[str]:
    """Return every legibility fault: overlaps, edges through boxes, shared lines."""
    found: list[str] = []
    keys = sorted(boxes)
    found += [
        f"boxes overlap: {a} and {b}"
        for i, a in enumerate(keys)
        for b in keys[i + 1 :]
        if boxes[a].overlaps(boxes[b], MIN_GAP / 2)
    ]
    for r in routes_:
        found += [
            f"edge {r.link.src} -> {r.link.dst} crosses {k}"
            for seg in _segments(r)
            for k in keys
            if k not in {r.link.src, r.link.dst} and _crosses_box(seg, boxes[k])
        ]
    for i, r in enumerate(routes_):
        found += [
            f"edges share a line: {r.link.src} -> {r.link.dst} and "
            f"{q.link.src} -> {q.link.dst}"
            for q in routes_[i + 1 :]
            if any(_collinear(s, t) for s in _segments(r) for t in _segments(q))
        ]
    placed = [lb.link for lb in labels]
    found += [
        f"no room for the label {r.link.label!r} of {r.link.src} -> {r.link.dst}"
        for r in routes_
        if r.link.label and r.link not in placed
    ]
    found += [
        f"labels overlap: {a.text} and {b.text}"
        for i, a in enumerate(labels)
        for b in labels[i + 1 :]
        if a.box.overlaps(b.box)
    ]
    found += [
        f"label {lb.text!r} overlaps {k}"
        for lb in labels
        for k in keys
        if lb.box.overlaps(boxes[k])
    ]
    return found


def _too_wide(key: str, lines: Iterable[Line], room: float) -> list[str]:
    return [
        f"{key}: {text!r} is wider than its box"
        for text, cls in lines
        if text_width(text, SMALL, mono=cls == "mono") > room
    ]


def text_problems(all_blocks: dict[str, Block]) -> list[str]:
    """Return each box text that would not fit, and each reads list over two lines."""
    found: list[str] = []
    for block in all_blocks.values():
        room = COLUMN_WIDTHS[block.column] - 2 * PAD
        if text_width(block.label, TITLE_SIZE) > room:
            found.append(f"{block.key}: {block.label!r} is wider than its box")
        found += _too_wide(block.key, [(block.tag, ""), *block.lines], room)
        for m in block.members:
            head = text_width(f"{BULLET} {m.title}", SMALL) + 8
            head += text_width(m.tag, SMALL, mono=True)
            if head > room:
                found.append(f"{m.key}: {m.title!r} and {m.tag!r} overflow")
            found += _too_wide(m.key, m.lines, room - INDENT)
    all_nodes = nodes()
    for key, prefixes in notes(all_nodes).items():
        column = all_blocks[where()[key][0]].column
        room = COLUMN_WIDTHS[column] - 2 * PAD - (INDENT if where()[key][1] else 0)
        found += [
            f"{key}: its {prefix} need more than two lines"
            for prefix, names in prefixes.items()
            if len(wrap(prefix, names, room)) > MAX_NOTE_LINES
        ]
    return found


# ---------------------------------------------------------------- render

STYLE: Final = """
svg { --bg: #ffffff; --fg: #1f2328; --muted: #57606a; --line: #4b5563;
  --read: #6b7280; --group: #8c959f; --halo: #ffffff;
  --actor: #f6f8fa; --edge: #eef2ff; --fn: #ecfdf5; --queue: #fff7ed;
  --sm: #fdf2f8; --bucket: #eff6ff; --table: #f0f9ff; --catalog: #f5f3ff;
  --s-data: #2563eb; --s-api: #059669; --s-ingest: #d97706; --s-external: #9ca3af;
  --monitoring: #fefce8; --accent: #c2410c; }
@media (prefers-color-scheme: dark) {
  svg { --bg: #0d1117; --fg: #e6edf3; --muted: #9da7b3; --line: #c9d1d9;
    --read: #9ca3af; --group: #6e7681; --halo: #0d1117;
    --actor: #161b22; --edge: #1e1b4b; --fn: #052e2b; --queue: #331d05;
    --sm: #3b0a24; --bucket: #0b1f3f; --table: #082f49; --catalog: #2e1065;
    --s-data: #60a5fa; --s-api: #34d399; --s-ingest: #fbbf24; --s-external: #6b7280;
    --monitoring: #2a2506; --accent: #fb923c; }
}
.bg { fill: var(--bg); }
text { fill: var(--fg); font-family: FONT; font-size: 11px; }
.mono { font-family: MONO; }
.title { font-size: 13px; font-weight: 600; }
.bullet { font-weight: 600; }
.h1 { font-size: 16px; font-weight: 600; }
.h2 { font-size: 12px; font-weight: 600; fill: var(--muted); }
.muted { fill: var(--muted); }
.node { stroke: var(--group); stroke-width: 1; }
.port { stroke: var(--group); stroke-width: 0.75; fill: none; }
.group { fill: none; stroke: var(--group); stroke-width: 1; stroke-dasharray: 6 4; }
.e { fill: none; stroke-width: 1.4; }
.write { stroke: var(--line); }
.read { stroke: var(--read); stroke-dasharray: 5 3; }
.head-write { fill: var(--line); }
.head-read { fill: var(--read); }
.dim { opacity: 0.25; }
.flow { stroke: var(--accent); stroke-width: 2.2; }
.badge { font-weight: 700; fill: var(--accent); }
.free { font-style: italic; fill: var(--muted); }
.elabel { paint-order: stroke; stroke: var(--halo); stroke-width: 4px;
  stroke-linejoin: round; }
.k-actor { fill: var(--actor); } .k-edge-service { fill: var(--edge); }
.k-function-zip, .k-function-image { fill: var(--fn); }
.k-queue { fill: var(--queue); } .k-state-machine { fill: var(--sm); }
.k-bucket { fill: var(--bucket); } .k-table { fill: var(--table); }
.k-catalog { fill: var(--catalog); } .k-monitoring { fill: var(--monitoring); }
.s-data { fill: var(--s-data); } .s-api { fill: var(--s-api); }
.s-ingest { fill: var(--s-ingest); } .s-external { fill: var(--s-external); }
""".replace("FONT", FONT).replace("MONO", MONO)

LEGEND_HEIGHT: Final = 124
LEGEND_KINDS: Final = (
    FUNCTION_ZIP,
    QUEUE,
    STATE_MACHINE,
    BUCKET,
    TABLE,
    CATALOG,
    MONITORING,
)


def _num(v: float) -> str:
    return f"{round(v, 1):g}"


def _text(x: float, y: float, text: str, cls: str = "", anchor: str = "") -> str:
    attrs = f' class="{cls}"' if cls else ""
    attrs += f' text-anchor="{anchor}"' if anchor else ""
    return f'<text x="{_num(x)}" y="{_num(y)}"{attrs}>{escape(text)}</text>'


def _block_svg(
    block: Block,
    box: Box,
    rows: dict[tuple[str, str], tuple[float, float]],
    lit: set[str] | None = None,
) -> list[str]:
    x, y = box.x, box.y
    out: list[str] = [
        f"<g><title>{escape(block.key)}</title>",
        (
            f'<rect class="node k-{block.kind}" x="{_num(x)}" y="{_num(y)}" '
            f'width="{_num(box.w)}" height="{_num(box.h)}" rx="6"/>'
        ),
        (
            f'<rect class="s-{block.stack}" x="{_num(x + 0.5)}" y="{_num(y + 0.5)}" '
            f'width="{_num(box.w - 1)}" height="{STRIP}" rx="3"/>'
        ),
        _text(x + PAD, y + STRIP + PAD + 10, block.label, "title"),
        _text(x + PAD, y + STRIP + PAD + LINE + 10, block.tag, "muted"),
    ]
    line_y = y + STRIP + PAD + 2 * LINE + 10
    for text, cls in block.lines:
        out.append(_text(x + PAD, line_y, text, cls))
        line_y += LINE
    for member in block.members:
        top, _ = rows[block.key, member.key]
        out.append(
            f'<line class="port" x1="{_num(x)}" y1="{_num(top)}" '
            f'x2="{_num(box.right)}" y2="{_num(top)}"/>'
        )
    out.append("</g>")
    for member in block.members:
        top, _ = rows[block.key, member.key]
        dim = ' class="dim"' if lit is not None and member.key not in lit else ""
        out += [
            f"<g{dim}><title>{escape(member.key)}</title>",
            _text(x + PAD, top + LINE, f"{BULLET} {member.title}", "bullet"),
        ]
        if member.tag:
            out.append(_text(box.right - PAD, top + LINE, member.tag, "mono", "end"))
        line_y = top + 2 * LINE
        for text, cls in member.lines:
            out.append(_text(x + PAD + INDENT, line_y, text, cls))
            line_y += LINE
        out.append("</g>")
    return out


def _edge_svg(r: Route, *, flow: bool = False) -> str:
    pts = " ".join(f"{_num(x)},{_num(y)}" for x, y in r.points)
    verb = r.link.verb
    marker = (
        'marker-end="url(#head-write)"'
        if verb == WRITE
        else 'marker-start="url(#head-read)"'
    )
    carried = "; ".join(
        f"{e.src} {e.verb}s {e.dst}{' ' + e.port + '/' if e.port else ''}: {e.basis}"
        for e in r.link.edges
    )
    return (
        f'<polyline class="e {verb}{" flow" if flow else ""}" points="{pts}" '
        f"{marker}>"
        f"<title>{escape(carried)}</title></polyline>"
    )


def _defs() -> list[str]:
    return [
        "<defs>",
        (
            '<marker id="head-write" viewBox="0 0 10 10" refX="10" refY="5" '
            'markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
            '<path class="head-write" d="M0,0 L10,5 L0,10 z"/></marker>'
        ),
        (
            '<marker id="head-read" viewBox="0 0 10 10" refX="10" refY="5" '
            'markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
            '<path class="head-read" d="M0,0 L10,5 L0,10 z"/></marker>'
        ),
        "</defs>",
    ]


def _groups(boxes: dict[str, Box]) -> list[str]:
    out = [_text(lb.box.x, lb.box.bottom - 4, lb.text, "h2") for lb in column_titles()]
    for column in STACK_COLUMNS:
        keys = ORDER[column]
        top = min(boxes[k].y for k in keys) - 14
        bottom = max(boxes[k].bottom for k in keys) + 10
        x = column_x(column) - 8
        out.append(
            f'<rect class="group" x="{_num(x)}" y="{_num(top)}" '
            f'width="{_num(COLUMN_WIDTHS[column] + 16)}" '
            f'height="{_num(bottom - top)}" rx="10"/>'
        )
    return out


def _swatches(x: float, y: float, items: Iterable[tuple[str, str]]) -> list[str]:
    out: list[str] = []
    for cls, text in items:
        out.append(
            f'<rect class="{cls}" x="{_num(x)}" y="{_num(y - 11)}" '
            'width="14" height="14" rx="3"/>'
        )
        out.append(_text(x + 20, y, text))
        x += 34 + text_width(text, SMALL)
    return out


def legend_box(boxes: dict[str, Box]) -> Box:
    """Return the legend's place: below the lowest box."""
    top = max(b.bottom for b in boxes.values()) + 20
    return Box(MARGIN, top, WIDTH - 2 * MARGIN, LEGEND_HEIGHT)


def _legend(box: Box) -> list[str]:
    x, y = box.x, box.y + 12
    out = [_text(x, y, "Legend", "h2")]
    y += 22
    out += [
        (
            f'<line class="e write" x1="{_num(x)}" y1="{_num(y - 4)}" '
            f'x2="{_num(x + 40)}" y2="{_num(y - 4)}" marker-end="url(#head-write)"/>'
        ),
        _text(x + 48, y, "write, send or invoke (caller to resource)"),
        (
            f'<line class="e read" x1="{_num(x + 330)}" y1="{_num(y - 4)}" '
            f'x2="{_num(x + 370)}" y2="{_num(y - 4)}" marker-start="url(#head-read)"/>'
        ),
        _text(x + 378, y, "read (resource to caller)"),
        _text(
            x + 560,
            y,
            (
                "To a queue: send. To a function: invoke. To Athena: query. "
                "To the HTTP API: a call with a JWT."
            ),
            "muted",
        ),
    ]
    y += 24
    out += _swatches(x, y, ((f"s-{s}", t) for s, t in STACK_TITLES.items()))
    y += 24
    out += _swatches(
        x,
        y,
        (
            (f"node k-{k}", "Lambda" if k == FUNCTION_ZIP else KIND_TAGS[k])
            for k in LEGEND_KINDS
        ),
    )
    y += 24
    out.append(
        _text(
            x,
            y,
            "Simplified, not resource-exact: resources group by kind, one bullet "
            "each; dead-letter queues fold into their queue; routes collapse per "
            "function; edges between the same two boxes collapse into one, labelled "
            "with what they carry.",
            "muted",
        )
    )
    y += 16
    out.append(
        _text(
            x,
            y,
            "Reads are listed in the boxes (reads: ...), not drawn; the drawn reads "
            "are a consumer's queue, a caller's entry, CloudFront's site/ and the "
            "archive fetches.",
            "muted",
        )
    )
    return out


def overview_routes() -> tuple[dict[str, Box], list[Route]]:
    """Return the overview's boxes and routed edges."""
    all_blocks = blocks()
    boxes = layout(all_blocks)
    return boxes, route(all_blocks, boxes)


def render_overview() -> str:
    """Return the overview SVG: the backbone over every model node and edge."""
    all_blocks = blocks()
    drawn_ = links(all_blocks=all_blocks)
    boxes = layout(all_blocks, drawn_)
    routes_ = route(all_blocks, boxes)
    labels = place_labels(routes_, boxes, column_titles())
    rows = spans(all_blocks, boxes, drawn_)
    legend = legend_box(boxes)
    height = legend.bottom + 8
    out = [
        (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" '
            f'height="{_num(height)}" viewBox="0 0 {WIDTH} {_num(height)}" '
            'role="img" aria-labelledby="title desc">'
        ),
        '<title id="title">asteroidea: system overview</title>',
        (
            '<desc id="desc">The deployed resources of asteroidea, grouped by kind '
            "and by stack, and the main data-flow edges between them; reads are "
            "listed in the boxes. Generated by make architecture.</desc>"
        ),
        f"<style>{STYLE}</style>",
        *_defs(),
        f'<rect class="bg" x="0" y="0" width="{WIDTH}" height="{_num(height)}"/>',
        _text(MARGIN, MARGIN + 16, "asteroidea: system overview", "h1"),
        _text(
            MARGIN + 230,
            MARGIN + 16,
            "Generated from the stack constants by make architecture; "
            "do not edit by hand.",
            "muted",
        ),
        *_groups(boxes),
        *(_edge_svg(r) for r in routes_),
    ]
    for key in sorted(boxes):
        out += _block_svg(all_blocks[key], boxes[key], rows)
    out += [_text(lb.box.x + 2, lb.box.bottom - 4, lb.text, "elabel") for lb in labels]
    out += _legend(legend)
    out.append("</svg>")
    return "\n".join(out) + "\n"


def overview_problems() -> list[str]:
    """Return every legibility fault of the overview; empty when it reads cleanly."""
    all_blocks = blocks()
    boxes = layout(all_blocks)
    routes_ = route(all_blocks, boxes)
    titles = column_titles()
    labels = [*place_labels(routes_, boxes, titles), *titles]
    # The legend counts as a box: no edge, label or node may cross it.
    with_legend = {**boxes, "legend": legend_box(boxes)}
    return [*problems(with_legend, routes_, labels), *text_problems(all_blocks)]


# ---------------------------------------------------------------- use cases


def _s(src: str, dst: str, verb: str, text: str) -> Step:
    return Step(text, src, dst, verb)


def _free(text: str) -> Step:
    return Step(text)


_COMMIT_CHAIN: Final = (
    _s("CommitFunction", "CommitQueue", R, "the queue triggers the commit"),
    _s("CommitFunction", "RawBucket", R, "reads the file from landing/"),
    _s("CommitFunction", "RawBucket", W, "writes the immutable copy to raw/"),
    _s("CommitFunction", "WarehouseBucket", W, "commits the assets rows to iceberg/"),
    _s("CommitFunction", "SessionsTable", W, "marks the session committed"),
    _s("CommitFunction", "ExtractQueue", W, "queues the extract"),
    _s("ExtractFunction", "ExtractQueue", R, "the queue triggers the extract"),
    _s("ExtractFunction", "RawBucket", R, "reads the NWB file from raw/"),
    _s("ExtractFunction", "WarehouseBucket", W, "commits the extracted tables"),
    _s("ExtractFunction", "SessionsTable", W, "records the extract status"),
    _s("ExtractFunction", "LfpBuildQueue", W, "queues the LFP build"),
    _s("LfpBuildFunction", "LfpBuildQueue", R, "the queue triggers the build"),
    _s("LfpBuildFunction", "DerivedBucket", W, "writes lfp/ and summary/"),
)

FLOWS: Final = (
    Flow(
        "curator-import",
        "Curator import: an archive dataset into the lake",
        (
            _s("Browser", "HttpApi", W, "a curator posts /imports"),
            _s("HttpApi", "ImportsFunction", W, "the route invokes the imports API"),
            _s("ImportsFunction", "ImportRunsTable", W, "records the import run"),
            _s("ImportsFunction", "ImportPlanFunction", W, "invokes the planner"),
            _s("ImportPlanFunction", "DandiApi", R, "lists the dataset's assets"),
            _s("ImportPlanFunction", "WarehouseBucket", R, "skips assets in iceberg/"),
            _s("ImportPlanFunction", "ImportRunsTable", W, "records the plan"),
            _s(
                "ImportPlanFunction",
                "ImportFunction",
                W,
                "invokes one import per asset",
            ),
            _s("Curator", "ImportFunction", W, "or: make import invokes it directly"),
            _s("ImportFunction", "DandiApi", R, "fetches the file"),
            _s("ImportFunction", "RawBucket", W, "writes it to landing/"),
            _s("ImportFunction", "SessionsTable", W, "creates the session"),
            _s("ImportFunction", "OwnersTable", W, "records the dataset owner"),
            _s("ImportFunction", "CommitQueue", W, "queues the commit"),
            *_COMMIT_CHAIN,
        ),
    ),
    Flow(
        "researcher-upload",
        "Researcher upload: a local NWB file into the lake",
        (
            _s("Uploader", "UserPool", W, "signs in with PKCE"),
            _s("Uploader", "HttpApi", W, "posts /sessions with the id token"),
            _s("HttpApi", "SessionFunction", W, "the route invokes the sessions API"),
            _s("SessionFunction", "SessionsTable", W, "creates the session"),
            _s("SessionFunction", "OwnersTable", W, "records the dataset owner"),
            _s("SessionFunction", "RawBucket", W, "presigns a PUT to landing/"),
            _free("the client PUTs the file to the presigned URL"),
            _s("SessionFunction", "CommitQueue", W, "on finalize, queues the commit"),
            *_COMMIT_CHAIN,
        ),
    ),
    Flow(
        "sdk-analysis",
        "SDK analysis: a notebook reads the lake",
        (
            _s("Researcher", "UserPool", W, "signs in with PKCE"),
            _s("Researcher", "IdentityPool", R, "trades the id token for credentials"),
            _s("IdentityPool", "ResearcherRole", R, "assumes the read-only role"),
            _s(
                "ResearcherRole",
                "WarehouseBucket",
                R,
                "DuckDB, PyIceberg read iceberg/",
            ),
            _s("ResearcherRole", "RawBucket", R, "remfile streams NWB from raw/"),
            _s("ResearcherRole", "AthenaWorkgroup", R, "runs SQL in the workgroup"),
            _s("AthenaWorkgroup", "GlueDatabase", R, "resolves the Iceberg tables"),
            _s("AthenaWorkgroup", "WarehouseBucket", R, "scans iceberg/"),
            _s(
                "AthenaWorkgroup", "AthenaResultsBucket", W, "writes results to athena/"
            ),
            _s("Researcher", "HttpApi", W, "calls the API for the catalog"),
            _s("HttpApi", "CatalogFunction", W, "the route invokes the catalog API"),
            _s("CatalogFunction", "AthenaWorkgroup", R, "queries the catalog"),
        ),
    ),
    Flow(
        "web-browse-compute",
        "Web browse and compute: the web app",
        (
            _s("Browser", "CloudFront", R, "loads the web app"),
            _s("CloudFront", "SiteBucket", R, "serves it from site/"),
            _s("Browser", "UserPool", W, "signs in through managed login"),
            _s("Browser", "HttpApi", W, "calls the API with the id token"),
            _s("HttpApi", "CatalogFunction", W, "the catalog routes"),
            _s("CatalogFunction", "AthenaWorkgroup", R, "queries the catalog tables"),
            _s("CatalogFunction", "RawBucket", R, "reads file headers from raw/"),
            _s("HttpApi", "ComputeFunction", W, "the compute routes"),
            _s("ComputeFunction", "AthenaWorkgroup", R, "queries spikes and events"),
            _s("ComputeFunction", "DerivedBucket", R, "reads lfp/ and summary/"),
            _s("ComputeFunction", "LabelsTable", R, "reads the labels"),
            _s("ComputeFunction", "RawBucket", R, "reads raw signal from raw/"),
        ),
    ),
    Flow(
        "artifacts-provenance",
        "Artifacts and provenance: saving an analysis result",
        (
            _s("Researcher", "HttpApi", W, "posts /artifacts"),
            _s("HttpApi", "PlatformFunction", W, "the route invokes the platform API"),
            _s(
                "PlatformFunction", "ArtifactsBucket", W, "presigns a PUT to artifacts/"
            ),
            _free("the SDK PUTs the result and its provenance to the presigned URL"),
            _s(
                "PlatformFunction",
                "ArtifactsBucket",
                R,
                "on finalize, checks the upload",
            ),
            _s("PlatformFunction", "ArtifactsBucket", W, "writes the index/ entry"),
            _s("PlatformFunction", "ArtifactsBucket", R, "GET /artifacts reads index/"),
        ),
    ),
    Flow(
        "reextract",
        "Re-extract: rebuild derived rows and re-run extract",
        (
            _s("Curator", "ReextractStateMachine", W, "make reextract starts it"),
            _s(
                "ReextractStateMachine",
                "DeriveFunction",
                W,
                "invokes derive per session",
            ),
            _s("DeriveFunction", "RawBucket", R, "reads the NWB file from raw/"),
            _s("DeriveFunction", "DerivedBucket", W, "writes rows/"),
            _s("DeriveFunction", "SessionsTable", W, "records the derive status"),
            _s("ReextractStateMachine", "ExtractQueue", W, "sends the extract pointer"),
            _s("ExtractFunction", "ExtractQueue", R, "the queue triggers the extract"),
            _s("ExtractFunction", "DerivedBucket", R, "reads the rows/ it needs"),
            _s("ExtractFunction", "WarehouseBucket", W, "commits the tables"),
            _s("ExtractFunction", "SessionsTable", W, "records the extract status"),
            _s("ReextractStateMachine", "SessionsTable", W, "on failure, records it"),
        ),
    ),
)


def step_edge(step: Step) -> Edge | None:
    """Return the one model edge a step names, or None for free text.

    Raises ValueError when the step names no edge or more than one.
    """
    if not step.src:
        return None
    found = [
        e for e in EDGES if (e.src, e.dst, e.verb) == (step.src, step.dst, step.verb)
    ]
    if len(found) != 1:
        msg = f"step {step.text!r} names {len(found)} model edges"
        raise ValueError(msg)
    return found[0]


def flow_start(edge: Edge, all_nodes: dict[str, Node]) -> str:
    """Return where a step starts: the queue for a consumer's read, else the caller."""
    if edge.verb == READ and all_nodes[edge.dst].kind == QUEUE:
        return edge.dst
    return edge.src


def order_problems(flow: Flow, all_nodes: dict[str, Node]) -> list[str]:
    """Return each step that starts where no earlier step reached, unless named."""
    reached: set[str] = set()
    jumps = dict(flow.jumps)
    found: list[str] = []
    for n, step in enumerate(flow.steps, 1):
        edge = step_edge(step)
        if edge is None:
            continue
        start = flow_start(edge, all_nodes)
        if start not in reached and all_nodes[start].kind != ACTOR and n not in jumps:
            found.append(f"{flow.name} step {n} starts at {start}, not reached")
        reached |= {edge.src, edge.dst}
    return found


def _end_name(edge: Edge, end: int, all_nodes: dict[str, Node]) -> str:
    return token(edge, all_nodes) if end else SHORT.get(edge.src, edge.src)


def flow_links(
    flow: Flow, all_nodes: dict[str, Node], all_blocks: dict[str, Block]
) -> tuple[Link, ...]:
    """Return one drawn edge per model edge of the flow, labelled with its steps.

    An end on a group lands on its member's bullet; on a top or bottom side, where a
    bullet cannot be reached, the label names the member instead.
    """
    places = where()
    numbers: dict[Edge, list[str]] = {}
    for n, step in enumerate(flow.steps, 1):
        edge = step_edge(step)
        if edge is not None:
            numbers.setdefault(edge, []).append(str(n))
    out: list[Link] = []
    for edge, ns in numbers.items():
        (sb, sm), (db, dm) = places[edge.src], places[edge.dst]
        ends = sides(sb, db, all_blocks)
        anchors = [
            m if side in "LR" else "" for m, side in zip((sm, dm), ends, strict=True)
        ]
        names = [
            _end_name(edge, end, all_nodes)
            for end, m in enumerate((sm, dm))
            if m and not anchors[end]
        ]
        label = " ".join([", ".join(ns), *names])
        out.append(Link(sb, db, edge.verb, (edge,), anchors[0], anchors[1], label))
    return tuple(out)


def flow_scene(
    flow: Flow,
) -> tuple[dict[str, Block], tuple[Link, ...], tuple[Link, ...]]:
    """Return the boxes, the flow's edges and the dimmed overview edges behind them."""
    all_nodes = nodes()
    all_blocks = blocks(all_nodes)
    front = flow_links(flow, all_nodes, all_blocks)
    used = {e for link in front for e in link.edges}
    back = tuple(
        Link(
            link.src,
            link.dst,
            link.verb,
            link.edges,
            link.src_member,
            link.dst_member,
            "",
        )
        for link in links(all_nodes, all_blocks)
        if not set(link.edges) <= used
    )
    return all_blocks, front, back


STEP_COLUMNS: Final = 2
STEP_TOP: Final = 34


def step_lines(flow: Flow) -> list[tuple[str, str]]:
    """Return each step's numbered line and its class ("free" for no AWS edge)."""
    all_nodes = nodes()
    out: list[tuple[str, str]] = []
    for n, step in enumerate(flow.steps, 1):
        edge = step_edge(step)
        if edge is None:
            out.append((f"{n}. [no AWS edge] {step.text}", "free"))
            continue
        arrow = "<-" if edge.verb == READ else "->"
        src, dst = all_nodes[edge.src].label, all_nodes[edge.dst].label
        out.append((f"{n}. {src} {arrow} {dst}: {step.text}", ""))
    return out


def steps_box(flow: Flow, boxes: dict[str, Box]) -> Box:
    """Return the step list's place: below the boxes, in columns."""
    rows = -(-len(flow.steps) // STEP_COLUMNS)
    top = max(b.bottom for b in boxes.values()) + 20
    return Box(MARGIN, top, WIDTH - 2 * MARGIN, STEP_TOP + rows * LINE)


def _steps_svg(flow: Flow, box: Box) -> list[str]:
    out = [_text(box.x, box.y + 14, f"Steps: {flow.title}", "h2")]
    rows = -(-len(flow.steps) // STEP_COLUMNS)
    width = box.w / STEP_COLUMNS
    for i, (text, cls) in enumerate(step_lines(flow)):
        x = box.x + (i // rows) * width
        y = box.y + STEP_TOP + (i % rows) * LINE
        out.append(_text(x, y, text, cls))
    return out


def _flow_parts(
    flow: Flow,
) -> tuple[dict[str, Block], dict[str, Box], list[Route], tuple[Link, ...]]:
    all_blocks, front, back = flow_scene(flow)
    drawn_ = (*back, *front)
    boxes = layout(all_blocks, drawn_)
    return all_blocks, boxes, route(all_blocks, boxes, drawn_), front


def _flow_members(flow: Flow) -> set[str]:
    return {
        k
        for step in flow.steps
        if (edge := step_edge(step)) is not None
        for k in (edge.src, edge.dst)
    }


def render_flow(flow: Flow) -> str:
    """Return one use case's SVG: the overview, the flow lit and numbered."""
    all_blocks, boxes, routes_, front = _flow_parts(flow)
    lit = _flow_members(flow)
    places = where()
    lit_blocks = {places[k][0] for k in lit}
    labels = place_labels(
        [r for r in routes_ if r.link in front], boxes, column_titles()
    )
    rows = spans(all_blocks, boxes, tuple(r.link for r in routes_))
    steps = steps_box(flow, boxes)
    legend = legend_box({"steps": steps})
    height = legend.bottom + 8
    out = [
        (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" '
            f'height="{_num(height)}" viewBox="0 0 {WIDTH} {_num(height)}" '
            'role="img" aria-labelledby="title desc">'
        ),
        f'<title id="title">asteroidea: {escape(flow.title)}</title>',
        (
            f'<desc id="desc">{escape(flow.title)}: the overview with this use '
            "case's edges lit and numbered, and its steps listed below. Generated by "
            "make architecture.</desc>"
        ),
        f"<style>{STYLE}</style>",
        *_defs(),
        f'<rect class="bg" x="0" y="0" width="{WIDTH}" height="{_num(height)}"/>',
        _text(MARGIN, MARGIN + 16, flow.title, "h1"),
        *_groups(boxes),
    ]
    out += [
        f'<g class="dim">{_edge_svg(r)}</g>' for r in routes_ if r.link not in front
    ]
    out += [_edge_svg(r, flow=True) for r in routes_ if r.link in front]
    for key in sorted(boxes):
        part = _block_svg(all_blocks[key], boxes[key], rows, lit)
        out += part if key in lit_blocks else ['<g class="dim">', *part, "</g>"]
    out += [
        _text(lb.box.x + 2, lb.box.bottom - 4, lb.text, "elabel badge") for lb in labels
    ]
    out += _steps_svg(flow, steps)
    out += _legend(legend)
    out.append("</svg>")
    return "\n".join(out) + "\n"


def flow_problems(flow: Flow) -> list[str]:
    """Return every legibility fault of one use case's SVG, dimmed edges included."""
    all_blocks, boxes, routes_, front = _flow_parts(flow)
    titles = column_titles()
    labels = [
        *place_labels([r for r in routes_ if r.link in front], boxes, titles),
        *titles,
    ]
    steps = steps_box(flow, boxes)
    room = steps.w / STEP_COLUMNS - 12
    with_parts = {**boxes, "steps": steps, "legend": legend_box({"steps": steps})}
    return [
        *problems(with_parts, routes_, labels),
        *text_problems(all_blocks),
        *(
            f"{flow.name}: step {text!r} is too wide"
            for text, _ in step_lines(flow)
            if text_width(text, SMALL) > room
        ),
        *order_problems(flow, nodes()),
    ]


def flow_path(flow: Flow) -> Path:
    """Return where a use case's SVG lives."""
    return OUT_DIR / f"{flow.name}.svg"


def main() -> int:
    """Write the overview and the use-case SVGs into `docs/src/architecture/`."""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    OVERVIEW.write_text(render_overview(), encoding="utf-8")
    found = overview_problems()
    for flow in FLOWS:
        flow_path(flow).write_text(render_flow(flow), encoding="utf-8")
        found += flow_problems(flow)
    for problem in found:
        sys.stderr.write(f"{problem}\n")
    boxes, routes_ = overview_routes()
    sys.stderr.write(
        f"overview: {len(routes_)} drawn edges over {len(EDGES)} model edges, "
        f"{crossings(routes_)} crossings, {len(boxes)} boxes\n"
    )
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
