"""Bedrock Knowledge Base sync endpoints."""

import os
import json
import logging
from datetime import datetime, timezone

import boto3
from fastapi import APIRouter, Request

from utils.response import api_response
from utils.dynamo import entity_table

logger = logging.getLogger(__name__)
router = APIRouter()

s3 = boto3.client('s3')
bedrock_agent = boto3.client('bedrock-agent')

BUCKET = os.environ.get('CONTENT_BUCKET')


@router.post("/universities/{uid}/kb/sync")
async def start_sync(uid: str, request: Request):
    """POST /v1/universities/{uid}/kb/sync — Trigger Bedrock KB ingestion."""
    try:
        body = await request.json()
    except Exception:
        body = {}

    kb_config = _load_kb_config(uid)
    if not kb_config:
        return api_response(400, {
            'error': f'No knowledge base configured for {uid}. '
                     'Add kb_config to configs/{uid}.json'
        })

    knowledge_base_id = kb_config.get('knowledge_base_id')
    data_source_ids = kb_config.get('data_source_ids', [])

    if not knowledge_base_id or not data_source_ids:
        return api_response(400, {'error': 'knowledge_base_id and data_source_ids required in kb_config'})

    results = []
    for ds_id in data_source_ids:
        try:
            resp = bedrock_agent.start_ingestion_job(
                knowledgeBaseId=knowledge_base_id,
                dataSourceId=ds_id,
            )
            ingestion = resp.get('ingestionJob', {})
            results.append({
                'data_source_id': ds_id,
                'ingestion_job_id': ingestion.get('ingestionJobId'),
                'status': ingestion.get('status'),
            })
        except Exception as e:
            results.append({
                'data_source_id': ds_id,
                'error': str(e),
            })

    # Clear the pending_sync notification so the sidebar banner goes away
    try:
        entity_table.update_item(
            Key={'university_id': uid, 'entity_key': 'kb_sync_status'},
            UpdateExpression='SET #st = :s, synced_at = :ts',
            ExpressionAttributeNames={'#st': 'status'},
            ExpressionAttributeValues={
                ':s': 'syncing',
                ':ts': datetime.now(timezone.utc).isoformat(),
            },
        )
    except Exception as e:
        logger.warning(f'Could not update kb_sync_status for {uid}: {e}')

    return api_response(202, {
        'university_id': uid,
        'knowledge_base_id': knowledge_base_id,
        'ingestion_jobs': results,
    })


@router.get("/universities/{uid}/kb/sync")
def get_sync_status(uid: str):
    """GET /v1/universities/{uid}/kb/sync — Get latest KB sync status."""
    kb_config = _load_kb_config(uid)
    if not kb_config:
        return api_response(400, {
            'error': f'No knowledge base configured for {uid}'
        })

    knowledge_base_id = kb_config.get('knowledge_base_id')
    data_source_ids = kb_config.get('data_source_ids', [])

    results = []
    for ds_id in data_source_ids:
        try:
            resp = bedrock_agent.list_ingestion_jobs(
                knowledgeBaseId=knowledge_base_id,
                dataSourceId=ds_id,
                maxResults=3,
                sortBy={'attribute': 'STARTED_AT', 'order': 'DESCENDING'},
            )
            jobs = []
            for job in resp.get('ingestionJobSummaries', []):
                jobs.append({
                    'ingestion_job_id': job.get('ingestionJobId'),
                    'status': job.get('status'),
                    'started_at': job.get('startedAt'),
                    'updated_at': job.get('updatedAt'),
                    'statistics': job.get('statistics'),
                })
            results.append({
                'data_source_id': ds_id,
                'recent_jobs': jobs,
            })
        except Exception as e:
            results.append({
                'data_source_id': ds_id,
                'error': str(e),
            })

    return api_response(200, {
        'university_id': uid,
        'knowledge_base_id': knowledge_base_id,
        'data_sources': results,
    })


def _load_kb_config(university_id):
    """Load kb_config section from university config in S3."""
    try:
        resp = s3.get_object(Bucket=BUCKET, Key=f'configs/{university_id}.json')
        config = json.loads(resp['Body'].read())
        return config.get('kb_config')
    except Exception:
        return None
