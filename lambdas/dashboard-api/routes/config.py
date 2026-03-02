"""University config CRUD endpoints."""

import os
import json
import logging

import boto3
from fastapi import APIRouter, Request

from utils.response import api_response

logger = logging.getLogger(__name__)
router = APIRouter()

s3 = boto3.client('s3')
BUCKET = os.environ.get('CONTENT_BUCKET')

DEFAULT_CRAWL_CONFIG = {
    'max_concurrent_requests': 10,
    'requests_per_second_per_domain': 3,
    'max_pages': 50000,
    'max_crawl_depth': 5,
    'request_timeout_seconds': 30,
    'exclude_extensions': ['.zip', '.mp4', '.mp3', '.jpg', '.jpeg', '.png', '.gif',
                           '.css', '.js', '.ico', '.svg', '.woff', '.woff2', '.ttf'],
    'pdf_extensions': ['.pdf'],
    'exclude_path_patterns': [r'.*\/wp-admin\/.*', r'.*\/login.*', r'.*\/cart.*'],
    'exclude_domains': [],
    'include_subdomains': True,
}

DEFAULT_FRESHNESS_DAYS = {
    'admissions': 7, 'financial_aid': 7, 'scholarships': 7,
    'academic_programs': 14, 'course_catalog': 14,
    'student_services': 14, 'housing_dining': 14,
    'campus_life': 30, 'athletics': 7,
    'faculty_staff': 30, 'library': 30,
    'events': 3, 'news': 3,
    'about': 60, 'policies': 60,
    'unknown': 14, 'other': 30,
}


@router.get("/universities/{uid}/config")
def get_config(uid: str):
    """GET /v1/universities/{uid}/config — Return full university config."""
    try:
        resp = s3.get_object(Bucket=BUCKET, Key=f'configs/{uid}.json')
        config = json.loads(resp['Body'].read())
        return api_response(200, config)

    except s3.exceptions.NoSuchKey:
        return api_response(404, {'error': f'Config not found for {uid}'})
    except Exception as e:
        logger.error(f"get_config error: {e}", exc_info=True)
        return api_response(500, {'error': str(e)})


@router.post("/universities/{uid}/config")
async def save_config(uid: str, request: Request):
    """POST /v1/universities/{uid}/config — Create or update university config."""
    try:
        body = await request.json()
    except Exception:
        return api_response(400, {'error': 'Invalid JSON body'})

    if not body.get('name'):
        return api_response(400, {'error': 'name is required'})
    if not body.get('root_domain'):
        return api_response(400, {'error': 'root_domain is required'})
    if not body.get('seed_urls'):
        return api_response(400, {'error': 'seed_urls list is required'})

    body['university_id'] = uid

    if 'crawl_config' not in body:
        body['crawl_config'] = DEFAULT_CRAWL_CONFIG.copy()
    if 'freshness_windows_days' not in body:
        body['freshness_windows_days'] = DEFAULT_FRESHNESS_DAYS.copy()
    if 'rate_limits' not in body:
        body['rate_limits'] = {'default_rps': 3}

    crawl_config = body.get('crawl_config', {})
    if not crawl_config.get('allowed_domain_patterns'):
        crawl_config['allowed_domain_patterns'] = [f'*.{body["root_domain"]}']
        body['crawl_config'] = crawl_config

    try:
        s3.put_object(
            Bucket=BUCKET,
            Key=f'configs/{uid}.json',
            Body=json.dumps(body, indent=2),
            ContentType='application/json',
        )

        return api_response(200, {
            'status': 'saved',
            'university_id': uid,
            'config': body,
        })

    except Exception as e:
        logger.error(f"save_config error: {e}", exc_info=True)
        return api_response(500, {'error': str(e)})
