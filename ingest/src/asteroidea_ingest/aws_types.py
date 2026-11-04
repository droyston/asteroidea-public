"""Structural types for the boto3 clients the ingest Lambdas use.

Each Protocol lists only the methods this package calls, with the keyword arguments
boto3 expects (spelt as the service API spells them), so the call sites are checked
without `Any` and the tests can still pass real stubbed boto3 clients.
"""

from typing import Any, Final, NotRequired, Protocol, TypedDict, Unpack

# S3 error codes that mean "no such key" (HeadObject answers 404, GetObject NoSuchKey).
S3_NOT_FOUND: Final = frozenset({"404", "NoSuchKey", "NotFound"})


class GetItemArgs(TypedDict):
    """Keyword arguments of `DynamoDB.GetItem` as used here."""

    TableName: str
    Key: dict[str, Any]
    ConsistentRead: bool


class PutItemArgs(TypedDict):
    """Keyword arguments of `DynamoDB.PutItem` as used here."""

    TableName: str
    Item: dict[str, Any]
    ConditionExpression: str
    ExpressionAttributeValues: NotRequired[dict[str, Any]]


class UpdateItemArgs(TypedDict):
    """Keyword arguments of `DynamoDB.UpdateItem` as used here."""

    TableName: str
    Key: dict[str, Any]
    UpdateExpression: str
    ConditionExpression: str
    ExpressionAttributeNames: dict[str, str]
    ExpressionAttributeValues: dict[str, Any]
    ReturnValues: NotRequired[str]


class QueryArgs(TypedDict):
    """Keyword arguments of `DynamoDB.Query` as used here."""

    TableName: str
    IndexName: str
    KeyConditionExpression: str
    ExpressionAttributeValues: dict[str, Any]
    ScanIndexForward: bool
    Limit: int


class ScanArgs(TypedDict):
    """Keyword arguments of `DynamoDB.Scan` as used here (`make reclaim`)."""

    TableName: str
    ExclusiveStartKey: NotRequired[dict[str, Any]]


class DynamoDb(Protocol):
    """The DynamoDB client methods the session store calls."""

    def get_item(self, **kwargs: Unpack[GetItemArgs]) -> dict[str, Any]:
        """Read one item."""
        ...

    def put_item(self, **kwargs: Unpack[PutItemArgs]) -> dict[str, Any]:
        """Write one item."""
        ...

    def update_item(self, **kwargs: Unpack[UpdateItemArgs]) -> dict[str, Any]:
        """Update one item."""
        ...

    def query(self, **kwargs: Unpack[QueryArgs]) -> dict[str, Any]:
        """Read items by key condition."""
        ...

    def scan(self, **kwargs: Unpack[ScanArgs]) -> dict[str, Any]:
        """Read one page of every item."""
        ...


class OwnerStore(Protocol):
    """The DynamoDB methods the dataset-owners table needs (`owners.py`)."""

    def get_item(self, **kwargs: Unpack[GetItemArgs]) -> dict[str, Any]:
        """Read one item."""
        ...

    def put_item(self, **kwargs: Unpack[PutItemArgs]) -> dict[str, Any]:
        """Write one item."""
        ...


class DeleteItemArgs(TypedDict):
    """Keyword arguments of `DynamoDB.DeleteItem` as used here (the labels table)."""

    TableName: str
    Key: dict[str, Any]
    ConditionExpression: str
    ExpressionAttributeNames: dict[str, str]
    ExpressionAttributeValues: dict[str, Any]


class ItemsQueryArgs(TypedDict):
    """Keyword arguments of a table `DynamoDB.Query` as used here (the labels table)."""

    TableName: str
    KeyConditionExpression: str
    ExpressionAttributeValues: dict[str, Any]
    ConsistentRead: bool
    ProjectionExpression: NotRequired[str]
    ExclusiveStartKey: NotRequired[dict[str, Any]]


class LabelReader(Protocol):
    """The one DynamoDB method the compute label aligns need (`compute.py`)."""

    def query(self, **kwargs: Unpack[ItemsQueryArgs]) -> dict[str, Any]:
        """Read one page of the items under a partition key."""
        ...


