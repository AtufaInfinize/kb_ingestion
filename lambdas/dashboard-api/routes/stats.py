"""University overview and stats endpoints."""

import os
import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Optional

import boto3
from boto3.dynamodb.conditions import Key, Attr
from fastapi import APIRouter

from utils.response import api_response
from utils.dynamo import (
    get_crawl_stats, get_processing_stats, count_total_urls,
    jobs_table, load_freshness_windows, entity_table,
)

logger = logging.getLogger(__name__)
router = APIRouter()

s3 = boto3.client('s3')
bedrock_agent = boto3.client('bedrock-agent')
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
    """GET /v1/universities/{uid}/stats — Dashboard overview stats.

    Runs all stat queries in parallel for speed. Returns:
      - Discovery stats  (DynamoDB url-registry)
      - Content stats    (S3 clean-content/ and raw-pdf/)
      - KB ingestion     (Bedrock Agent latest job)
      - Freshness / staleness
      - KB sync notification status
    """
    try:
        config = _load_config(uid)
        name = config.get('name', uid) if config else uid

        # Run all independent queries in parallel
        results = {}
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {
                pool.submit(get_crawl_stats, uid): 'crawl_stats',
                pool.submit(get_processing_stats, uid): 'processing_stats',
                pool.submit(count_total_urls, uid): 'total_registered',
                pool.submit(_count_s3_content, uid): 's3_content',
                pool.submit(_count_s3_media, uid): 's3_media',
                pool.submit(_get_stale_info, uid): 'stale_info',
                pool.submit(_get_kb_sync_status, uid): 'kb_sync_info',
                pool.submit(_get_kb_ingestion_stats, uid, config): 'kb_ingestion',
            }
            for future in as_completed(futures):
                key = futures[future]
                try:
                    results[key] = future.result()
                except Exception as e:
                    logger.warning(f"Stats query failed for {key}: {e}")
                    results[key] = None

        crawl_stats = results.get('crawl_stats') or {}
        processing_stats = results.get('processing_stats') or {}
        total_registered = results.get('total_registered') or 0
        s3_content = results.get('s3_content') or {}
        s3_media = results.get('s3_media') or {}
        stale_info = results.get('stale_info') or {}
        kb_sync_info = results.get('kb_sync_info') or {}
        kb_ingestion = results.get('kb_ingestion') or {}

        # S3-based content counts (source of truth for what can go into KB)
        total_content_pages = s3_content.get('md_count', 0)
        classified_pages = s3_content.get('metadata_count', 0)
        unclassified_pages = max(0, total_content_pages - classified_pages)

        # Media counts
        total_media_files = s3_media.get('total', 0)
        media_by_type = s3_media.get('by_type', {})

        # KB ingestion
        ingested_pages = kb_ingestion.get('ingested', 0)
        ingestion_failed = kb_ingestion.get('failed', 0)

        # pages_changed: tracked by content-cleaner (incremental crawl) and
        # crawl-orchestrator.  Represents new/modified pages from the latest
        # crawl that need to be synced to KB.  Always available (even after
        # sync completes — useful as a historical stat).
        pages_changed = kb_sync_info.get('pages_changed', 0)
        pending_kb = kb_sync_info.get('pending_kb_sync', False)

        # Dead URLs
        dead_count = crawl_stats.get('dead', 0)

        # Total discovered (sum of all crawl statuses in DynamoDB)
        total_discovered = sum(crawl_stats.values())

        return api_response(200, {
            'university_id': uid,
            'name': name,

            # --- Discovery stats (DynamoDB url-registry) ---
            'total_urls': total_registered,
            'total_discovered_urls': total_discovered,
            'urls_by_crawl_status': crawl_stats,
            'urls_by_processing_status': processing_stats,

            # --- Content stats (S3 clean-content/) ---
            'total_content_pages': total_content_pages,
            'classified_pages': classified_pages,
            'unclassified_pages': unclassified_pages,

            # --- Media stats (S3 raw-pdf/) ---
            'total_media_files': total_media_files,
            'media_by_type': media_by_type,

            # --- KB ingestion stats (Bedrock Agent) ---
            'kb_ingestion': {
                'ingested_pages': ingested_pages,
                'failed_pages': ingestion_failed,
                'scanned_pages': kb_ingestion.get('scanned', 0),
                'new_indexed': kb_ingestion.get('new_indexed', 0),
                'modified_indexed': kb_ingestion.get('modified_indexed', 0),
                'deleted': kb_ingestion.get('deleted', 0),
                'last_sync_status': kb_ingestion.get('last_status'),
                'last_sync_at': kb_ingestion.get('last_sync_at'),
            },

            # --- KB sync (content-cleaner tracking) ---
            'pages_changed': pages_changed,
            'pending_kb_sync': pending_kb,

            # --- Dead URLs ---
            'dead_urls': dead_count,

            # --- Freshness / staleness ---
            'last_crawled_at': stale_info.get('last_crawled_at'),
            'days_since_crawl': stale_info.get('days_since_crawl'),
            'stale_categories': stale_info.get('stale_categories', []),

            # --- KB sync notification ---
            'crawl_completed_at': kb_sync_info.get('crawl_completed_at'),

            # --- Config summary ---
            'config': {
                'seed_urls_count': len(config.get('seed_urls', [])) if config else 0,
                'max_crawl_depth': config.get('crawl_config', {}).get('max_crawl_depth', 0) if config else 0,
                'allowed_domain_patterns': config.get('crawl_config', {}).get('allowed_domain_patterns', []) if config else [],
            } if config else None,
        })

    except Exception as e:
        logger.error(f"get_stats error: {e}", exc_info=True)
        return api_response(500, {'error': str(e)})


