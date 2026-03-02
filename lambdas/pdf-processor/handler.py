"""
Media Metadata Lambda
Creates .metadata.json sidecar files for raw media (PDF, images, audio, video)
so Bedrock Knowledge Bases can filter and return source URLs.

Does NOT copy or move files - Bedrock KB ingests them from raw-pdf/ directly.
The metadata sidecar must be in the same directory as the media file.

Triggered by: pdf-processing-queue (SQS)
Input message format:
{
    "url": "https://www.phc.edu/catalog.pdf",
    "university_id": "phc",
    "domain": "www.phc.edu",
    "s3_key": "raw-pdf/phc/www.phc.edu/d4e5f6a7.pdf",
    "content_type": "application/pdf",
    "crawled_at": "2026-02-16T...",
    "url_hash": "d4e5f6a7",
    "depth": 1,
    "action": "process" | "delete",
    "page_category": "admissions"  (optional — manual uploads include this)
}
"""

import json
import os
import re
import logging
from datetime import datetime, timezone
from urllib.parse import urlparse, unquote

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3 = boto3.client('s3')
dynamodb = boto3.resource('dynamodb')

BUCKET = os.environ.get('CONTENT_BUCKET')
URL_REGISTRY_TABLE = os.environ.get('URL_REGISTRY_TABLE', 'url-registry-dev')

# Media extensions for title inference
_MEDIA_EXT_RE = re.compile(
    r'\.(pdf|jpe?g|png|gif|webp|bmp|tiff|svg|mp3|wav|ogg|flac|aac|m4a|mp4|avi|mov|mkv|webm)$',
    re.IGNORECASE,
)

# Category inference from filename/URL patterns
CATEGORY_PATTERNS = {
    'admissions': ['admission', 'application', 'apply', 'enrollment', 'transfer'],
    'financial_aid': ['financial-aid', 'scholarship', 'tuition', 'fafsa', 'cost', 'fee'],
    'academic_programs': ['catalog', 'curriculum', 'program', 'major', 'degree', 'syllabus'],
    'course_catalog': ['course-catalog', 'course-schedule', 'class-schedule'],
    'student_services': ['student-handbook', 'student-guide', 'orientation'],
    'housing_dining': ['housing', 'residence', 'dining', 'meal'],
    'policies': ['policy', 'handbook', 'compliance', 'regulation', 'code-of-conduct'],
    'athletics': ['athletic', 'sport', 'team-roster'],
    'faculty_staff': ['faculty', 'directory', 'cv', 'resume', 'vita'],
    'events': ['event', 'calendar', 'schedule', 'agenda'],
    'news': ['newsletter', 'press-release', 'announcement'],
    'about': ['annual-report', 'strategic-plan', 'fact-sheet', 'accreditation'],
    'careers': ['job-description', 'employment', 'position'],
    'alumni': ['alumni', 'giving', 'impact-report']
}


def handler(event, context):
    """Process SQS messages containing media file info."""
    results = []

    for record in event.get('Records', []):
        try:
            message = json.loads(record['body'])

            s3_key = message.get('s3_key', '')
            if not s3_key.startswith('raw-pdf/'):
                continue

            result = process_media(message)
            results.append(result)
        except Exception as e:
            logger.error(f"Error processing media: {e}", exc_info=True)
            results.append({'status': 'error', 'error': str(e)})

    succeeded = sum(1 for r in results if r.get('status') == 'success')
    logger.info(f"Media metadata batch: {succeeded} processed")

    return {'results': results}


def _content_type_label(content_type: str) -> str:
    """Derive a short label from a MIME content type."""
    ct = (content_type or '').lower()
    if ct.startswith('image/'):
        return 'image'
    if ct.startswith('audio/'):
        return 'audio'
    if ct.startswith('video/'):
        return 'video'
    if 'pdf' in ct:
        return 'pdf'
    return 'other'


def process_media(message):
    """Create metadata sidecar for a raw media file (PDF, image, audio, video)."""
    action = message.get('action', 'process')
    url = message.get('url', '')
    university_id = message.get('university_id', '')
    domain = message.get('domain', '')
    url_hash = message.get('url_hash', '')
    s3_key = message.get('s3_key', '')
    depth = message.get('depth', 0)
    crawled_at = message.get('crawled_at', '')
    content_type = message.get('content_type', '')

    if action == 'delete':
        return handle_deletion(s3_key)

    if not s3_key:
        return {'status': 'skipped', 'reason': 'no_s3_key', 'url': url}

    # Verify the file exists in S3
    try:
        s3.head_object(Bucket=BUCKET, Key=s3_key)
    except Exception:
        logger.warning(f"Media not found in S3: {s3_key}")
        return {'status': 'skipped', 'reason': 'not_found', 'url': url}

    ct_label = _content_type_label(content_type)
    logger.info(f"Creating metadata for {ct_label}: {url}")

    title = infer_media_title(url)
    # Use explicit category from message (manual uploads), fall back to inference
    category = message.get('page_category') or infer_category(url)

    # Bedrock KB requires: fileName.extension.metadata.json
    metadata_key = f"{s3_key}.metadata.json"

    metadata_doc = {
        "metadataAttributes": {
            "source_url": url,
            "category": category,
            "subcategory": f"{ct_label}_document",
            "title": title,
            "domain": domain,
            "university_id": university_id,
            "depth": depth,
            "content_type": ct_label,
            "last_updated": crawled_at or datetime.now(timezone.utc).isoformat()
        }
    }

    s3.put_object(
        Bucket=BUCKET,
        Key=metadata_key,
        Body=json.dumps(metadata_doc, indent=2).encode('utf-8'),
        ContentType='application/json'
    )
    logger.info(f"Stored metadata: {metadata_key} (category: {category}, type: {ct_label})")

    update_url_registry(url, category)

    return {
        'status': 'success',
        'url': url,
        'metadata_key': metadata_key,
        'category': category,
        'title': title
    }


def infer_media_title(url):
    """Infer a readable title from the media URL."""
    parsed = urlparse(url)
    path = unquote(parsed.path)
    filename = path.split('/')[-1]
    name = _MEDIA_EXT_RE.sub('', filename)
    name = re.sub(r'[-_]+', ' ', name)
    name = re.sub(r'\s+', ' ', name).strip()

    if name == name.lower() or name == name.upper():
        name = name.title()

    return name if name else 'Untitled Media'


def infer_category(url):
    """Infer page category from URL patterns."""
    url_lower = url.lower()
    for category, patterns in CATEGORY_PATTERNS.items():
        if any(p in url_lower for p in patterns):
            return category
    return 'other'


def handle_deletion(s3_key):
    """Delete the metadata sidecar for a removed file."""
    metadata_key = f"{s3_key}.metadata.json"
    try:
        s3.delete_object(Bucket=BUCKET, Key=metadata_key)
        logger.info(f"Deleted metadata: {metadata_key}")
    except Exception as e:
        logger.warning(f"Could not delete {metadata_key}: {e}")

    return {'status': 'success', 'action': 'deleted', 's3_key': s3_key}


def update_url_registry(url, category):
    """Update URL registry with processing status."""
    try:
        table = dynamodb.Table(URL_REGISTRY_TABLE)
        table.update_item(
            Key={'url': url},
            UpdateExpression='SET page_category = :cat, processing_status = :status, classified_at = :ts',
            ExpressionAttributeValues={
                ':cat': category,
                ':status': 'processed_media',
                ':ts': datetime.now(timezone.utc).isoformat()
            }
        )
    except Exception as e:
        logger.warning(f"Could not update URL registry for {url}: {e}")
