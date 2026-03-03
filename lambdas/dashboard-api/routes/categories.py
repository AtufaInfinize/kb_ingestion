"""Category views and curation endpoints."""

import os
import json
import logging
import hashlib
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

import boto3
from fastapi import APIRouter, Query, Request

from utils.response import api_response
from utils.dynamo import (
    count_all_categories, query_pages_by_category, url_table,
    record_kb_sync_event,
)
from utils.constants import VALID_CATEGORIES, CATEGORY_LABELS

logger = logging.getLogger(__name__)
router = APIRouter()

s3 = boto3.client('s3')
sqs = boto3.client('sqs')

BUCKET = os.environ.get('CONTENT_BUCKET')
CRAWL_QUEUE_URL = os.environ.get('CRAWL_QUEUE_URL')
PROCESSING_QUEUE_URL = os.environ.get('PROCESSING_QUEUE_URL')
PDF_PROCESSING_QUEUE_URL = os.environ.get('PDF_PROCESSING_QUEUE_URL')


@router.get("/universities/{uid}/categories")
def list_categories(uid: str):
    """GET /v1/universities/{uid}/categories — Category cards with counts."""
    try:
        categories_to_count = sorted(VALID_CATEGORIES - {'excluded'})
        counts = count_all_categories(uid, categories_to_count)

        cards = []
        total = 0
        for cat in sorted(counts.keys()):
            count = counts[cat]
            if count > 0:
                cards.append({
                    'category': cat,
                    'label': CATEGORY_LABELS.get(cat, cat),
                    'count': count,
                })
                total += count

        return api_response(200, {
            'university_id': uid,
            'total_pages': total,
            'categories': cards,
        })

    except Exception as e:
        logger.error(f"list_categories error: {e}", exc_info=True)
        return api_response(500, {'error': str(e)})


@router.get("/universities/{uid}/categories/{cat}/pages")
def list_pages(
    uid: str,
    cat: str,
    limit: int = Query(default=50, le=200),
    next_token: Optional[str] = None,
    domain: Optional[str] = None,
    processing_status: Optional[str] = None,
):
    """GET /v1/universities/{uid}/categories/{cat}/pages — Paginated page list."""
    if cat not in VALID_CATEGORIES:
        return api_response(400, {'error': f'Invalid category: {cat}'})

    try:
        items, new_token = query_pages_by_category(
            uid, cat, limit=limit, next_token=next_token,
            domain=domain, processing_status=processing_status,
        )

        pages = []
        for item in items:
            pages.append({
                'url': item.get('url'),
                'url_hash': item.get('url_hash'),
                'domain': item.get('domain'),
                'subcategory': item.get('subcategory'),
                'processing_status': item.get('processing_status'),
                'crawl_status': item.get('crawl_status'),
                'content_type': item.get('content_type'),
                'content_length': item.get('content_length'),
                'last_crawled_at': item.get('last_crawled_at'),
            })

        return api_response(200, {
            'university_id': uid,
            'category': cat,
            'pages': pages,
            'count': len(pages),
            'next_token': new_token,
        })

    except Exception as e:
        logger.error(f"list_pages error: {e}", exc_info=True)
        return api_response(500, {'error': str(e)})


@router.post("/universities/{uid}/categories/{cat}/pages")
async def add_pages(uid: str, cat: str, request: Request):
    """POST /v1/universities/{uid}/categories/{cat}/pages — Add URLs to a category."""
    if cat not in VALID_CATEGORIES:
        return api_response(400, {'error': f'Invalid category: {cat}'})

    try:
        body = await request.json()
    except Exception:
        return api_response(400, {'error': 'Invalid JSON body'})

    urls = body.get('urls', [])
    if not urls:
        return api_response(400, {'error': 'urls list is required'})

    trigger_crawl = body.get('trigger_crawl', True)
    now = datetime.now(timezone.utc).isoformat()
    added = []
    errors = []

    for url in urls:
        try:
            result = _add_url_to_category(url, uid, cat, trigger_crawl, now)
            added.append(result)
        except Exception as e:
            errors.append({'url': url, 'error': str(e)})

    if added:
        record_kb_sync_event(uid, 'category_change')

    return api_response(200, {'added': added, 'errors': errors})


