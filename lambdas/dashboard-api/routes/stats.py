"""University overview and stats endpoints."""

import os
import json
import logging
from datetime import datetime, timezone
from typing import Optional

import boto3
from boto3.dynamodb.conditions import Key, Attr
from fastapi import APIRouter

from utils.response import api_response
from utils.dynamo import get_crawl_stats, get_processing_stats, jobs_table, load_freshness_windows, entity_table

logger = logging.getLogger(__name__)
router = APIRouter()

s3 = boto3.client('s3')
BUCKET = os.environ.get('CONTENT_BUCKET')


@router.get("/universities")
def list_universities():
    """GET /v1/universities — List all configured universities."""
    try:
        resp = s3.list_objects_v2(
            Bucket=BUCKET,
            Prefix='configs/',
            Delimiter='/',
        )
        universities = []
        for obj in resp.get('Contents', []):
            key = obj['Key']
            if not key.endswith('.json'):
                continue
            try:
                config_resp = s3.get_object(Bucket=BUCKET, Key=key)
                config = json.loads(config_resp['Body'].read())
                universities.append({
                    'university_id': config.get('university_id'),
                    'name': config.get('name', config.get('university_id')),
                })
            except Exception as e:
                logger.warning(f"Failed to read config {key}: {e}")

        return api_response(200, {'universities': universities})

    except Exception as e:
        logger.error(f"list_universities error: {e}", exc_info=True)
        return api_response(500, {'error': str(e)})


@router.get("/universities/{uid}/stats")
def get_stats(uid: str):
    """GET /v1/universities/{uid}/stats — Dashboard overview stats."""
    try:
        config = _load_config(uid)
        name = config.get('name', uid) if config else uid

        crawl_stats = get_crawl_stats(uid)
        processing_stats = get_processing_stats(uid)

        total = sum(crawl_stats.values())

        unclassified_count = _count_unclassified(uid)

        stale_info = _get_stale_info(uid)
        kb_sync_info = _get_kb_sync_status(uid)

        return api_response(200, {
            'university_id': uid,
            'name': name,
            'total_urls': total,
            'urls_by_crawl_status': crawl_stats,
            'urls_by_processing_status': processing_stats,
            'unclassified_count': unclassified_count,
            'last_crawled_at': stale_info.get('last_crawled_at'),
            'days_since_crawl': stale_info.get('days_since_crawl'),
            'stale_categories': stale_info.get('stale_categories', []),
            'pending_kb_sync': kb_sync_info.get('pending_kb_sync', False),
            'pages_changed_last_crawl': kb_sync_info.get('pages_changed', 0),
            'crawl_completed_at': kb_sync_info.get('crawl_completed_at'),
            'config': {
                'seed_urls_count': len(config.get('seed_urls', [])) if config else 0,
                'max_crawl_depth': config.get('crawl_config', {}).get('max_crawl_depth', 0) if config else 0,
                'allowed_domain_patterns': config.get('crawl_config', {}).get('allowed_domain_patterns', []) if config else [],
            } if config else None,
        })

    except Exception as e:
        logger.error(f"get_stats error: {e}", exc_info=True)
        return api_response(500, {'error': str(e)})


def _count_unclassified(university_id: str) -> int:
    """Count .md files that have no .metadata.json sidecar (pages needing classification).

    Lists up to 1000 objects per prefix to keep latency acceptable for large sites.
    For universities with >1000 pages this is an undercount — treated as an estimate.
    """
    prefix = f'clean-content/{university_id}/'
    try:
        md_count       = 0
        metadata_count = 0
        paginator = s3.get_paginator('list_objects_v2')

        for page in paginator.paginate(Bucket=BUCKET, Prefix=prefix, PaginationConfig={'MaxItems': 5000}):
            for obj in page.get('Contents', []):
                key = obj['Key']
                if key.endswith('.md.metadata.json'):
                    metadata_count += 1
                elif key.endswith('.md'):
                    md_count += 1

        return max(0, md_count - metadata_count)
    except Exception as e:
        logger.warning(f"Failed to count unclassified pages for {university_id}: {e}")
        return 0


def _get_stale_info(university_id: str) -> dict:
    """Return staleness info: last crawl date and which categories have exceeded their window.

    Queries pipeline-jobs for the most recent completed job, then compares
    days-since-crawl to each category's freshness window (from entity-store).
    """
    try:
        windows = load_freshness_windows(university_id)

        # Find the most recent completed pipeline job (up to 50 scanned, newest-first)
        resp = jobs_table.query(
            IndexName='university-created-index',
            KeyConditionExpression=Key('university_id').eq(university_id),
            FilterExpression=Attr('overall_status').eq('completed'),
            ScanIndexForward=False,
            Limit=50,
        )
        items = resp.get('Items', [])

        if not items:
            # Never crawled → every category is stale
            return {
                'last_crawled_at': None,
                'days_since_crawl': None,
                'stale_categories': list(windows.keys()),
            }

        last_crawl_str = items[0].get('created_at', '')
        try:
            last_crawl_dt = datetime.fromisoformat(last_crawl_str.replace('Z', '+00:00'))
        except ValueError:
            logger.warning(f"Could not parse last crawl date: {last_crawl_str!r}")
            return {'last_crawled_at': last_crawl_str, 'days_since_crawl': None, 'stale_categories': []}

        days_since = (datetime.now(timezone.utc) - last_crawl_dt).days
        stale = [cat for cat, window_days in windows.items() if days_since >= window_days]

        return {
            'last_crawled_at': last_crawl_str,
            'days_since_crawl': days_since,
            'stale_categories': stale,
        }

    except Exception as e:
        logger.warning(f"Stale check failed for {university_id}: {e}")
        return {'last_crawled_at': None, 'days_since_crawl': None, 'stale_categories': []}


def _get_kb_sync_status(university_id: str) -> dict:
    """Read kb_sync_status from entity-store.

    Returns dict with:
      pending_kb_sync: True if content changed and KB Sync has not yet been triggered
      pages_changed: number of pages that changed in the last crawl
      crawl_completed_at: ISO timestamp of last crawl completion
    """
    try:
        resp = entity_table.get_item(
            Key={'university_id': university_id, 'entity_key': 'kb_sync_status'}
        )
        item = resp.get('Item', {})
        status = item.get('status', '')
        pages_changed = int(item.get('pages_changed', 0))
        return {
            'pending_kb_sync': status == 'pending_sync',
            'pages_changed': pages_changed,
            'crawl_completed_at': item.get('crawl_completed_at'),
        }
    except Exception as e:
        logger.warning(f'Could not read kb_sync_status for {university_id}: {e}')
        return {'pending_kb_sync': False, 'pages_changed': 0, 'crawl_completed_at': None}


def _load_config(university_id):
    """Load university config from S3."""
    try:
        resp = s3.get_object(Bucket=BUCKET, Key=f'configs/{university_id}.json')
        return json.loads(resp['Body'].read())
    except s3.exceptions.NoSuchKey:
        return None
    except Exception:
        return None
