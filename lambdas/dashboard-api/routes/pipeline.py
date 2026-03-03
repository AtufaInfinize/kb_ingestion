"""Pipeline management endpoints — start, status, list."""

import os
import json
import logging
from datetime import datetime, timezone
from typing import Optional

import boto3
from fastapi import APIRouter, Query, Request

from utils.response import api_response
from utils.dynamo import (
    jobs_table, get_crawl_stats, get_processing_stats, get_queue_depth,
    find_running_pipeline,
)
from utils.pagination import encode_token, decode_token

logger = logging.getLogger(__name__)
router = APIRouter()

sfn = boto3.client('stepfunctions')
sqs = boto3.client('sqs')
bedrock = boto3.client('bedrock')
STATE_MACHINE_ARN = os.environ.get('STATE_MACHINE_ARN')
CRAWL_QUEUE_URL = os.environ.get('CRAWL_QUEUE_URL')
PROCESSING_QUEUE_URL = os.environ.get('PROCESSING_QUEUE_URL')


@router.post("/universities/{uid}/pipeline")
async def start_pipeline(uid: str, request: Request):
    """POST /v1/universities/{uid}/pipeline — Start full pipeline."""
    try:
        body = await request.json()
    except Exception:
        return api_response(400, {'error': 'Invalid JSON body'})

    refresh_mode = body.get('refresh_mode', 'incremental')
    valid_modes = ('full', 'incremental', 'domain')
    if refresh_mode not in valid_modes:
        return api_response(400, {'error': f'refresh_mode must be one of: {", ".join(valid_modes)}'})

    domain = body.get('domain', '')
    if refresh_mode == 'domain' and not domain:
        return api_response(400, {'error': 'domain is required for domain refresh_mode'})

    running_job = find_running_pipeline(uid)
    if running_job:
        return api_response(409, {
            'error': 'A pipeline is already running for this university',
            'running_job_id': running_job,
        })

    now = datetime.now(timezone.utc)
    ts = now.strftime('%Y%m%d-%H%M%S')
    job_id = f'{uid}-{refresh_mode}-{ts}'

    sfn_input = {
        'university_id': uid,
        'refresh_mode': refresh_mode,
        'domain': domain,
        'triggered_at': now.isoformat(),
        'triggered_by': 'api',
    }

    try:
        sfn_resp = sfn.start_execution(
            stateMachineArn=STATE_MACHINE_ARN,
            name=job_id,
            input=json.dumps(sfn_input),
        )
        execution_arn = sfn_resp['executionArn']
    except Exception as e:
        logger.error(f"Failed to start Step Functions: {e}", exc_info=True)
        return api_response(500, {'error': f'Failed to start pipeline: {e}'})

    ttl = int(now.timestamp()) + (90 * 86400)  # 90 days
    jobs_table.put_item(Item={
        'job_id': job_id,
        'university_id': uid,
        'created_at': now.isoformat(),
        'refresh_mode': refresh_mode,
        'domain': domain,
        'triggered_by': 'api',
        'overall_status': 'running',
        'execution_arn': execution_arn,
        'crawl_stage': {'status': 'running', 'started_at': now.isoformat()},
        'clean_stage': {'status': 'pending'},
        'classify_stage': {'status': 'pending'},
        'ttl': ttl,
    })

    return api_response(202, {
        'job_id': job_id,
        'university_id': uid,
        'refresh_mode': refresh_mode,
        'overall_status': 'running',
        'crawl_stage': {'status': 'running', 'started_at': now.isoformat()},
        'clean_stage': {'status': 'pending'},
        'classify_stage': {'status': 'pending'},
    })


@router.get("/universities/{uid}/pipeline")
def list_jobs(
    uid: str,
    limit: int = Query(default=20, le=100),
    next_token: Optional[str] = None,
):
    """GET /v1/universities/{uid}/pipeline — List job history."""
    kwargs = {
        'IndexName': 'university-created-index',
        'KeyConditionExpression': 'university_id = :uid',
        'ExpressionAttributeValues': {':uid': uid},
        'ScanIndexForward': False,  # Newest first
        'Limit': limit,
    }

    if next_token:
        kwargs['ExclusiveStartKey'] = decode_token(next_token)

    resp = jobs_table.query(**kwargs)
    items = resp.get('Items', [])
    new_token = encode_token(resp.get('LastEvaluatedKey'))

    return api_response(200, {
        'jobs': items,
        'count': len(items),
        'next_token': new_token,
    })


@router.get("/universities/{uid}/pipeline/{job_id}")
def get_job_status(uid: str, job_id: str):
    """GET /v1/universities/{uid}/pipeline/{job_id} — Live pipeline progress."""
    resp = jobs_table.get_item(Key={'job_id': job_id})
    job = resp.get('Item')
    if not job:
        return api_response(404, {'error': f'Job not found: {job_id}'})

    if job.get('university_id') != uid:
        return api_response(404, {'error': f'Job not found: {job_id}'})

    if job.get('overall_status') == 'running':
        job = _augment_live_progress(job, uid)

    return api_response(200, job)


