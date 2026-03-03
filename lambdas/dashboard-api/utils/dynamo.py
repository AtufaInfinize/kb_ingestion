"""Shared DynamoDB query helpers for dashboard API."""

import os
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

import boto3

logger = logging.getLogger()

dynamodb = boto3.resource('dynamodb')
sqs = boto3.client('sqs')

URL_REGISTRY_TABLE   = os.environ.get('URL_REGISTRY_TABLE',   'url-registry-dev')
PIPELINE_JOBS_TABLE  = os.environ.get('PIPELINE_JOBS_TABLE',  'pipeline-jobs-dev')
ENTITY_STORE_TABLE   = os.environ.get('ENTITY_STORE_TABLE',   'entity-store-dev')

url_table    = dynamodb.Table(URL_REGISTRY_TABLE)
jobs_table   = dynamodb.Table(PIPELINE_JOBS_TABLE)
entity_table = dynamodb.Table(ENTITY_STORE_TABLE)

# Categories that participate in crawling (excludes the 'excluded' sentinel)
CRAWLABLE_CATEGORIES = [
    'admissions', 'financial_aid', 'academic_programs', 'course_catalog',
    'student_services', 'housing_dining', 'campus_life', 'athletics',
    'faculty_staff', 'library', 'it_services', 'policies',
    'events', 'news', 'about', 'careers', 'alumni', 'other', 'low_value',
]

DEFAULT_FRESHNESS_DAYS = {cat: 1 for cat in CRAWLABLE_CATEGORIES}

_FRESHNESS_ENTITY_KEY = 'freshness_config'


def load_freshness_windows(university_id: str) -> dict:
    """Read per-category freshness windows from entity-store.

    Returns a dict of {category: days}. Any category not stored explicitly
    gets the default of 1 day.
    """
    resp = entity_table.get_item(
        Key={'university_id': university_id, 'entity_key': _FRESHNESS_ENTITY_KEY}
    )
    item = resp.get('Item')
    if not item:
        return DEFAULT_FRESHNESS_DAYS.copy()
    stored = item.get('windows', {})
    result = DEFAULT_FRESHNESS_DAYS.copy()
    result.update({k: int(v) for k, v in stored.items() if k in result})
    return result


def count_by_category(university_id, category):
    """Count URLs for a university+category using university-category-index GSI."""
    resp = url_table.query(
        IndexName='university-category-index',
        KeyConditionExpression='university_id = :uid AND page_category = :cat',
        ExpressionAttributeValues={':uid': university_id, ':cat': category},
        Select='COUNT',
    )
    return resp['Count']


def count_all_categories(university_id, categories):
    """Count URLs per category in parallel. Returns {category: count}."""
    results = {}
    with ThreadPoolExecutor(max_workers=len(categories)) as pool:
        futures = {
            pool.submit(count_by_category, university_id, cat): cat
            for cat in categories
        }
        for future in as_completed(futures):
            cat = futures[future]
            try:
                results[cat] = future.result()
            except Exception as e:
                logger.warning(f"Count query failed for {cat}: {e}")
                results[cat] = 0
    return results


def count_by_crawl_status(university_id, status):
    """Count URLs by crawl_status using university-status-index GSI."""
    resp = url_table.query(
        IndexName='university-status-index',
        KeyConditionExpression='university_id = :uid AND crawl_status = :cs',
        ExpressionAttributeValues={':uid': university_id, ':cs': status},
        Select='COUNT',
    )
    return resp['Count']


def count_by_processing_status(university_id, processing_status):
    """Count URLs by processing_status (filtered on university-status-index GSI)."""
    count = 0
    kwargs = {
        'IndexName': 'university-status-index',
        'KeyConditionExpression': 'university_id = :uid AND crawl_status = :cs',
        'FilterExpression': 'processing_status = :ps',
        'ExpressionAttributeValues': {
            ':uid': university_id,
            ':cs': 'crawled',
            ':ps': processing_status,
        },
        'Select': 'COUNT',
    }
    while True:
        resp = url_table.query(**kwargs)
        count += resp['Count']
        if 'LastEvaluatedKey' not in resp:
            break
        kwargs['ExclusiveStartKey'] = resp['LastEvaluatedKey']
    return count


def get_crawl_stats(university_id):
    """Get URL counts grouped by crawl_status. Returns dict of status->count."""
    statuses = ['pending', 'crawled', 'error', 'failed', 'dead',
                'redirected', 'blocked_robots', 'skipped_depth']
    results = {}
    with ThreadPoolExecutor(max_workers=len(statuses)) as pool:
        futures = {
            pool.submit(count_by_crawl_status, university_id, s): s
            for s in statuses
        }
        for future in as_completed(futures):
            s = futures[future]
            try:
                results[s] = future.result()
            except Exception:
                results[s] = 0
    return results


