"""
Refresh Trigger Lambda

Handles API Gateway requests:
- POST /crawl/refresh — Start a new crawl
- GET /crawl/status/{execution_id} — Check crawl status

This is the entry point for manual and programmatic crawl triggers.
The EventBridge daily schedule bypasses this and triggers
Step Functions directly.
"""

import os
import json
import logging
from datetime import datetime, timezone

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ─────────────────────────────────────────────
# AWS Clients
# ─────────────────────────────────────────────
sfn_client = boto3.client("stepfunctions")
dynamodb = boto3.resource("dynamodb")

# ─────────────────────────────────────────────
# Environment Variables
# ─────────────────────────────────────────────
STATE_MACHINE_ARN = os.environ["STATE_MACHINE_ARN"]
URL_REGISTRY_TABLE = os.environ["URL_REGISTRY_TABLE"]

url_table = dynamodb.Table(URL_REGISTRY_TABLE)


# ═════════════════════════════════════════════
# MAIN HANDLER
# ═════════════════════════════════════════════
def lambda_handler(event, context):
    """
    Route API Gateway requests to the appropriate handler.

    POST /crawl/refresh
    Body: {
        "university_id": "phc",
        "refresh_mode": "full" | "incremental" | "domain",
        "domain": "www.phc.edu"  (required only for domain mode)
    }

    GET /crawl/status/{execution_id}
    Returns execution status and crawl statistics.
    """
    http_method = event.get("httpMethod", "")
    path = event.get("path", "")

    logger.info(f"API request: {http_method} {path}")

    try:
        if http_method == "POST" and "/refresh" in path:
            return handle_refresh(event)
        elif http_method == "GET" and "/status/" in path:
            return handle_status(event)
        else:
            return response(400, {"error": f"Unknown endpoint: {http_method} {path}"})

    except Exception as e:
        logger.error(f"Unhandled error: {e}", exc_info=True)
        return response(500, {"error": "Internal server error"})


# ═════════════════════════════════════════════
# POST /crawl/refresh
# ═════════════════════════════════════════════
def handle_refresh(event):
    """Validate request and start Step Functions execution."""

    # Parse request body
    try:
        body = json.loads(event.get("body", "{}"))
    except json.JSONDecodeError:
        return response(400, {"error": "Invalid JSON body"})

    # Validate university_id
    university_id = body.get("university_id")
    if not university_id:
        return response(400, {"error": "university_id is required"})

    # Validate refresh_mode
    refresh_mode = body.get("refresh_mode", "incremental")
    valid_modes = ("full", "incremental", "domain")
    if refresh_mode not in valid_modes:
        return response(400, {
            "error": f"refresh_mode must be one of: {', '.join(valid_modes)}"
        })

    # Validate domain for domain-specific refresh
    domain = body.get("domain", "")
    if refresh_mode == "domain" and not domain:
        return response(400, {
            "error": "domain is required when refresh_mode is 'domain'"
        })

    # Check if there's already a running crawl for this university
    running_execution = find_running_execution(university_id)
    if running_execution:
        return response(409, {
            "error": "A crawl is already running for this university",
            "running_execution": running_execution,
            "hint": "Wait for it to complete or check status at /crawl/status/{execution_id}"
        })

    # Build Step Functions input
    now = datetime.now(timezone.utc)
    execution_name = f"{university_id}-{refresh_mode}-{now.strftime('%Y%m%d-%H%M%S')}"

    sfn_input = {
        "university_id": university_id,
        "refresh_mode": refresh_mode,
        "domain": domain,
        "triggered_at": now.isoformat(),
        "triggered_by": "api",
    }

    # Start execution
    try:
        result = sfn_client.start_execution(
            stateMachineArn=STATE_MACHINE_ARN,
            name=execution_name,
            input=json.dumps(sfn_input),
        )

        execution_arn = result["executionArn"]
        # Extract just the execution name from the ARN for a cleaner ID
        execution_id = execution_arn.split(":")[-1]

        logger.info(f"Started crawl: {execution_name} (mode={refresh_mode})")

        return response(202, {
            "message": "Crawl started successfully",
            "execution_id": execution_id,
            "university_id": university_id,
            "refresh_mode": refresh_mode,
            "domain": domain if domain else None,
            "started_at": now.isoformat(),
            "check_status": f"/crawl/status/{execution_id}",
        })

    except sfn_client.exceptions.ExecutionAlreadyExists:
        return response(409, {
            "error": "An execution with this name already exists. Try again in a moment."
        })
    except Exception as e:
        logger.error(f"Failed to start execution: {e}")
        return response(500, {"error": "Failed to start crawl execution"})