# ---------------------------------------------------------------------------
# S3 content helpers
# ---------------------------------------------------------------------------

def _count_s3_content(university_id: str) -> dict:
    """Count .md files and .md.metadata.json sidecars in clean-content/.

    Returns {md_count, metadata_count, total_size_bytes}.
    Capped at 50,000 objects (~50 S3 API calls) to stay within API Gateway's
    29-second timeout.  For universities exceeding the cap the counts are
    marked approximate.
    """
    prefix = f'clean-content/{university_id}/'
    md_count = 0
    metadata_count = 0
    total_size_bytes = 0
    capped = False
    MAX_OBJECTS = 50_000
    objects_seen = 0
    try:
        paginator = s3.get_paginator('list_objects_v2')
        for page in paginator.paginate(Bucket=BUCKET, Prefix=prefix):
            for obj in page.get('Contents', []):
                objects_seen += 1
                key = obj['Key']
                if key.endswith('.md.metadata.json'):
                    metadata_count += 1
                elif key.endswith('.md'):
                    md_count += 1
                    total_size_bytes += obj.get('Size', 0)
            if objects_seen >= MAX_OBJECTS:
                capped = True
                break
    except Exception as e:
        logger.warning(f"Failed to count S3 content for {university_id}: {e}")
    return {
        'md_count': md_count,
        'metadata_count': metadata_count,
        'total_size_bytes': total_size_bytes,
        'approximate': capped,
    }


