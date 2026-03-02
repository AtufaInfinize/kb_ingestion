"""
Crawl Scheduler Lambda

Triggered daily/weekly by EventBridge. Replaces the hardcoded single-university
EventBridge → Step Functions rules with per-university staleness checks.

Incremental (daily at 2 AM UTC):
  - Lists every configured university from S3
  - Skips any university that already has a running pipeline
  - Checks if any category's freshness window has expired since the last
    completed crawl (using entity-store-dev for windows, pipeline-jobs for dates)
  - Triggers the crawl state machine only for universities with stale content

Full (weekly, Sundays at 3 AM UTC):
  - Same multi-university listing, but triggers every university unconditionally
    (skips only those with a pipeline already running)

Input event: {"refresh_mode": "incremental" | "full"}
"""

import json
import logging
import os
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Attr, Key

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3         = boto3.client("s3")
sfn        = boto3.client("stepfunctions")
dynamodb   = boto3.resource("dynamodb")

CONTENT_BUCKET      = os.environ["CONTENT_BUCKET"]
STATE_MACHINE_ARN   = os.environ["STATE_MACHINE_ARN"]
PIPELINE_JOBS_TABLE = os.environ["PIPELINE_JOBS_TABLE"]
ENTITY_STORE_TABLE  = os.environ["ENTITY_STORE_TABLE"]

jobs_table   = dynamodb.Table(PIPELINE_JOBS_TABLE)
entity_table = dynamodb.Table(ENTITY_STORE_TABLE)

_FRESHNESS_ENTITY_KEY = "freshness_config"
_DEFAULT_FRESHNESS_DAYS = 1  # per-category default when nothing is saved


def lambda_handler(event, context):
    refresh_mode = event.get("refresh_mode", "incremental")
    logger.info(f"CrawlScheduler triggered: refresh_mode={refresh_mode}")

    universities = _list_universities()
    logger.info(f"Found {len(universities)} configured universities: {universities}")

    triggered, skipped = [], []

    for uid in universities:
        if _has_running_job(uid):
            logger.info(f"{uid}: pipeline already running — skipping")
            skipped.append(uid)
            continue

        sched_item = _load_freshness_item(uid)

        if refresh_mode == "full":
            if not bool(sched_item.get("full_enabled", True)):
                logger.info(f"{uid}: weekly full crawl disabled — skipping")
                skipped.append(uid)
                continue
            _start_crawl(uid, "full")
            triggered.append(uid)
        else:
            if not bool(sched_item.get("incremental_enabled", True)):
                logger.info(f"{uid}: daily incremental crawl disabled — skipping")
                skipped.append(uid)
                continue
            stale, reason = _is_stale(uid, sched_item)
            if stale:
                logger.info(f"{uid}: stale ({reason}) — triggering incremental crawl")
                _start_crawl(uid, "incremental")
                triggered.append(uid)
            else:
                logger.info(f"{uid}: fresh ({reason}) — skipping")
                skipped.append(uid)

    logger.info(f"Triggered={triggered}  Skipped={skipped}")
    return {"refresh_mode": refresh_mode, "triggered": triggered, "skipped": skipped}


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def _list_universities() -> list[str]:
    """Return all university IDs that have a config JSON in S3."""
    resp = s3.list_objects_v2(Bucket=CONTENT_BUCKET, Prefix="configs/", Delimiter="/")
    ids = []
    for obj in resp.get("Contents", []):
        key = obj["Key"]
        if key.endswith(".json"):
            uid = key[len("configs/"):-len(".json")]
            if uid:
                ids.append(uid)
    return ids


def _has_running_job(uid: str) -> bool:
    """True if a pipeline job with overall_status='running' exists for this university."""
    resp = jobs_table.query(
        IndexName="university-created-index",
        KeyConditionExpression=Key("university_id").eq(uid),
        FilterExpression=Attr("overall_status").eq("running"),
        ScanIndexForward=False,
        Limit=5,
    )
    return bool(resp.get("Items"))


def _load_freshness_item(uid: str) -> dict:
    """Load the freshness_config entity for a university (empty dict if missing)."""
    try:
        resp = entity_table.get_item(
            Key={"university_id": uid, "entity_key": _FRESHNESS_ENTITY_KEY}
        )
        return resp.get("Item") or {}
    except Exception:
        return {}


def _is_stale(uid: str, item: dict | None = None) -> tuple[bool, str]:
    """Return (is_stale, reason).

    Stale = days since last completed crawl >= the minimum freshness window
    across all categories for this university.

    Pass a pre-loaded freshness item to avoid an extra DynamoDB call.
    """
    # Load per-category freshness windows; fall back to 1-day default
    if item is None:
        item = _load_freshness_item(uid)
    if item and item.get("windows"):
        min_window = min(int(v) for v in item["windows"].values())
    else:
        min_window = _DEFAULT_FRESHNESS_DAYS

    # Find the most recent completed crawl job
    resp = jobs_table.query(
        IndexName="university-created-index",
        KeyConditionExpression=Key("university_id").eq(uid),
        FilterExpression=Attr("overall_status").eq("completed"),
        ScanIndexForward=False,
        Limit=10,
    )
    items = resp.get("Items", [])
    if not items:
        return True, "never crawled"

    last_crawl_str = items[0].get("created_at", "")
    try:
        last_crawl_dt = datetime.fromisoformat(last_crawl_str.replace("Z", "+00:00"))
    except ValueError:
        return True, f"unparseable crawl date: {last_crawl_str!r}"

    days_since = (datetime.now(timezone.utc) - last_crawl_dt).days
    if days_since >= min_window:
        return True, f"{days_since}d since crawl >= {min_window}d window"
    return False, f"{days_since}d since crawl < {min_window}d window"


def _start_crawl(uid: str, mode: str):
    sfn.start_execution(
        stateMachineArn=STATE_MACHINE_ARN,
        input=json.dumps({"university_id": uid, "refresh_mode": mode}),
    )
