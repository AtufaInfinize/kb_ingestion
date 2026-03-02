"""
Crawl Orchestrator Lambda

Handles Step Functions tasks:
- validate_request: Validate and parse the refresh request
- queue_stale_urls: Find URLs past their freshness window and re-queue them
- check_progress: Monitor crawl completion by checking queue depth
- generate_summary: Create a summary of what was crawled

Triggered by: Step Functions state machine
"""

import os
import json
import logging
import time
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ─────────────────────────────────────────────
# AWS Clients
# ─────────────────────────────────────────────
sqs = boto3.client("sqs")
dynamodb = boto3.resource("dynamodb")
s3 = boto3.client("s3")
lambda_client = boto3.client("lambda")

# ─────────────────────────────────────────────
# Environment Variables
# ─────────────────────────────────────────────
CRAWL_QUEUE_URL = os.environ["CRAWL_QUEUE_URL"]
PROCESSING_QUEUE_URL = os.environ.get("PROCESSING_QUEUE_URL", "")
URL_REGISTRY_TABLE = os.environ["URL_REGISTRY_TABLE"]
CONTENT_BUCKET = os.environ["CONTENT_BUCKET"]
ROBOTS_CACHE_TABLE = os.environ["ROBOTS_CACHE_TABLE"]
BATCH_CLASSIFIER_FUNCTION = os.environ.get("BATCH_CLASSIFIER_FUNCTION", "")
PIPELINE_JOBS_TABLE = os.environ.get("PIPELINE_JOBS_TABLE", "")

ENTITY_STORE_TABLE = os.environ.get("ENTITY_STORE_TABLE", "entity-store-dev")

url_table    = dynamodb.Table(URL_REGISTRY_TABLE)
jobs_table   = dynamodb.Table(PIPELINE_JOBS_TABLE) if PIPELINE_JOBS_TABLE else None
entity_table = dynamodb.Table(ENTITY_STORE_TABLE)

# Default freshness windows (used if config doesn't specify)
DEFAULT_FRESHNESS_DAYS = {
    "admissions": 7,
    "financial_aid": 7,
    "scholarships": 7,
    "academic_programs": 14,
    "course_catalog": 14,
    "student_services": 14,
    "housing_dining": 14,
    "campus_life": 30,
    "spiritual_life": 30,
    "athletics": 7,
    "faculty_staff": 30,
    "library": 30,
    "distance_learning": 14,
    "career_services": 30,
    "alumni": 30,
    "about": 60,
    "policies": 60,
    "accreditation": 60,
    "events": 3,
    "news": 3,
    "campus_safety": 14,
    "international_students": 14,
    "student_organizations": 30,
    "commencement": 14,
    "orientation": 14,
    "registration": 7,
    "academic_calendar": 7,
    "tuition_fees": 14,
    "unknown": 14,
    "other": 30,
}


# ═════════════════════════════════════════════
# MAIN HANDLER — Routes to task functions
# ═════════════════════════════════════════════
def lambda_handler(event, context):
    """
    Route to the appropriate task based on event.task field.
    Step Functions passes different payloads for each step.
    """
    task = event.get("task", "")

    logger.info(f"Orchestrator task: {task}")

    if task == "validate_request":
        return validate_request(event.get("input", {}))
    elif task == "queue_stale_urls":
        return queue_stale_urls(event)
    elif task == "check_progress":
        return check_progress(event)
    elif task == "trigger_classification":
        return trigger_classification(event)
    elif task == "generate_summary":
        return generate_summary(event)
    else:
        logger.error(f"Unknown task: {task}")
        raise ValueError(f"Unknown orchestrator task: {task}")


