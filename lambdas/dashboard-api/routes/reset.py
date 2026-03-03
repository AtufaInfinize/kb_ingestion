"""University data reset endpoints — delete crawl/classification results."""

import os
import json
import logging
from datetime import datetime, timezone

import boto3
from fastapi import APIRouter, Request

from utils.response import api_response
from utils.dynamo import url_table, jobs_table, record_kb_sync_event

logger = logging.getLogger(__name__)
router = APIRouter()

s3 = boto3.client('s3')
sfn = boto3.client('stepfunctions')
dynamodb = boto3.resource('dynamodb')

BUCKET = os.environ.get('CONTENT_BUCKET')
ENTITY_STORE_TABLE = os.environ.get('ENTITY_STORE_TABLE', 'entity-store-dev')
STATE_MACHINE_ARN = os.environ.get('STATE_MACHINE_ARN')

entity_table = dynamodb.Table(ENTITY_STORE_TABLE)


@router.post("/universities/{uid}/reset")
async def reset_university(uid: str, request: Request):
    """POST /v1/universities/{uid}/reset — Delete data for a university.

    Body: {"scope": "all" | "classification"}

    scope=all:
        - Delete all url-registry items for university
        - Delete all entity-store items for university
        - Delete all pipeline-jobs items for university
        - Delete S3: raw-html/{uid}/, clean-content/{uid}/, batch-jobs/{uid}/,
                     crawl-summaries/{uid}/
        - Keeps: configs/{uid}.json

    scope=classification:
        - Delete entity-store items for university
        - Delete S3 metadata sidecars: clean-content/{uid}/**/*.metadata.json
        - Delete S3: batch-jobs/{uid}/
        - Reset url-registry: clear page_category, processing_status → "cleaned"
        - Keeps: raw HTML, clean markdown, config, jobs
    """
    try:
        body = await request.json()
    except Exception:
        return api_response(400, {'error': 'Invalid JSON body'})

    scope = body.get('scope', 'all')
    if scope not in ('all', 'classification'):
        return api_response(400, {'error': 'scope must be "all" or "classification"'})

    results = {'scope': scope, 'university_id': uid}

    try:
        results['pipelines_stopped'] = _stop_running_pipelines(uid)

        if scope == 'all':
            results.update(_reset_all(uid))
        else:
            results.update(_reset_classification(uid))

        record_kb_sync_event(uid, 'data_reset')

        return api_response(200, results)

    except Exception as e:
        logger.error(f"Reset error: {e}", exc_info=True)
        return api_response(500, {'error': str(e)})


def _stop_running_pipelines(university_id):
    """Stop all running Step Functions executions for this university.

    Queries Step Functions directly (not just pipeline-jobs table) to catch
    ALL running executions — including scheduled triggers and executions
    that don't have a pipeline-jobs record.
    """
    stopped = 0
    now = datetime.now(timezone.utc).isoformat()

    # 1. Stop executions via Step Functions API (catches everything)
    if STATE_MACHINE_ARN:
        try:
            paginator = sfn.get_paginator('list_executions')
            for page in paginator.paginate(
                stateMachineArn=STATE_MACHINE_ARN,
                statusFilter='RUNNING',
            ):
                for execution in page.get('executions', []):
                    exec_arn = execution['executionArn']
                    exec_name = execution.get('name', '')

                    try:
                        desc = sfn.describe_execution(executionArn=exec_arn)
                        exec_input = json.loads(desc.get('input', '{}'))
                        if exec_input.get('university_id') != university_id:
                            continue
                    except Exception:
                        if not exec_name.startswith(f'{university_id}-'):
                            continue

                    try:
                        sfn.stop_execution(
                            executionArn=exec_arn,
                            cause='Reset requested via dashboard',
                        )
                        logger.info(f"Stopped execution: {exec_name}")
                        stopped += 1
                    except Exception as e:
                        logger.warning(f"Failed to stop execution {exec_name}: {e}")
        except Exception as e:
            logger.warning(f"Failed to list executions: {e}")

    # 2. Also mark any pipeline-jobs records as cancelled
    try:
        kwargs = {
            'IndexName': 'university-created-index',
            'KeyConditionExpression': 'university_id = :uid',
            'ExpressionAttributeValues': {':uid': university_id},
            'ScanIndexForward': False,
        }
        resp = jobs_table.query(**kwargs)
        for job in resp.get('Items', []):
            if job.get('overall_status') != 'running':
                continue
            try:
                jobs_table.update_item(
                    Key={'job_id': job['job_id']},
                    UpdateExpression='SET overall_status = :s, cancelled_at = :t',
                    ExpressionAttributeValues={':s': 'cancelled', ':t': now},
                )
            except Exception as e:
                logger.warning(f"Failed to update job {job['job_id']}: {e}")
    except Exception as e:
        logger.warning(f"Failed to update pipeline-jobs: {e}")

    logger.info(f"Stopped {stopped} running pipelines for {university_id}")
    return stopped


def _reset_all(university_id):
    """Delete everything except config."""
    counts = {}
    counts['url_registry_deleted'] = _delete_url_registry_items(university_id)
    counts['entity_store_deleted'] = _delete_entity_store_items(university_id)
    counts['pipeline_jobs_deleted'] = _delete_pipeline_jobs(university_id)

    prefixes = [
        f'raw-html/{university_id}/',
        f'clean-content/{university_id}/',
        f'batch-jobs/{university_id}/',
        f'crawl-summaries/{university_id}/',
    ]
    s3_deleted = 0
    for prefix in prefixes:
        s3_deleted += _delete_s3_prefix(prefix)
    counts['s3_objects_deleted'] = s3_deleted

    return counts