class LabelStore(Protocol):
    """The DynamoDB methods the labels table needs (`labels_api.py`)."""

    def get_item(self, **kwargs: Unpack[GetItemArgs]) -> dict[str, Any]:
        """Read one item."""
        ...

    def put_item(self, **kwargs: Unpack[PutItemArgs]) -> dict[str, Any]:
        """Write one item."""
        ...

    def update_item(self, **kwargs: Unpack[UpdateItemArgs]) -> dict[str, Any]:
        """Update one item."""
        ...

    def delete_item(self, **kwargs: Unpack[DeleteItemArgs]) -> dict[str, Any]:
        """Delete one item."""
        ...

    def query(self, **kwargs: Unpack[ItemsQueryArgs]) -> dict[str, Any]:
        """Read one page of the items under a partition key."""
        ...


class HeadObjectArgs(TypedDict):
    """Keyword arguments of `S3.HeadObject` as used here."""

    Bucket: str
    Key: str
    ChecksumMode: NotRequired[str]


class CopyObjectArgs(TypedDict):
    """Keyword arguments of `S3.CopyObject` as used here."""

    Bucket: str
    Key: str
    CopySource: dict[str, str]
    ChecksumAlgorithm: str
    MetadataDirective: NotRequired[str]
    TaggingDirective: NotRequired[str]


class PutObjectArgs(TypedDict):
    """Keyword arguments of `S3.PutObject` as used here (the import stream)."""

    Bucket: str
    Key: str
    Body: object
    ContentLength: NotRequired[int]
    ChecksumSHA256: NotRequired[str]
    ContentType: NotRequired[str]


class GetObjectArgs(TypedDict):
    """Keyword arguments of `S3.GetObject` as used here (ranged reads)."""

    Bucket: str
    Key: str
    VersionId: NotRequired[str]
    Range: NotRequired[str]


class ListObjectsArgs(TypedDict):
    """Keyword arguments of `S3.ListObjectsV2` as used here."""

    Bucket: str
    Prefix: str
    MaxKeys: int
    Delimiter: NotRequired[str]
    ContinuationToken: NotRequired[str]


class ObjectLister(Protocol):
    """The S3 method the ownership probe calls (any S3 client has it)."""

    def list_objects_v2(self, **kwargs: Unpack[ListObjectsArgs]) -> dict[str, Any]:
        """List up to `MaxKeys` objects under a prefix."""
        ...


class DeleteObjectArgs(TypedDict):
    """Keyword arguments of `S3.DeleteObject` as used here."""

    Bucket: str
    Key: str
    VersionId: NotRequired[str]


class PutObjectRetentionArgs(TypedDict):
    """Keyword arguments of `S3.PutObjectRetention` as used here (Object Lock)."""

    Bucket: str
    Key: str
    VersionId: str
    Retention: dict[str, Any]


class PresignArgs(TypedDict):
    """Keyword arguments of `generate_presigned_url` as used here."""

    ClientMethod: str
    Params: dict[str, Any]
    ExpiresIn: int


class S3(ObjectLister, Protocol):
    """The S3 client methods the API and commit Lambdas call."""

    def head_object(self, **kwargs: Unpack[HeadObjectArgs]) -> dict[str, Any]:
        """Read object metadata."""
        ...

    def copy_object(self, **kwargs: Unpack[CopyObjectArgs]) -> dict[str, Any]:
        """Copy one object."""
        ...

    def put_object(self, **kwargs: Unpack[PutObjectArgs]) -> dict[str, Any]:
        """Write one object."""
        ...

    def get_object(self, **kwargs: Unpack[GetObjectArgs]) -> dict[str, Any]:
        """Read a byte range of one object version."""
        ...

    def delete_object(self, **kwargs: Unpack[DeleteObjectArgs]) -> dict[str, Any]:
        """Delete one object version."""
        ...

    def put_object_retention(
        self, **kwargs: Unpack[PutObjectRetentionArgs]
    ) -> dict[str, Any]:
        """Set governance retention on one object version."""
        ...

    def generate_presigned_url(self, **kwargs: Unpack[PresignArgs]) -> str:
        """Presign a request."""
        ...


class SendMessageArgs(TypedDict):
    """Keyword arguments of `SQS.SendMessage` as used here (FIFO queues)."""

    QueueUrl: str
    MessageBody: str
    MessageGroupId: str
    MessageDeduplicationId: str