# ═════════════════════════════════════════════
# TASK: Validate Request
# ═════════════════════════════════════════════
def validate_request(input_data):
    """
    Validate the crawl refresh request.
    Called as the first step in the state machine.

    Checks:
    - university_id exists and has a config
    - refresh_mode is valid
    - domain is provided for domain-specific refresh
    """
    university_id = input_data.get("university_id", "")
    refresh_mode = input_data.get("refresh_mode", "incremental")
    domain = input_data.get("domain", "")

    # Validate university_id
    if not university_id:
        logger.error("No university_id provided")
        return {
            "is_valid": "false",
            "error": "university_id is required",
            "university_config": "{}",
            "refresh_mode": refresh_mode,
            "domain": domain,
        }

    # Try to load config
    config = load_config(university_id)
    if not config:
        logger.error(f"No config found for {university_id}")
        return {
            "is_valid": "false",
            "error": f"No configuration found for: {university_id}",
            "university_config": "{}",
            "refresh_mode": refresh_mode,
            "domain": domain,
        }

    # Validate refresh_mode
    if refresh_mode not in ("full", "incremental", "domain"):
        return {
            "is_valid": "false",
            "error": f"Invalid refresh_mode: {refresh_mode}",
            "university_config": json.dumps(config),
            "refresh_mode": refresh_mode,
            "domain": domain,
        }

    # Validate domain for domain-specific refresh
    if refresh_mode == "domain" and not domain:
        return {
            "is_valid": "false",
            "error": "domain is required for domain refresh mode",
            "university_config": json.dumps(config),
            "refresh_mode": refresh_mode,
            "domain": domain,
        }

    logger.info(f"Validated: university={university_id}, mode={refresh_mode}")

    # Persist crawl mode so content-cleaner knows whether to skip classification
    now = datetime.now(timezone.utc).isoformat()
    try:
        entity_table.put_item(Item={
            "university_id": university_id,
            "entity_key": "current_crawl_mode",
            "entity_type": "crawl_state",
            "refresh_mode": refresh_mode,
            "started_at": now,
        })
        # Reset the pages-changed counter for this run
        entity_table.update_item(
            Key={"university_id": university_id, "entity_key": "kb_sync_status"},
            UpdateExpression=(
                "SET #st = :running, pages_changed = :zero, entity_type = :et, "
                "crawl_started_at = :now"
            ),
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={
                ":running": "running",
                ":zero": 0,
                ":et": "crawl_state",
                ":now": now,
            },
        )
    except Exception as e:
        logger.warning(f"Could not persist crawl mode to entity-store: {e}")

    return {
        "is_valid": "true",
        "university_config": json.dumps(config),
        "refresh_mode": refresh_mode,
        "domain": domain,
    }


# ═════════════════════════════════════════════
# TASK: Queue Stale URLs
# ═════════════════════════════════════════════
def queue_stale_urls(event):
    """
    For incremental refresh: scan the URL registry,
    find URLs past their freshness window based on their
    page_category, and push them back to the crawl queue.

    This is where the per-category freshness windows come into play.
    A page categorized as 'admissions' with a 7-day window that was
    last crawled 8 days ago gets re-queued. A page categorized as
    'policies' with a 30-day window crawled 15 days ago gets skipped.
    """
    university_id = event.get("university_id", "")
    refresh_mode = event.get("refresh_mode", "incremental")
    domain_filter = event.get("domain_filter", "")

    # Always load S3 config (needed for crawl settings like max_crawl_depth)
    config = load_config(university_id)

    # Load freshness windows — entity-store (frontend-configured) takes priority,
    # fall back to S3 config freshness_windows_days, then to hardcoded defaults.
    entity_windows = _load_freshness_from_entity_store(university_id)
    freshness_windows = DEFAULT_FRESHNESS_DAYS.copy()
    if entity_windows:
        freshness_windows.update(entity_windows)
    elif config:
        freshness_windows.update(config.get("freshness_windows_days", {}))

    # Crawl depth comes from S3 config only
    crawl_config = config.get("crawl_config", {}) if config else {}
    max_crawl_depth = crawl_config.get("max_crawl_depth", 3)

    now = datetime.now(timezone.utc)
    urls_queued = 0

    if refresh_mode == "full":
        # Full refresh: re-queue ALL crawled URLs
        urls_queued = queue_all_urls(university_id, domain_filter, max_crawl_depth)
    else:
        # Incremental: only queue stale URLs
        urls_queued = queue_stale(
            university_id, domain_filter, freshness_windows, now, max_crawl_depth
        )

    logger.info(f"Queued {urls_queued} stale URLs for {university_id}")

    return {"urls_queued": urls_queued}