def _reset_classification(university_id):
    """Delete classification results only — keep crawl data."""
    counts = {}
    counts['entity_store_deleted'] = _delete_entity_store_items(university_id)
    counts['sidecars_deleted'] = _delete_s3_prefix(
        f'clean-content/{university_id}/',
        suffix_filter='.metadata.json',
    )
    counts['batch_jobs_deleted'] = _delete_s3_prefix(f'batch-jobs/{university_id}/')
    counts['urls_reset'] = _reset_classification_fields(university_id)
    return counts


def _delete_url_registry_items(university_id):
    """Delete all url-registry items for a university via GSI scan + batch delete."""
    deleted = 0
    statuses = ['pending', 'crawled', 'error', 'failed', 'dead',
                'redirected', 'blocked_robots', 'skipped_depth']

    for status in statuses:
        kwargs = {
            'IndexName': 'university-status-index',
            'KeyConditionExpression': 'university_id = :uid AND crawl_status = :cs',
            'ExpressionAttributeValues': {':uid': university_id, ':cs': status},
            'ProjectionExpression': '#u',
            'ExpressionAttributeNames': {'#u': 'url'},
        }

        while True:
            resp = url_table.query(**kwargs)
            items = resp.get('Items', [])

            if items:
                with url_table.batch_writer() as batch:
                    for item in items:
                        batch.delete_item(Key={'url': item['url']})
                        deleted += 1

            if 'LastEvaluatedKey' not in resp:
                break
            kwargs['ExclusiveStartKey'] = resp['LastEvaluatedKey']

    logger.info(f"Deleted {deleted} url-registry items for {university_id}")
    return deleted


def _delete_entity_store_items(university_id):
    """Delete all entity-store items for a university."""
    deleted = 0
    kwargs = {
        'KeyConditionExpression': 'university_id = :uid',
        'ExpressionAttributeValues': {':uid': university_id},
        'ProjectionExpression': 'university_id, entity_key',
    }

    while True:
        resp = entity_table.query(**kwargs)
        items = resp.get('Items', [])

        if items:
            with entity_table.batch_writer() as batch:
                for item in items:
                    batch.delete_item(Key={
                        'university_id': item['university_id'],
                        'entity_key': item['entity_key'],
                    })
                    deleted += 1

        if 'LastEvaluatedKey' not in resp:
            break
        kwargs['ExclusiveStartKey'] = resp['LastEvaluatedKey']

    logger.info(f"Deleted {deleted} entity-store items for {university_id}")
    return deleted


def _delete_pipeline_jobs(university_id):
    """Delete all pipeline-jobs items for a university."""
    deleted = 0
    kwargs = {
        'IndexName': 'university-created-index',
        'KeyConditionExpression': 'university_id = :uid',
        'ExpressionAttributeValues': {':uid': university_id},
        'ProjectionExpression': 'job_id',
    }

    while True:
        resp = jobs_table.query(**kwargs)
        items = resp.get('Items', [])

        if items:
            with jobs_table.batch_writer() as batch:
                for item in items:
                    batch.delete_item(Key={'job_id': item['job_id']})
                    deleted += 1

        if 'LastEvaluatedKey' not in resp:
            break
        kwargs['ExclusiveStartKey'] = resp['LastEvaluatedKey']

    logger.info(f"Deleted {deleted} pipeline-jobs items for {university_id}")
    return deleted


def _delete_s3_prefix(prefix, suffix_filter=None):
    """Delete all S3 objects under a prefix. Optional suffix filter."""
    deleted = 0
    continuation_token = None

    while True:
        list_kwargs = {'Bucket': BUCKET, 'Prefix': prefix, 'MaxKeys': 1000}
        if continuation_token:
            list_kwargs['ContinuationToken'] = continuation_token

        resp = s3.list_objects_v2(**list_kwargs)
        contents = resp.get('Contents', [])

        if not contents:
            break

        if suffix_filter:
            keys = [{'Key': obj['Key']} for obj in contents if obj['Key'].endswith(suffix_filter)]
        else:
            keys = [{'Key': obj['Key']} for obj in contents]

        if keys:
            s3.delete_objects(Bucket=BUCKET, Delete={'Objects': keys, 'Quiet': True})
            deleted += len(keys)

        if not resp.get('IsTruncated'):
            break
        continuation_token = resp.get('NextContinuationToken')

    logger.info(f"Deleted {deleted} S3 objects under {prefix}")
    return deleted


def _reset_classification_fields(university_id):
    """Reset page_category and processing_status for all classified items."""
    reset_count = 0

    kwargs = {
        'IndexName': 'university-status-index',
        'KeyConditionExpression': 'university_id = :uid AND crawl_status = :cs',
        'ExpressionAttributeValues': {':uid': university_id, ':cs': 'crawled'},
        'ProjectionExpression': '#u',
        'ExpressionAttributeNames': {'#u': 'url'},
    }

    while True:
        resp = url_table.query(**kwargs)
        items = resp.get('Items', [])

        for item in items:
            try:
                url_table.update_item(
                    Key={'url': item['url']},
                    UpdateExpression='REMOVE page_category, subcategory, classified_at SET processing_status = :ps',
                    ExpressionAttributeValues={':ps': 'cleaned'},
                )
                reset_count += 1
            except Exception as e:
                logger.warning(f"Failed to reset {item['url']}: {e}")

        if 'LastEvaluatedKey' not in resp:
            break
        kwargs['ExclusiveStartKey'] = resp['LastEvaluatedKey']

    logger.info(f"Reset classification for {reset_count} items in {university_id}")
    return reset_count