# ═════════════════════════════════════════════
# GET /crawl/status/{execution_id}
# ═════════════════════════════════════════════
def handle_status(event):
    """Get status of a crawl execution with live statistics."""

    # Extract execution_id from path parameters
    path_params = event.get("pathParameters", {}) or {}
    execution_id = path_params.get("execution_id", "")

    if not execution_id:
        return response(400, {"error": "execution_id is required"})

    # Build the full execution ARN from the state machine ARN
    # State machine ARN: arn:aws:states:region:account:stateMachine:name
    # Execution ARN:     arn:aws:states:region:account:execution:name:execution_id
    execution_arn = STATE_MACHINE_ARN.replace(
        ":stateMachine:", ":execution:"
    ) + ":" + execution_id

    try:
        exec_response = sfn_client.describe_execution(
            executionArn=execution_arn
        )
    except sfn_client.exceptions.ExecutionDoesNotExist:
        return response(404, {"error": f"Execution not found: {execution_id}"})
    except Exception as e:
        logger.error(f"Failed to describe execution: {e}")
        return response(500, {"error": "Failed to fetch execution status"})

    # Parse execution details
    status = exec_response["status"]  # RUNNING, SUCCEEDED, FAILED, TIMED_OUT, ABORTED
    sfn_input = json.loads(exec_response.get("input", "{}"))
    university_id = sfn_input.get("university_id", "unknown")

    result = {
        "execution_id": execution_id,
        "status": status,
        "university_id": university_id,
        "refresh_mode": sfn_input.get("refresh_mode"),
        "started_at": exec_response["startDate"].isoformat(),
    }

    # Add completion time if finished
    if exec_response.get("stopDate"):
        result["completed_at"] = exec_response["stopDate"].isoformat()
        # Calculate duration
        duration = exec_response["stopDate"] - exec_response["startDate"]
        result["duration_seconds"] = int(duration.total_seconds())

    # Add output if available (from GenerateSummary step)
    if exec_response.get("output"):
        try:
            result["summary"] = json.loads(exec_response["output"])
        except json.JSONDecodeError:
            pass

    # Add error info if failed
    if status == "FAILED" and exec_response.get("error"):
        result["error"] = exec_response.get("error")
        result["cause"] = exec_response.get("cause")

    # Add live crawl statistics from DynamoDB
    stats = get_crawl_stats(university_id)
    result["stats"] = stats

    return response(200, result)


# ═════════════════════════════════════════════
# HELPER FUNCTIONS
# ═════════════════════════════════════════════
def find_running_execution(university_id):
    """
    Check if there's already a running execution for this university.
    Returns the execution name if found, None otherwise.
    """
    try:
        result = sfn_client.list_executions(
            stateMachineArn=STATE_MACHINE_ARN,
            statusFilter="RUNNING",
            maxResults=20,
        )

        for execution in result.get("executions", []):
            name = execution["name"]
            # Our naming convention: {university_id}-{mode}-{timestamp}
            if name.startswith(f"{university_id}-"):
                return name

    except Exception as e:
        logger.warning(f"Failed to check running executions: {e}")

    return None


def get_crawl_stats(university_id):
    """
    Get current crawl statistics from DynamoDB.
    Counts URLs by status for this university.
    """
    stats = {
        "total_urls": 0,
        "pending": 0,
        "crawled": 0,
        "error": 0,
        "failed": 0,
        "dead": 0,
        "redirected": 0,
        "blocked_robots": 0,
        "skipped_depth": 0,
    }

    try:
        # Use the university-status-index GSI we added
        for status_key in stats:
            if status_key == "total_urls":
                continue

            try:
                result = url_table.query(
                    IndexName="university-status-index",
                    KeyConditionExpression="university_id = :uid AND crawl_status = :status",
                    ExpressionAttributeValues={
                        ":uid": university_id,
                        ":status": status_key,
                    },
                    Select="COUNT",
                )
                count = result.get("Count", 0)
                stats[status_key] = count
                stats["total_urls"] += count
            except Exception:
                pass

    except Exception as e:
        logger.warning(f"Failed to get crawl stats: {e}")

    return stats


def response(status_code, body):
    """Format API Gateway Lambda proxy response."""
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, X-Api-Key",
        },
        "body": json.dumps(body, default=str),
    }