def queue_all_urls(university_id, domain_filter, max_crawl_depth):
    """Queue all known URLs for a full re-crawl."""
    urls_queued = 0

    # Scan using the university-status-index GSI for 'crawled' URLs
    scan_kwargs = {
        "IndexName": "university-status-index",
        "KeyConditionExpression": "university_id = :uid AND crawl_status = :status",
        "ExpressionAttributeValues": {
            ":uid": university_id,
            ":status": "crawled",
        },
        "ProjectionExpression": "#u, #d, #dep",
        "ExpressionAttributeNames": {
            "#u": "url",
            "#d": "domain",
            "#dep": "depth",
        },
    }

    done = False
    while not done:
        result = url_table.query(**scan_kwargs)

        for item in result.get("Items", []):
            url = item["url"]
            domain = item.get("domain", "")
            depth = item.get("depth", 0)

            # Apply domain filter
            if domain_filter and domain_filter not in domain:
                continue

            # Reset status to pending and re-queue
            mark_pending(url)
            push_to_queue(url, university_id, domain, depth, max_crawl_depth)
            urls_queued += 1

        # Handle pagination
        if "LastEvaluatedKey" in result:
            scan_kwargs["ExclusiveStartKey"] = result["LastEvaluatedKey"]
        else:
            done = True

    return urls_queued


def queue_stale(university_id, domain_filter, freshness_windows, now, max_crawl_depth):
    """Queue only URLs that are past their freshness window."""
    urls_queued = 0

    # Query all crawled URLs for this university
    query_kwargs = {
        "IndexName": "university-status-index",
        "KeyConditionExpression": "university_id = :uid AND crawl_status = :status",
        "ExpressionAttributeValues": {
            ":uid": university_id,
            ":status": "crawled",
        },
        "ProjectionExpression": "#u, #d, page_category, last_crawled_at, #dep",
        "ExpressionAttributeNames": {
            "#u": "url",
            "#d": "domain",
            "#dep": "depth",
        },
    }

    done = False
    while not done:
        result = url_table.query(**query_kwargs)

        for item in result.get("Items", []):
            url = item["url"]
            domain = item.get("domain", "")
            category = item.get("page_category", "unknown")
            last_crawled = item.get("last_crawled_at", "never")
            depth = item.get("depth", 0)

            # Apply domain filter
            if domain_filter and domain_filter not in domain:
                continue

            # Skip if never crawled (shouldn't happen for 'crawled' status, but safety check)
            if last_crawled == "never":
                continue

            # Calculate staleness
            try:
                last_crawled_dt = datetime.fromisoformat(last_crawled)
            except ValueError:
                continue

            # Get freshness window for this category
            freshness_days = freshness_windows.get(category, freshness_windows.get("unknown", 14))
            cutoff = now - timedelta(days=freshness_days)

            # If last crawled before the cutoff, it's stale
            if last_crawled_dt < cutoff:
                mark_pending(url)
                push_to_queue(url, university_id, domain, depth, max_crawl_depth)
                urls_queued += 1

        # Handle pagination
        if "LastEvaluatedKey" in result:
            query_kwargs["ExclusiveStartKey"] = result["LastEvaluatedKey"]
        else:
            done = True

    return urls_queued


def mark_pending(url):
    """Reset a URL's status to pending for re-crawl."""
    try:
        url_table.update_item(
            Key={"url": url},
            UpdateExpression="SET crawl_status = :status",
            ExpressionAttributeValues={":status": "pending"},
        )
    except Exception as e:
        logger.error(f"Failed to mark pending: {url}: {e}")

def push_to_queue(url, university_id, domain, depth, max_crawl_depth):
    """Push a URL to the crawl queue for re-crawling."""
    try:
        parsed = urlparse(url)
        path_parts = parsed.path.strip("/").split("/")
        prefix = path_parts[0] if path_parts and path_parts[0] else "root"
        message_group = f"{domain}#{prefix}"[:128]

        sqs.send_message(
            QueueUrl=CRAWL_QUEUE_URL,
            MessageBody=json.dumps({
                "url": url,
                "university_id": university_id,
                "domain": domain,
                "queued_at": datetime.now(timezone.utc).isoformat(),
                "depth": depth,
                "max_crawl_depth": max_crawl_depth,
            }),
            MessageGroupId=message_group,
        )
    except Exception as e:
        logger.error(f"Failed to queue {url}: {e}")

