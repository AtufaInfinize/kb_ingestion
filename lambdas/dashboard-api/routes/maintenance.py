"""Maintenance endpoints — DLQ inspection."""

import os
import json
import logging

import boto3
from fastapi import APIRouter

from utils.response import api_response

logger = logging.getLogger(__name__)
router = APIRouter()

sqs = boto3.client('sqs')

# DLQ queue URLs injected via Lambda env vars
_DLQ_CONFIGS = [
    ("crawl-dlq",           os.environ.get("CRAWL_DLQ_URL", "")),
    ("processing-dlq",      os.environ.get("PROCESSING_DLQ_URL", "")),
    ("pdf-processing-dlq",  os.environ.get("PDF_PROCESSING_DLQ_URL", "")),
]


@router.get("/universities/{uid}/dlq")
def get_dlq_report(uid: str):
    """GET /v1/universities/{uid}/dlq — Peek at DLQ messages (non-destructive).

    Returns up to 10 messages per DLQ with VisibilityTimeout=0 so they
    immediately reappear in the queue after being sampled.
    """
    queues = []

    for name, queue_url in _DLQ_CONFIGS:
        if not queue_url:
            queues.append({"name": name, "depth": 0, "messages": [], "error": "Queue URL not configured"})
            continue

        try:
            # Get approximate depth
            attrs = sqs.get_queue_attributes(
                QueueUrl=queue_url,
                AttributeNames=["ApproximateNumberOfMessages"],
            )
            depth = int(attrs["Attributes"].get("ApproximateNumberOfMessages", 0))

            # Peek at up to 10 messages — VisibilityTimeout=0 means they
            # become visible again immediately and are NOT consumed.
            messages = []
            if depth > 0:
                resp = sqs.receive_message(
                    QueueUrl=queue_url,
                    MaxNumberOfMessages=10,
                    VisibilityTimeout=0,
                    AttributeNames=["ApproximateReceiveCount", "SentTimestamp"],
                )
                for msg in resp.get("Messages", []):
                    parsed = {}
                    try:
                        parsed = json.loads(msg.get("Body", "{}"))
                    except Exception:
                        pass

                    messages.append({
                        "url":           parsed.get("url", parsed.get("s3_key", "")),
                        "university_id": parsed.get("university_id", uid),
                        "receive_count": int(msg.get("Attributes", {}).get("ApproximateReceiveCount", 0)),
                        "sent_at":       msg.get("Attributes", {}).get("SentTimestamp", ""),
                        "body_preview":  msg.get("Body", "")[:200],
                    })

            queues.append({"name": name, "depth": depth, "messages": messages})

        except Exception as e:
            logger.warning(f"DLQ inspect failed for {name}: {e}")
            queues.append({"name": name, "depth": -1, "messages": [], "error": str(e)})

    total_failed = sum(max(q["depth"], 0) for q in queues)
    return api_response(200, {
        "university_id": uid,
        "total_failed":  total_failed,
        "queues":        queues,
    })
