"""WebHosting: site bucket and CloudFront distribution."""

from aws_cdk import RemovalPolicy
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_iam as iam
from aws_cdk import aws_s3 as s3
from constructs import Construct

SITE_PREFIX = "site"
HANDOUT_PREFIX = "handout"


class WebHosting(Construct):
    """Private site bucket served only through CloudFront from `site/`.

    `handout/` (wheel, notebook, manifest) sits beside `site/`. CDK's OAC grant
    covers every object key of the bucket (`arnForObjects("*")`), so an explicit
    Deny keeps CloudFront from ever serving `handout/*`.
    """

    def __init__(self, scope: Construct, construct_id: str) -> None:
        """Create the bucket, the distribution and the handout Deny."""
        super().__init__(scope, construct_id)
        self.bucket = s3.Bucket(
            self,
            "SiteBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.RETAIN,
        )
        self.distribution = cloudfront.Distribution(
            self,
            "Distribution",
            default_root_object="index.html",
            price_class=cloudfront.PriceClass.PRICE_CLASS_100,
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(
                    self.bucket,
                    origin_path=f"/{SITE_PREFIX}",
                ),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                response_headers_policy=cloudfront.ResponseHeadersPolicy.SECURITY_HEADERS,
            ),
            error_responses=[
                cloudfront.ErrorResponse(
                    http_status=status,
                    response_http_status=200,
                    response_page_path="/index.html",
                )
                for status in (403, 404)
            ],
        )
        deny = iam.PolicyStatement(
            effect=iam.Effect.DENY,
            actions=["s3:GetObject"],
            resources=[self.bucket.arn_for_objects(f"{HANDOUT_PREFIX}/*")],
        )
        deny.add_service_principal("cloudfront.amazonaws.com")
        self.bucket.add_to_resource_policy(deny)

    @property
    def url(self) -> str:
        """Return the hosted app URL, `https://<distribution domain>`."""
        return f"https://{self.distribution.distribution_domain_name}"