def _augment_live_progress(job, university_id):
    """Add real-time counts to a running job record."""
    now = datetime.now(timezone.utc).isoformat()
    updated = False

    crawl_stats = get_crawl_stats(university_id)
    processing_stats = get_processing_stats(university_id)

    total_crawled = crawl_stats.get('crawled', 0)
    total_pending = crawl_stats.get('pending', 0)
    total_errors = sum(crawl_stats.get(s, 0) for s in ['error', 'failed', 'dead'])
    total_classified = processing_stats.get('classified', 0)
    total_unprocessed = processing_stats.get('unprocessed', 0)

    crawl_q = get_queue_depth(CRAWL_QUEUE_URL) if CRAWL_QUEUE_URL else {'available': 0, 'in_flight': 0}
    processing_q = get_queue_depth(PROCESSING_QUEUE_URL) if PROCESSING_QUEUE_URL else {'available': 0, 'in_flight': 0}

    crawl_queue_empty = crawl_q['available'] == 0 and crawl_q['in_flight'] == 0
    processing_queue_empty = processing_q['available'] == 0 and processing_q['in_flight'] == 0

    crawl_stage = job.get('crawl_stage', {})
    clean_stage = job.get('clean_stage', {})
    classify_stage = job.get('classify_stage', {})

    # Update crawl stage
    crawl_stage['total'] = total_crawled + total_pending + total_errors
    crawl_stage['completed'] = total_crawled
    crawl_stage['pending'] = total_pending
    crawl_stage['failed'] = total_errors
    crawl_stage['queue'] = crawl_q

    # Detect stage transitions
    # Guard: total > 0 prevents false completion when pipeline just started
    # (before seeds are queued, all counts are 0 and queues are empty)
    crawl_done = crawl_queue_empty and total_pending == 0 and crawl_stage['total'] > 0

    if crawl_done and crawl_stage.get('status') == 'running':
        crawl_stage['status'] = 'completed'
        crawl_stage['completed_at'] = now
        clean_stage['status'] = 'running'
        clean_stage['started_at'] = now
        updated = True

    # Update clean stage
    clean_stage['completed'] = total_classified + total_unprocessed
    clean_stage['queue'] = processing_q

    clean_done = crawl_done and processing_queue_empty

    if clean_done and clean_stage.get('status') == 'running':
        clean_stage['status'] = 'completed'
        clean_stage['completed_at'] = now
        # Classification is triggered by the Step Functions orchestrator
        # after crawl+clean complete — not by the dashboard API.
        if not classify_stage.get('classify_triggered'):
            classify_stage['status'] = 'waiting'
        updated = True

    # Update classify stage
    classify_stage['completed'] = total_classified
    classify_stage['total'] = total_crawled

    # Check Step Functions execution status
    sfn_succeeded = False
    execution_arn = job.get('execution_arn')
    if execution_arn:
        try:
            exec_resp = sfn.describe_execution(executionArn=execution_arn)
            sfn_status = exec_resp.get('status', '')
            if sfn_status in ('FAILED', 'ABORTED', 'TIMED_OUT'):
                job['overall_status'] = 'failed'
                updated = True
            elif sfn_status == 'SUCCEEDED':
                sfn_succeeded = True
        except Exception:
            pass

    # Check Bedrock batch job statuses (stored by orchestrator)
    batch_jobs_done = _check_batch_jobs(classify_stage.get('batch_jobs', []))
    classify_stage['batch_jobs_done'] = batch_jobs_done

    # For incremental crawls, classification is skipped entirely.
    # Mark complete when crawl+clean done and SFN succeeded.
    refresh_mode = job.get('refresh_mode', 'full')
    incremental_done = (
        refresh_mode == 'incremental'
        and clean_done
        and sfn_succeeded
    )

    # For full crawls, classification must also finish.
    classify_done = (
        clean_done
        and classify_stage.get('classify_triggered', False)
        and sfn_succeeded
        and batch_jobs_done
    )

    if (incremental_done or classify_done) and job.get('overall_status') == 'running':
        if refresh_mode == 'incremental':
            classify_stage['status'] = 'skipped'
        else:
            classify_stage['status'] = 'completed'
            classify_stage['completed_at'] = now
        job['overall_status'] = 'completed'
        updated = True

    job['crawl_stage'] = crawl_stage
    job['clean_stage'] = clean_stage
    job['classify_stage'] = classify_stage

    # Persist updated stages if transitions happened
    if updated:
        try:
            jobs_table.update_item(
                Key={'job_id': job['job_id']},
                UpdateExpression='SET overall_status = :os, crawl_stage = :cs, clean_stage = :cls, classify_stage = :clss',
                ExpressionAttributeValues={
                    ':os': job['overall_status'],
                    ':cs': crawl_stage,
                    ':cls': clean_stage,
                    ':clss': classify_stage,
                },
            )
        except Exception as e:
            logger.warning(f"Failed to persist job status update: {e}")

    return job


def _check_batch_jobs(batch_jobs):
    """Check if all stored Bedrock batch jobs are completed.

    Returns True if batch_jobs list is empty (no jobs to check) or
    all jobs have status Completed/PartiallyCompleted/Failed/Stopped.
    Returns False if any job is still running or status can't be determined.
    """
    if not batch_jobs:
        return True  # Nothing to classify, or batch_jobs not yet stored — treat as done

    for job_info in batch_jobs:
        job_arn = job_info.get('job_arn', '')
        if not job_arn:
            continue
        try:
            resp = bedrock.get_model_invocation_job(jobIdentifier=job_arn)
            status = resp.get('status', '')
            # Update the job_info dict with latest status (shown in UI)
            job_info['status'] = status
            if status in ('InProgress', 'Submitted', 'Validating', 'Scheduled'):
                return False
        except Exception as e:
            logger.warning(f"Failed to check batch job {job_arn}: {e}")
            return False

    return True