# ═════════════════════════════════════════════
# TASK: Check Progress
# ═════════════════════════════════════════════
def check_progress(event):
    """
    Check if the crawl is complete by looking at:
    1. SQS queue depth (approximate number of messages)
    2. DynamoDB count of pending URLs

    The crawl is "complete" when both the queue is empty
    and no URLs are in pending status.
    """
    university_id = event.get("university_id", "")

    # Check SQS queue depth
    try:
        queue_attrs = sqs.get_queue_attributes(
            QueueUrl=CRAWL_QUEUE_URL,
            AttributeNames=[
                "ApproximateNumberOfMessages",
                "ApproximateNumberOfMessagesNotVisible",
            ],
        )
        attrs = queue_attrs.get("Attributes", {})
        messages_available = int(attrs.get("ApproximateNumberOfMessages", 0))
        messages_in_flight = int(attrs.get("ApproximateNumberOfMessagesNotVisible", 0))
        total_in_queue = messages_available + messages_in_flight
    except Exception as e:
        logger.warning(f"Failed to check queue depth: {e}")
        total_in_queue = -1  # Unknown

    # Count pending URLs in DynamoDB
    pending_count = 0
    try:
        result = url_table.query(
            IndexName="university-status-index",
            KeyConditionExpression="university_id = :uid AND crawl_status = :status",
            ExpressionAttributeValues={
                ":uid": university_id,
                ":status": "pending",
            },
            Select="COUNT",
        )
        pending_count = result.get("Count", 0)
    except Exception as e:
        logger.warning(f"Failed to count pending URLs: {e}")

    # Count crawled URLs
    crawled_count = 0
    try:
        result = url_table.query(
            IndexName="university-status-index",
            KeyConditionExpression="university_id = :uid AND crawl_status = :status",
            ExpressionAttributeValues={
                ":uid": university_id,
                ":status": "crawled",
            },
            Select="COUNT",
        )
        crawled_count = result.get("Count", 0)
    except Exception as e:
        logger.warning(f"Failed to count crawled URLs: {e}")

    # Count failed URLs
    failed_count = 0
    for status in ["error", "failed"]:
        try:
            result = url_table.query(
                IndexName="university-status-index",
                KeyConditionExpression="university_id = :uid AND crawl_status = :status",
                ExpressionAttributeValues={
                    ":uid": university_id,
                    ":status": status,
                },
                Select="COUNT",
            )
            failed_count += result.get("Count", 0)
        except Exception:
            pass

    # If crawl queue is fully drained but some URLs are still 'pending',
    # those messages ended up in the crawl DLQ (or were lost). Mark them
    # as 'error' so the pipeline isn't blocked forever.
    orphaned_count = 0
    if total_in_queue == 0 and pending_count > 0:
        orphaned_count = _mark_orphaned_pending_urls(university_id)
        if orphaned_count:
            logger.warning(f"Marked {orphaned_count} orphaned pending URLs as error for {university_id}")
            pending_count = 0

    # Check processing queue depth (content cleaning)
    processing_in_queue = 0
    if PROCESSING_QUEUE_URL:
        try:
            pq_attrs = sqs.get_queue_attributes(
                QueueUrl=PROCESSING_QUEUE_URL,
                AttributeNames=[
                    "ApproximateNumberOfMessages",
                    "ApproximateNumberOfMessagesNotVisible",
                ],
            )
            pq = pq_attrs.get("Attributes", {})
            processing_in_queue = (
                int(pq.get("ApproximateNumberOfMessages", 0))
                + int(pq.get("ApproximateNumberOfMessagesNotVisible", 0))
            )
        except Exception as e:
            logger.warning(f"Failed to check processing queue depth: {e}")
            processing_in_queue = -1  # Unknown — don't declare complete

    # Pipeline is complete when crawl queue, pending URLs, AND processing
    # queue are all empty (crawl done + cleaning done)
    is_complete = (
        total_in_queue == 0
        and pending_count == 0
        and processing_in_queue == 0
    )

    logger.info(
        f"Progress: crawl_queue={total_in_queue}, pending={pending_count}, "
        f"crawled={crawled_count}, failed={failed_count}, "
        f"processing_queue={processing_in_queue}, orphaned={orphaned_count}, "
        f"complete={is_complete}"
    )

    return {
        "is_complete": "true" if is_complete else "false",
        "pages_crawled": crawled_count,
        "pages_remaining": pending_count + total_in_queue,
        "pages_failed": failed_count,
        "crawl_queue_depth": total_in_queue,
        "processing_queue_depth": processing_in_queue,
    }