@router.delete("/universities/{uid}/categories/{cat}/pages")
async def remove_pages(uid: str, cat: str, request: Request):
    """DELETE /v1/universities/{uid}/categories/{cat}/pages — Remove URLs."""
    try:
        body = await request.json()
    except Exception:
        return api_response(400, {'error': 'Invalid JSON body'})

    urls = body.get('urls', [])
    if not urls:
        return api_response(400, {'error': 'urls list is required'})

    action = body.get('action', 'mark_excluded')
    now = datetime.now(timezone.utc).isoformat()
    removed = []
    errors = []

    for url in urls:
        try:
            url_table.update_item(
                Key={'url': url},
                UpdateExpression='SET page_category = :cat, manually_curated = :mc, curated_at = :ts',
                ExpressionAttributeValues={
                    ':cat': 'excluded',
                    ':mc': True,
                    ':ts': now,
                },
            )

            if action == 'delete_content':
                _delete_clean_content(url, uid)

            removed.append({'url': url, 'action': action})
        except Exception as e:
            errors.append({'url': url, 'error': str(e)})

    if removed:
        record_kb_sync_event(uid, 'category_change')

    return api_response(200, {'removed': removed, 'errors': errors})


@router.post("/universities/{uid}/categories/{cat}/media/upload-url")
async def get_upload_url(uid: str, cat: str, request: Request):
    """POST /v1/universities/{uid}/categories/{cat}/media/upload-url — Presigned S3 upload."""
    try:
        body = await request.json()
    except Exception:
        return api_response(400, {'error': 'Invalid JSON body'})

    filename = body.get('filename')
    if not filename:
        return api_response(400, {'error': 'filename is required'})

    content_type = body.get('content_type', 'application/pdf')
    safe_name = filename.replace(' ', '_').replace('/', '_')
    s3_key = f'raw-pdf/{uid}/manual/{safe_name}'

    try:
        upload_url = s3.generate_presigned_url(
            'put_object',
            Params={
                'Bucket': BUCKET,
                'Key': s3_key,
                'ContentType': content_type,
            },
            ExpiresIn=3600,
        )

        return api_response(200, {
            'upload_url': upload_url,
            's3_key': s3_key,
            'category': cat,
            'filename': safe_name,
        })

    except Exception as e:
        logger.error(f"get_upload_url error: {e}", exc_info=True)
        return api_response(500, {'error': str(e)})


@router.post("/universities/{uid}/categories/{cat}/media/process")
async def process_uploaded_media(uid: str, cat: str, request: Request):
    """POST /v1/universities/{uid}/categories/{cat}/media/process — Trigger media processing after S3 upload."""
    try:
        body = await request.json()
    except Exception:
        return api_response(400, {'error': 'Invalid JSON body'})

    s3_key = body.get('s3_key')
    filename = body.get('filename', '')
    content_type = body.get('content_type', 'application/pdf')

    if not s3_key:
        return api_response(400, {'error': 's3_key is required'})

    url_hash = hashlib.md5(s3_key.encode()).hexdigest()[:8]
    message = {
        'url': f'manual-upload://{uid}/{filename}',
        'university_id': uid,
        'domain': 'manual',
        's3_key': s3_key,
        'content_type': content_type,
        'url_hash': url_hash,
        'depth': 0,
        'action': 'process',
        'page_category': cat,
        'crawled_at': datetime.now(timezone.utc).isoformat(),
    }

    try:
        sqs.send_message(
            QueueUrl=PDF_PROCESSING_QUEUE_URL,
            MessageBody=json.dumps(message),
        )
        return api_response(200, {'status': 'processing', 's3_key': s3_key})
    except Exception as e:
        logger.error(f"process_uploaded_media error: {e}", exc_info=True)
        return api_response(500, {'error': str(e)})


@router.post("/universities/{uid}/categories/{cat}/rename")
async def rename_category(uid: str, cat: str, request: Request):
    """POST /v1/universities/{uid}/categories/{cat}/rename — Move all pages to a new category."""
    if cat not in VALID_CATEGORIES:
        return api_response(400, {'error': f'Invalid source category: {cat}'})

    try:
        body = await request.json()
    except Exception:
        return api_response(400, {'error': 'Invalid JSON body'})

    new_category = body.get('new_category')
    if not new_category or new_category not in VALID_CATEGORIES:
        return api_response(400, {'error': f'new_category must be one of: {sorted(VALID_CATEGORIES)}'})

    if new_category == cat:
        return api_response(400, {'error': 'new_category must differ from current category'})

    now = datetime.now(timezone.utc).isoformat()
    moved = 0
    errors = []

    kwargs = {
        'IndexName': 'university-category-index',
        'KeyConditionExpression': 'university_id = :uid AND page_category = :cat',
        'ExpressionAttributeValues': {':uid': uid, ':cat': cat},
        'ProjectionExpression': '#u',
        'ExpressionAttributeNames': {'#u': 'url'},
    }

    while True:
        resp = url_table.query(**kwargs)
        for item in resp.get('Items', []):
            try:
                url_table.update_item(
                    Key={'url': item['url']},
                    UpdateExpression='SET page_category = :cat, curated_at = :ts',
                    ExpressionAttributeValues={':cat': new_category, ':ts': now},
                )
                moved += 1
            except Exception as e:
                errors.append({'url': item['url'], 'error': str(e)})

        if 'LastEvaluatedKey' not in resp:
            break
        kwargs['ExclusiveStartKey'] = resp['LastEvaluatedKey']

    if moved > 0:
        record_kb_sync_event(uid, 'category_change')

    return api_response(200, {
        'old_category': cat,
        'new_category': new_category,
        'pages_moved': moved,
        'errors': errors,
    })


