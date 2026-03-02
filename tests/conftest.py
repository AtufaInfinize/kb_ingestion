"""
Pytest fixtures: moto-mocked AWS resources (DynamoDB, S3, SQS).

Each test gets fresh, isolated AWS mocks — no real AWS calls are made.
"""

import os
import json
import hashlib
import pytest
import boto3
from moto import mock_aws


# ─── Constants used across tests ───────────────────────────────────────────────
BUCKET = "university-kb-content-test"
TABLE  = "url-registry-dev"
REGION = "us-east-1"
UID    = "testuni"


def _url_hash(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:16]


# ─── Core moto fixture ─────────────────────────────────────────────────────────

@pytest.fixture(scope="function")
def aws_mocks():
    """Activate moto mocks for DynamoDB, S3, SQS for the duration of a test."""
    with mock_aws():
        yield


@pytest.fixture(scope="function")
def dynamo_table(aws_mocks):
    """Create the url-registry DynamoDB table with correct keys and GSI."""
    ddb = boto3.resource("dynamodb", region_name=REGION)
    table = ddb.create_table(
        TableName=TABLE,
        KeySchema=[{"AttributeName": "url", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "url",           "AttributeType": "S"},
            {"AttributeName": "university_id", "AttributeType": "S"},
            {"AttributeName": "page_category", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
        GlobalSecondaryIndexes=[
            {
                "IndexName": "university-category-index",
                "KeySchema": [
                    {"AttributeName": "university_id", "KeyType": "HASH"},
                    {"AttributeName": "page_category",  "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
        ],
    )
    table.wait_until_exists()
    return table


@pytest.fixture(scope="function")
def s3_bucket(aws_mocks):
    """Create the content S3 bucket."""
    s3 = boto3.client("s3", region_name=REGION)
    s3.create_bucket(Bucket=BUCKET)
    return s3


@pytest.fixture(scope="function")
def sqs_queues(aws_mocks):
    """Create stub SQS queues (FIFO for crawl queue)."""
    sqs = boto3.client("sqs", region_name=REGION)
    crawl_q = sqs.create_queue(
        QueueName="crawl-queue-test.fifo",
        Attributes={"FifoQueue": "true", "ContentBasedDeduplication": "true"},
    )
    return {
        "crawl": crawl_q["QueueUrl"],
    }


@pytest.fixture(scope="function", autouse=True)
def set_env_vars(dynamo_table, s3_bucket, sqs_queues):
    """Inject environment variables so route modules pick up mocked resources."""
    os.environ["CONTENT_BUCKET"]      = BUCKET
    os.environ["URL_REGISTRY_TABLE"]  = TABLE
    os.environ["AWS_DEFAULT_REGION"]  = REGION
    os.environ["CRAWL_QUEUE_URL"]     = sqs_queues["crawl"]
    os.environ["PROCESSING_QUEUE_URL"] = sqs_queues["crawl"]   # stub
    os.environ["PDF_PROCESSING_QUEUE_URL"] = sqs_queues["crawl"]  # stub
    yield
    for k in ("CONTENT_BUCKET", "URL_REGISTRY_TABLE", "AWS_DEFAULT_REGION",
              "CRAWL_QUEUE_URL", "PROCESSING_QUEUE_URL", "PDF_PROCESSING_QUEUE_URL"):
        os.environ.pop(k, None)


# ─── Seed helpers ──────────────────────────────────────────────────────────────

def seed_url(table, url: str, category: str = "admissions", **extra):
    """Insert a URL item into the mocked DynamoDB table."""
    from urllib.parse import urlparse
    parsed = urlparse(url)
    item = {
        "url":           url,
        "url_hash":      _url_hash(url),
        "university_id": UID,
        "domain":        parsed.netloc,
        "crawl_status":  "crawled",
        "page_category": category,
        **extra,
    }
    table.put_item(Item=item)
    return item


def seed_s3_objects(s3_client, url: str):
    """Put stub .md and .metadata.json objects for a URL into the mocked S3 bucket."""
    from urllib.parse import urlparse
    parsed  = urlparse(url)
    domain  = parsed.netloc
    h       = _url_hash(url)
    prefix  = f"clean-content/{UID}/{domain}/{h}"

    s3_client.put_object(Bucket=BUCKET, Key=f"{prefix}.md",               Body=b"# Test content")
    s3_client.put_object(Bucket=BUCKET, Key=f"{prefix}.md.metadata.json", Body=b'{"category":"admissions"}')
    return prefix