class QueueAttributesArgs(TypedDict):
    """Keyword arguments of `SQS.GetQueueAttributes` as used here."""

    QueueUrl: str
    AttributeNames: list[str]


class Sqs(Protocol):
    """The SQS client methods the Lambdas and `make reclaim` call."""

    def send_message(self, **kwargs: Unpack[SendMessageArgs]) -> dict[str, Any]:
        """Send one message."""
        ...

    def get_queue_attributes(
        self, **kwargs: Unpack[QueueAttributesArgs]
    ) -> dict[str, Any]:
        """Read queue attributes."""
        ...


class StartQueryArgs(TypedDict):
    """Keyword arguments of `Athena.StartQueryExecution` as used here."""

    QueryString: str
    WorkGroup: str
    QueryExecutionContext: dict[str, str]
    ExecutionParameters: NotRequired[list[str]]


class QueryIdArgs(TypedDict):
    """Keyword arguments of `Athena.GetQueryExecution` and `StopQueryExecution`."""

    QueryExecutionId: str


class QueryResultsArgs(TypedDict):
    """Keyword arguments of `Athena.GetQueryResults` as used here."""

    QueryExecutionId: str
    NextToken: NotRequired[str]


class Athena(Protocol):
    """The Athena client methods the catalog Lambda calls."""

    def start_query_execution(self, **kwargs: Unpack[StartQueryArgs]) -> dict[str, Any]:
        """Start one query."""
        ...

    def get_query_execution(self, **kwargs: Unpack[QueryIdArgs]) -> dict[str, Any]:
        """Read a query's state."""
        ...

    def get_query_results(self, **kwargs: Unpack[QueryResultsArgs]) -> dict[str, Any]:
        """Read one page of a finished query's rows."""
        ...

    def stop_query_execution(self, **kwargs: Unpack[QueryIdArgs]) -> dict[str, Any]:
        """Stop a running query."""
        ...


class GetTableArgs(TypedDict):
    """Keyword arguments of `Glue.GetTable` as used here (the label provenance read)."""

    DatabaseName: str
    Name: str


class Glue(Protocol):
    """The one Glue client method the labels Lambda calls."""

    def get_table(self, **kwargs: Unpack[GetTableArgs]) -> dict[str, Any]:
        """Read one table's definition, with its `Parameters`."""
        ...


class ObjectReader(Protocol):
    """The one S3 method the labels Lambda calls (the Iceberg metadata JSON)."""

    def get_object(self, **kwargs: Unpack[GetObjectArgs]) -> dict[str, Any]:
        """Read one object; the reply's `Body` has `.read()`."""
        ...


class RunsTable(Protocol):
    """The one DynamoDB method the import planner calls on the runs table."""

    def update_item(self, **kwargs: Unpack[UpdateItemArgs]) -> dict[str, Any]:
        """Update (or create) one run item."""
        ...


class InvokeArgs(TypedDict):
    """Keyword arguments of `Lambda.Invoke` as used here (asynchronous only)."""

    FunctionName: str
    InvocationType: str
    Payload: bytes


class Invoker(Protocol):
    """The one Lambda method the import planner calls."""

    def invoke(self, **kwargs: Unpack[InvokeArgs]) -> dict[str, Any]:
        """Invoke a function."""
        ...


class BatchGetItemArgs(TypedDict):
    """Keyword arguments of `DynamoDB.BatchGetItem` as used here."""

    RequestItems: dict[str, Any]


class ImportsStore(Protocol):
    """The DynamoDB methods ImportsFunction calls (runs and sessions tables)."""

    def get_item(self, **kwargs: Unpack[GetItemArgs]) -> dict[str, Any]:
        """Read one item."""
        ...

    def put_item(self, **kwargs: Unpack[PutItemArgs]) -> dict[str, Any]:
        """Write one item."""
        ...

    def query(self, **kwargs: Unpack[QueryArgs]) -> dict[str, Any]:
        """Read items by key condition."""
        ...

    def batch_get_item(self, **kwargs: Unpack[BatchGetItemArgs]) -> dict[str, Any]:
        """Read up to 100 items by key."""
        ...