def _count_s3_media(university_id: str) -> dict:
    """Count media files in raw-pdf/{university_id}/ (PDFs, images, audio, video).

    Returns {total, by_type: {pdf, image, audio, video, other}, total_size_bytes}.
    """
    prefix = f'raw-pdf/{university_id}/'
    by_type = {'pdf': 0, 'image': 0, 'audio': 0, 'video': 0, 'other': 0}
    total = 0
    total_size_bytes = 0

    ext_map = {
        '.pdf': 'pdf',
        '.jpg': 'image', '.jpeg': 'image', '.png': 'image',
        '.gif': 'image', '.webp': 'image', '.bmp': 'image',
        '.mp3': 'audio', '.wav': 'audio', '.ogg': 'audio',
        '.flac': 'audio', '.aac': 'audio', '.m4a': 'audio',
        '.mp4': 'video', '.avi': 'video', '.mov': 'video',
        '.mkv': 'video', '.webm': 'video',
    }

    try:
        paginator = s3.get_paginator('list_objects_v2')
        for page in paginator.paginate(Bucket=BUCKET, Prefix=prefix,
                                        PaginationConfig={'MaxItems': 10_000}):
            for obj in page.get('Contents', []):
                key = obj['Key'].lower()
                total += 1
                total_size_bytes += obj.get('Size', 0)
                ext = '.' + key.rsplit('.', 1)[-1] if '.' in key else ''
                media_type = ext_map.get(ext, 'other')
                by_type[media_type] += 1
    except Exception as e:
        logger.warning(f"Failed to count S3 media for {university_id}: {e}")
    return {'total': total, 'by_type': by_type, 'total_size_bytes': total_size_bytes}


# ---------------------------------------------------------------------------
# Bedrock KB ingestion helpers
# ---------------------------------------------------------------------------

def _get_kb_ingestion_stats(university_id: str, config: dict) -> dict:
    """Get latest KB ingestion job statistics from Bedrock Agent.

    Returns aggregate counts across all data sources: ingested, failed, scanned,
    plus the last sync status and timestamp.
    """
    empty = {'ingested': 0, 'failed': 0, 'scanned': 0, 'last_status': None, 'last_sync_at': None}

    if not config:
        return empty

    kb_config = config.get('kb_config')
    if not kb_config:
        return empty

    knowledge_base_id = kb_config.get('knowledge_base_id')
    data_source_ids = kb_config.get('data_source_ids', [])
    if not knowledge_base_id or not data_source_ids:
        return empty

    total_ingested = 0
    total_failed = 0
    total_scanned = 0
    total_new_indexed = 0
    total_modified_indexed = 0
    total_deleted = 0
    last_status = None
    last_sync_at = None

    for ds_id in data_source_ids:
        try:
            resp = bedrock_agent.list_ingestion_jobs(
                knowledgeBaseId=knowledge_base_id,
                dataSourceId=ds_id,
                maxResults=1,
                sortBy={'attribute': 'STARTED_AT', 'order': 'DESCENDING'},
            )
            jobs = resp.get('ingestionJobSummaries', [])
            if not jobs:
                continue

            job = jobs[0]
            stats = job.get('statistics', {})
            scanned = int(stats.get('numberOfDocumentsScanned', 0))
            failed = int(stats.get('numberOfDocumentsFailed', 0))
            new_indexed = int(stats.get('numberOfNewDocumentsIndexed', 0))
            modified_indexed = int(stats.get('numberOfModifiedDocumentsIndexed', 0))
            deleted = int(stats.get('numberOfDocumentsDeleted', 0))

            # numberOfDocumentsIndexed only counts NEW documents in this job.
            # For the true "in KB" count, use scanned - failed (documents that
            # Bedrock successfully processed, whether new or already present).
            total_ingested += max(0, scanned - failed)
            total_failed += failed
            total_scanned += scanned
            total_new_indexed += new_indexed
            total_modified_indexed += modified_indexed
            total_deleted += deleted

            job_status = job.get('status')
            job_time = job.get('updatedAt') or job.get('startedAt')
            if job_time:
                time_str = job_time.isoformat() if hasattr(job_time, 'isoformat') else str(job_time)
                if last_sync_at is None or time_str > (last_sync_at or ''):
                    last_sync_at = time_str
                    last_status = job_status

        except Exception as e:
            logger.warning(f"Failed to get ingestion stats for ds {ds_id}: {e}")

    return {
        'ingested': total_ingested,
        'failed': total_failed,
        'scanned': total_scanned,
        'new_indexed': total_new_indexed,
        'modified_indexed': total_modified_indexed,
        'deleted': total_deleted,
        'last_status': last_status,
        'last_sync_at': last_sync_at,
    }


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