@router.post("/universities/{uid}/categories/{cat}/delete")
async def delete_category(uid: str, cat: str, request: Request):
    """POST /v1/universities/{uid}/categories/{cat}/delete — Exclude all pages in a category."""
    if cat not in VALID_CATEGORIES:
        return api_response(400, {'error': f'Invalid category: {cat}'})

    try:
        body = await request.json()
    except Exception:
        body = {}

    target = body.get('target_category', 'excluded')
    if target not in VALID_CATEGORIES:
        return api_response(400, {'error': f'Invalid target_category: {target}'})

    now = datetime.now(timezone.utc).isoformat()
    deleted = 0
    errors = []

    kwargs = {
        'IndexName': 'university-category-index',
        'KeyConditionExpression': 'university_id = :uid AND page_category = :cat',
        'ExpressionAttributeValues': {':uid': uid, ':cat': cat},
        'ProjectionExpression': '#u',
        'ExpressionAttributeNames': {'#u': 'url'},
    }

    while True:
        resp = url_table.query(**kwargs)
        for item in resp.get('Items', []):
            try:
                url_table.update_item(
                    Key={'url': item['url']},
                    UpdateExpression='SET page_category = :cat, curated_at = :ts',
                    ExpressionAttributeValues={':cat': target, ':ts': now},
                )
                deleted += 1
            except Exception as e:
                errors.append({'url': item['url'], 'error': str(e)})

        if 'LastEvaluatedKey' not in resp:
            break
        kwargs['ExclusiveStartKey'] = resp['LastEvaluatedKey']

    if deleted > 0:
        record_kb_sync_event(uid, 'category_change')

    return api_response(200, {
        'category': cat,
        'target_category': target,
        'pages_deleted': deleted,
        'errors': errors,
    })


def _add_url_to_category(url, university_id, category, trigger_crawl, now):
    """Add a single URL to a category. Register + optionally queue for crawl."""
    parsed = urlparse(url)
    domain = parsed.netloc
    url_hash = hashlib.sha256(url.encode()).hexdigest()[:16]

    resp = url_table.get_item(Key={'url': url})
    existing = resp.get('Item')

    if existing:
        crawl_status = existing.get('crawl_status', '')
        url_table.update_item(
            Key={'url': url},
            UpdateExpression='SET page_category = :cat, manually_curated = :mc, curated_at = :ts',
            ExpressionAttributeValues={
                ':cat': category,
                ':mc': True,
                ':ts': now,
            },
        )

        if crawl_status == 'crawled':
            return {'url': url, 'status': 'category_updated'}
        elif trigger_crawl:
            _queue_for_crawl(url, university_id, domain, 0)
            return {'url': url, 'status': 'queued_for_crawl'}
        else:
            return {'url': url, 'status': 'category_updated'}
    else:
        url_table.put_item(Item={
            'url': url,
            'url_hash': url_hash,
            'university_id': university_id,
            'domain': domain,
            'path': parsed.path,
            'crawl_status': 'pending',
            'page_category': category,
            'manually_curated': True,
            'curated_at': now,
            'discovered_at': now,
            'depth': 0,
        })

        if trigger_crawl:
            _queue_for_crawl(url, university_id, domain, 0)
            return {'url': url, 'status': 'registered_and_queued'}
        else:
            return {'url': url, 'status': 'registered'}


def _queue_for_crawl(url, university_id, domain, depth):
    """Push a URL to the crawl queue."""
    sqs.send_message(
        QueueUrl=CRAWL_QUEUE_URL,
        MessageBody=json.dumps({
            'url': url,
            'university_id': university_id,
            'domain': domain,
            'depth': depth,
            'max_crawl_depth': depth + 1,
            'queued_at': datetime.now(timezone.utc).isoformat(),
        }),
        MessageGroupId=f'{domain}#manual',
    )


def _delete_clean_content(url, university_id):
    """Delete clean content .md and .metadata.json from S3 for a URL."""
    resp = url_table.get_item(Key={'url': url}, ProjectionExpression='url_hash, #d',
                              ExpressionAttributeNames={'#d': 'domain'})
    item = resp.get('Item')
    if not item:
        return

    url_hash = item.get('url_hash', '')
    domain = item.get('domain', '')
    prefix = f'clean-content/{university_id}/{domain}/{url_hash}'

    for suffix in ['.md', '.md.metadata.json']:
        try:
            s3.delete_object(Bucket=BUCKET, Key=f'{prefix}{suffix}')
        except Exception:
            pass