def _mark_orphaned_pending_urls(university_id):
    """Mark pending URLs as error when the crawl queue is fully drained.

    This handles the case where a URL's SQS message was sent to the crawl DLQ
    (after maxReceiveCount failures) but the URL's crawl_status was never
    updated from 'pending'. Without this, check_progress would loop forever.
    """
    marked = 0
    kwargs = {
        "IndexName": "university-status-index",
        "KeyConditionExpression": "university_id = :uid AND crawl_status = :status",
        "ExpressionAttributeValues": {":uid": university_id, ":status": "pending"},
        "ProjectionExpression": "#u",
        "ExpressionAttributeNames": {"#u": "url"},
    }
    while True:
        result = url_table.query(**kwargs)
        for item in result.get("Items", []):
            try:
                url_table.update_item(
                    Key={"url": item["url"]},
                    UpdateExpression="SET crawl_status = :s",
                    ExpressionAttributeValues={":s": "error"},
                )
                marked += 1
            except Exception as e:
                logger.warning(f"Failed to mark orphaned URL {item['url']}: {e}")
        if "LastEvaluatedKey" not in result:
            break
        kwargs["ExclusiveStartKey"] = result["LastEvaluatedKey"]
    return marked


# ═════════════════════════════════════════════
# TASK: Trigger Classification
# ═════════════════════════════════════════════
def trigger_classification(event):
    """
    Invoke batch-classifier-prepare Lambda to start page classification.
    Called after crawl + content cleaning are both complete.
    Also updates the pipeline-jobs record to mark classify stage as running.
    """
    university_id = event.get("university_id", "")
    job_id = event.get("job_id", "")

    if not BATCH_CLASSIFIER_FUNCTION:
        logger.warning("BATCH_CLASSIFIER_FUNCTION not configured — skipping")
        return {"triggered": False, "reason": "not configured"}

    try:
        payload = json.dumps({
            "university_id": university_id,
            "triggered_by": f"pipeline:{job_id}",
        })
        resp = lambda_client.invoke(
            FunctionName=BATCH_CLASSIFIER_FUNCTION,
            InvocationType="RequestResponse",
            Payload=payload,
        )
        result = json.loads(resp["Payload"].read().decode("utf-8"))
        logger.info(f"Classification triggered for {university_id}: {result}")
    except Exception as e:
        logger.error(f"Failed to trigger classification: {e}", exc_info=True)
        return {"triggered": False, "error": str(e)}

    # Extract batch job info from prepare Lambda response
    batch_jobs = []
    for j in result.get("jobs", []):
        entry = {"job_name": j.get("job_name", "")}
        if j.get("job_arn"):
            entry["job_arn"] = j["job_arn"]
        batch_jobs.append(entry)

    # Update pipeline-jobs record with batch job names
    if jobs_table and job_id:
        now = datetime.now(timezone.utc).isoformat()
        try:
            jobs_table.update_item(
                Key={"job_id": job_id},
                UpdateExpression="SET classify_stage = :cs",
                ExpressionAttributeValues={
                    ":cs": {
                        "status": "running",
                        "started_at": now,
                        "classify_triggered": True,
                        "batch_jobs": batch_jobs,
                    },
                },
            )
        except Exception as e:
            logger.warning(f"Failed to update pipeline-jobs: {e}")

    return {"triggered": True, "batch_jobs": batch_jobs}