def get_processing_stats(university_id):
    """Get processing stats using fast GSI queries instead of slow filtered scans.

    'classified' = sum of all category counts (items with page_category are classified).
    'unprocessed' = crawled - classified (approx, includes cleaned + empty_content).
    """
    from utils.constants import VALID_CATEGORIES

    # Fast: count all categorized items via university-category-index
    categories_to_count = sorted(VALID_CATEGORIES - {'excluded'})
    category_counts = count_all_categories(university_id, categories_to_count)
    classified = sum(category_counts.values())

    # Fast: count crawled via university-status-index (single key query, no filter)
    crawled = count_by_crawl_status(university_id, 'crawled')

    return {
        'classified': classified,
        'unprocessed': max(0, crawled - classified),
    }


def count_total_urls(university_id):
    """Count ALL URLs for a university via university-category-index (partition key only).

    Paginated to handle large result sets correctly. This covers every item
    that has a page_category attribute set (crawler always sets 'unknown' on
    registration, so virtually all items are included).
    """
    from boto3.dynamodb.conditions import Key

    count = 0
    kwargs = {
        'IndexName': 'university-category-index',
        'KeyConditionExpression': Key('university_id').eq(university_id),
        'Select': 'COUNT',
    }
    while True:
        resp = url_table.query(**kwargs)
        count += resp['Count']
        if 'LastEvaluatedKey' not in resp:
            break
        kwargs['ExclusiveStartKey'] = resp['LastEvaluatedKey']
    return count


def get_queue_depth(queue_url):
    """Get approximate number of messages in an SQS queue."""
    try:
        resp = sqs.get_queue_attributes(
            QueueUrl=queue_url,
            AttributeNames=['ApproximateNumberOfMessages', 'ApproximateNumberOfMessagesNotVisible'],
        )
        attrs = resp.get('Attributes', {})
        return {
            'available': int(attrs.get('ApproximateNumberOfMessages', 0)),
            'in_flight': int(attrs.get('ApproximateNumberOfMessagesNotVisible', 0)),
        }
    except Exception as e:
        logger.warning(f"Failed to get queue depth: {e}")
        return {'available': 0, 'in_flight': 0}


def query_pages_by_category(university_id, category, limit=50, next_token=None,
                            domain=None, processing_status=None):
    """Query pages in a category with optional filters. Returns (items, next_token)."""
    from utils.pagination import decode_token, encode_token

    kwargs = {
        'IndexName': 'university-category-index',
        'KeyConditionExpression': 'university_id = :uid AND page_category = :cat',
        'ExpressionAttributeValues': {':uid': university_id, ':cat': category},
        'Limit': limit,
    }

    filter_parts = []
    if domain:
        filter_parts.append('#d = :domain')
        kwargs['ExpressionAttributeValues'][':domain'] = domain
        kwargs.setdefault('ExpressionAttributeNames', {})['#d'] = 'domain'
    if processing_status:
        filter_parts.append('processing_status = :ps')
        kwargs['ExpressionAttributeValues'][':ps'] = processing_status

    if filter_parts:
        kwargs['FilterExpression'] = ' AND '.join(filter_parts)

    if next_token:
        kwargs['ExclusiveStartKey'] = decode_token(next_token)

    resp = url_table.query(**kwargs)
    items = resp.get('Items', [])
    new_token = encode_token(resp.get('LastEvaluatedKey'))

    return items, new_token


def record_kb_sync_event(university_id: str, event_type: str = 'category_change'):
    """Record that a KB-affecting change happened so the dashboard shows 'sync needed'.

    event_type:
      'category_change' — atomically increments categories_modified counter
      'data_reset'      — sets data_reset = true
    """
    try:
        if event_type == 'category_change':
            entity_table.update_item(
                Key={'university_id': university_id, 'entity_key': 'kb_sync_status'},
                UpdateExpression='ADD categories_modified :n SET entity_type = :et',
                ExpressionAttributeValues={':n': 1, ':et': 'crawl_state'},
            )
        elif event_type == 'data_reset':
            entity_table.update_item(
                Key={'university_id': university_id, 'entity_key': 'kb_sync_status'},
                UpdateExpression='SET data_reset = :t, entity_type = :et',
                ExpressionAttributeValues={':t': True, ':et': 'crawl_state'},
            )
    except Exception as e:
        logger.warning(f"Could not record kb_sync_event ({event_type}) for {university_id}: {e}")


def find_running_pipeline(university_id: str):
    """Check if a pipeline is currently running for this university.

    Returns the job_id if found, None otherwise.
    """
    from boto3.dynamodb.conditions import Key, Attr

    try:
        resp = jobs_table.query(
            IndexName='university-created-index',
            KeyConditionExpression=Key('university_id').eq(university_id),
            FilterExpression=Attr('overall_status').eq('running'),
            ScanIndexForward=False,
            Limit=5,
        )
        items = resp.get('Items', [])
        if items:
            return items[0].get('job_id')
    except Exception as e:
        logger.warning(f"Could not check running pipelines for {university_id}: {e}")
    return None