# ═════════════════════════════════════════════
# TASK: Generate Summary
# ═════════════════════════════════════════════
def generate_summary(event):
    """
    Generate a final crawl summary after completion.
    Counts URLs by status and content type,
    and stores the summary in S3 for reference.
    """
    university_id = event.get("university_id", "")
    now = datetime.now(timezone.utc)

    # Gather statistics
    status_counts = {}
    for status in ["crawled", "pending", "error", "failed", "dead",
                    "redirected", "blocked_robots", "skipped_depth"]:
        try:
            result = url_table.query(
                IndexName="university-status-index",
                KeyConditionExpression="university_id = :uid AND crawl_status = :status",
                ExpressionAttributeValues={
                    ":uid": university_id,
                    ":status": status,
                },
                Select="COUNT",
            )
            status_counts[status] = result.get("Count", 0)
        except Exception:
            status_counts[status] = 0

    total_urls = sum(status_counts.values())

    # Count by content type (sample — full count would require scan)
    summary = {
        "university_id": university_id,
        "completed_at": now.isoformat(),
        "total_urls_discovered": total_urls,
        "urls_by_status": status_counts,
        "success_rate": round(
            status_counts.get("crawled", 0) / max(total_urls, 1) * 100, 1
        ),
    }

    # Store summary in S3
    try:
        summary_key = f"crawl-summaries/{university_id}/{now.strftime('%Y%m%d-%H%M%S')}.json"
        s3.put_object(
            Bucket=CONTENT_BUCKET,
            Key=summary_key,
            Body=json.dumps(summary, indent=2),
            ContentType="application/json",
        )
        summary["s3_summary_key"] = summary_key
        logger.info(f"Summary stored at s3://{CONTENT_BUCKET}/{summary_key}")
    except Exception as e:
        logger.warning(f"Failed to store summary in S3: {e}")

    # Read pages_changed counter written by content-cleaner, then write final kb_sync_status
    pages_changed = 0
    try:
        ks_resp = entity_table.get_item(
            Key={"university_id": university_id, "entity_key": "kb_sync_status"}
        )
        ks_item = ks_resp.get("Item", {})
        pages_changed = int(ks_item.get("pages_changed", 0))
    except Exception as e:
        logger.warning(f"Could not read pages_changed from entity-store: {e}")

    try:
        completed_at = now.isoformat()
        new_status = "pending_sync" if pages_changed > 0 else "no_changes"
        entity_table.update_item(
            Key={"university_id": university_id, "entity_key": "kb_sync_status"},
            UpdateExpression=(
                "SET #st = :s, pages_changed = :n, crawl_completed_at = :ts, entity_type = :et"
            ),
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={
                ":s": new_status,
                ":n": pages_changed,
                ":ts": completed_at,
                ":et": "crawl_state",
            },
        )
        logger.info(f"kb_sync_status → {new_status}, pages_changed={pages_changed}")
    except Exception as e:
        logger.warning(f"Could not write kb_sync_status to entity-store: {e}")

    summary["pages_changed"] = pages_changed

    logger.info(f"Crawl summary: {json.dumps(summary)}")

    return summary


# ═════════════════════════════════════════════
# CONFIG LOADING
# ═════════════════════════════════════════════
def load_config(university_id):
    """Load university config from S3."""
    try:
        response = s3.get_object(
            Bucket=CONTENT_BUCKET,
            Key=f"configs/{university_id}.json"
        )
        return json.loads(response["Body"].read().decode("utf-8"))
    except Exception:
        logger.warning(f"Could not load config for {university_id}")
        return None


def _load_freshness_from_entity_store(university_id: str) -> dict:
    """Load per-category freshness windows from entity-store-dev (set via dashboard).

    Returns the stored windows dict, or an empty dict if nothing has been saved yet.
    An empty return tells the caller to fall back to S3 config or hardcoded defaults.
    """
    try:
        resp = entity_table.get_item(
            Key={"university_id": university_id, "entity_key": "freshness_config"}
        )
        item = resp.get("Item")
        if item and item.get("windows"):
            return {k: int(v) for k, v in item["windows"].items()}
    except Exception as e:
        logger.warning(f"Could not read freshness from entity-store for {university_id}: {e}")
    return {}
