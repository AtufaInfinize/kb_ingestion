"""
Batch Classifier Process Lambda

Processes output from Bedrock batch inference jobs.
Writes metadata sidecars to S3 and updates DynamoDB.

Triggered by:
  - EventBridge rule when a Bedrock batch job completes
  - Manual invocation with manifest_key or job_arn

Manual invoke:
    aws lambda invoke --function-name batch-classifier-process-dev \
        --cli-binary-format raw-in-base64-out \
        --payload '{"manifest_key":"batch-jobs/gmu/.../manifest.json"}' /dev/stdout

Or with job ARN:
    aws lambda invoke --function-name batch-classifier-process-dev \
        --cli-binary-format raw-in-base64-out \
        --payload '{"job_arn":"arn:aws:bedrock:us-east-1:...:model-invocation-job/..."}' /dev/stdout
"""

import json
import os
import re
import logging
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3 = boto3.client('s3')
dynamodb = boto3.resource('dynamodb')
bedrock = boto3.client('bedrock')

BUCKET = os.environ.get('CONTENT_BUCKET')
URL_REGISTRY_TABLE = os.environ.get('URL_REGISTRY_TABLE', 'url-registry-dev')
ENTITY_STORE_TABLE = os.environ.get('ENTITY_STORE_TABLE', 'entity-store-dev')

WRITE_THREADS = 20
DYNAMO_THREADS = 10

VALID_CATEGORIES = {
    'admissions', 'financial_aid', 'academic_programs', 'course_catalog',
    'student_services', 'housing_dining', 'campus_life', 'athletics',
    'faculty_staff', 'library', 'it_services', 'policies',
    'events', 'news', 'about', 'careers', 'alumni', 'other',
    'low_value',
}


def handler(event, context):
    """Process Bedrock batch inference output and write metadata sidecars."""
    manifest_key = None
    job_arn = None

    # Support EventBridge trigger
    # Bedrock sends batchJobArn in the event detail
    if 'detail' in event:
        detail = event['detail']
        job_arn = (detail.get('batchJobArn') or
                   detail.get('jobArn') or
                   detail.get('invocationJobArn', ''))
        status = detail.get('status', '')
        logger.info(f"EventBridge trigger: job={job_arn} status={status}")
        if status not in ('Completed', 'PartiallyCompleted'):
            return {'status': 'skipped', 'reason': f'Job status is {status}'}
    else:
        # Manual invocation
        manifest_key = event.get('manifest_key')
        job_arn = event.get('job_arn')

    # Resolve manifest
    if not manifest_key:
        if not job_arn:
            return {'status': 'error', 'message': 'Provide manifest_key or job_arn'}
        manifest_key = find_manifest_for_job(job_arn)
        if not manifest_key:
            return {'status': 'error',
                    'message': f'Could not find manifest for job {job_arn}'}

    # Load manifest
    logger.info(f"Loading manifest: {manifest_key}")
    manifest = load_json_from_s3(manifest_key)
    university_id = manifest['university_id']
    records_meta = manifest['records']
    output_uri = manifest['output_uri']

    # Load batch output
    logger.info(f"Loading batch output from {output_uri}...")
    output_records = load_batch_output(output_uri)
    logger.info(f"Loaded {len(output_records)} output records")

    if not output_records:
        return {'status': 'error', 'message': 'No output records found',
                'output_uri': output_uri}

    # Process each result
    sidecar_writes = []
    dynamo_updates = []
    fact_writes = []
    succeeded = 0
    failed = 0
    skipped = 0

    for record in output_records:
        record_id = record.get('recordId', '')
        meta = records_meta.get(record_id)
        if not meta:
            logger.warning(f"No manifest entry for recordId={record_id}")
            skipped += 1
            continue

        # Check for model error
        model_output = record.get('modelOutput')
        if not model_output:
            error = record.get('error', {})
            logger.warning(f"Model error for {record_id}: {error}")
            failed += 1
            continue

        # Parse the classification from model response
        raw_text = ''
        content_blocks = model_output.get('content', [])
        if content_blocks:
            raw_text = content_blocks[0].get('text', '')

        classification = parse_classification(raw_text)
        if not classification:
            logger.warning(f"Could not parse classification for {record_id}")
            failed += 1
            continue

        category = classification.get('category', 'other')
        if category not in VALID_CATEGORIES:
            category = 'other'

        # Build fact strings (max 5, max 60 chars each)
        raw_facts = classification.get('facts', [])
        fact_strings = []
        for f in raw_facts[:5]:
            if isinstance(f, str):
                fact_strings.append(f[:60])
            elif isinstance(f, dict):
                fact_strings.append(f.get('value', str(f))[:60])

        # Build metadata sidecar document
        metadata_doc = {
            'metadataAttributes': {
                'source_url': meta['url'],
                'category': category,
                'subcategory': classification.get('subcategory', ''),
                'summary': classification.get('summary', ''),
                'title': meta.get('title', ''),
                'domain': meta['domain'],
                'university_id': university_id,
                'depth': meta.get('depth', 0),
                'content_length': meta.get('content_length', 0),
                'last_updated': meta.get('crawled_at',
                                         datetime.now(timezone.utc).isoformat()),
                'is_useful_page': classification.get('is_useful_page', 'yes'),
                'is_high_traffic_page': classification.get('is_high_traffic_page', 'no'),
                'facts': fact_strings,
            }
        }

        sidecar_key = f"{meta['clean_s3_key']}.metadata.json"
        sidecar_writes.append((sidecar_key, metadata_doc))

        dynamo_updates.append((
            meta['url'], category,
            classification.get('subcategory', '')
        ))

        if fact_strings:
            fact_writes.append((
                university_id, meta['url'], category, fact_strings
            ))

        succeeded += 1

    # Write metadata sidecars to S3
    logger.info(f"Writing {len(sidecar_writes)} metadata sidecars...")
    sidecar_ok = write_sidecars_parallel(sidecar_writes)

    # Update DynamoDB URL registry
    logger.info(f"Updating {len(dynamo_updates)} DynamoDB records...")
    dynamo_ok = update_dynamo_parallel(dynamo_updates)

    # Store facts in entity store
    if fact_writes:
        logger.info(f"Storing facts for {len(fact_writes)} pages...")
        store_facts_batch(fact_writes)

    return {
        'status': 'done',
        'university_id': university_id,
        'total_output_records': len(output_records),
        'classified': succeeded,
        'failed': failed,
        'skipped': skipped,
        'sidecars_written': sidecar_ok,
        'dynamo_updated': dynamo_ok,
    }


# ---------------------------------------------------------------------------
# S3 / Bedrock helpers
# ---------------------------------------------------------------------------

def find_manifest_for_job(job_arn):
    """Derive manifest S3 key from a Bedrock batch job's output config."""
    try:
        job = bedrock.get_model_invocation_job(jobIdentifier=job_arn)
        output_uri = (job.get('outputDataConfig', {})
                         .get('s3OutputDataConfig', {})
                         .get('s3Uri', ''))
        # output_uri: s3://bucket/.../output/
        # manifest:   s3://bucket/.../manifest.json
        prefix = output_uri.replace(f's3://{BUCKET}/', '').rstrip('/')
        if prefix.endswith('/output'):
            manifest_key = prefix[:-len('/output')] + '/manifest.json'
            try:
                s3.head_object(Bucket=BUCKET, Key=manifest_key)
                return manifest_key
            except Exception:
                logger.warning(f"Manifest not found at {manifest_key}")
    except Exception as e:
        logger.error(f"Could not get job details for {job_arn}: {e}")
    return None


def load_json_from_s3(key):
    """Load and parse a JSON file from S3."""
    resp = s3.get_object(Bucket=BUCKET, Key=key)
    return json.loads(resp['Body'].read().decode('utf-8'))


def load_batch_output(output_uri):
    """Load and parse all .jsonl.out files from the batch output prefix."""
    prefix = output_uri.replace(f's3://{BUCKET}/', '')
    records = []

    paginator = s3.get_paginator('list_objects_v2')
    for page in paginator.paginate(Bucket=BUCKET, Prefix=prefix):
        for obj in page.get('Contents', []):
            key = obj['Key']
            if key.endswith('.jsonl.out'):
                logger.info(f"Reading output file: {key} ({obj['Size']:,} bytes)")
                resp = s3.get_object(Bucket=BUCKET, Key=key)
                body = resp['Body'].read().decode('utf-8')
                for line in body.strip().split('\n'):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError as e:
                        logger.warning(f"Invalid JSON in {key}: {e}")

    return records


# ---------------------------------------------------------------------------
# Parallel writers
# ---------------------------------------------------------------------------

def write_sidecars_parallel(writes):
    """Write metadata sidecar files to S3 in parallel."""
    success = 0
    errors = 0

    def _write(item):
        key, doc = item
        try:
            s3.put_object(
                Bucket=BUCKET, Key=key,
                Body=json.dumps(doc, separators=(',', ':')).encode('utf-8'),
                ContentType='application/json'
            )
            return True
        except Exception as e:
            logger.warning(f"Sidecar write failed {key}: {e}")
            return False

    with ThreadPoolExecutor(max_workers=WRITE_THREADS) as pool:
        futures = [pool.submit(_write, w) for w in writes]
        done = 0
        for f in as_completed(futures):
            if f.result():
                success += 1
            else:
                errors += 1
            done += 1
            if done % 10000 == 0:
                logger.info(f"  Sidecars: {done}/{len(writes)} done...")

    logger.info(f"Sidecars: {success} written, {errors} errors")
    return success


def update_dynamo_parallel(updates):
    """Update URL registry with classification results using parallel writes."""
    table = dynamodb.Table(URL_REGISTRY_TABLE)
    now = datetime.now(timezone.utc).isoformat()
    success = 0
    errors = 0

    def _update(item):
        url, category, subcategory = item
        try:
            update_expr = ('SET page_category = :cat, '
                           'processing_status = :status, '
                           'classified_at = :ts')
            expr_values = {
                ':cat': category,
                ':status': 'classified',
                ':ts': now,
            }
            if subcategory:
                update_expr += ', subcategory = :subcat'
                expr_values[':subcat'] = subcategory

            table.update_item(
                Key={'url': url},
                UpdateExpression=update_expr,
                ExpressionAttributeValues=expr_values,
            )
            return True
        except Exception as e:
            logger.warning(f"DynamoDB update failed for {url}: {e}")
            return False

    with ThreadPoolExecutor(max_workers=DYNAMO_THREADS) as pool:
        futures = [pool.submit(_update, u) for u in updates]
        done = 0
        for f in as_completed(futures):
            if f.result():
                success += 1
            else:
                errors += 1
            done += 1
            if done % 10000 == 0:
                logger.info(f"  DynamoDB: {done}/{len(updates)} done...")

    logger.info(f"DynamoDB: {success} updated, {errors} errors")
    return success


def store_facts_batch(fact_writes):
    """Store extracted facts in the entity store table."""
    import hashlib
    table = dynamodb.Table(ENTITY_STORE_TABLE)
    now = datetime.now(timezone.utc).isoformat()

    try:
        with table.batch_writer(overwrite_by_pkeys=['university_id', 'entity_key']) as batch:
            for university_id, url, category, facts in fact_writes:
                url_hash = hashlib.md5(url.encode()).hexdigest()[:12]
                for i, fact in enumerate(facts):
                    if not fact.strip():
                        continue
                    batch.put_item(Item={
                        'university_id': university_id,
                        'entity_key': f"{category}#fact#{url_hash}#{i}",
                        'entity_type': 'fact',
                        'category': category,
                        'key': f'fact_{i}',
                        'value': fact,
                        'source_url': url,
                        'updated_at': now,
                    })
    except Exception as e:
        logger.error(f"Error storing facts: {e}")


# ---------------------------------------------------------------------------
# Classification parsing (same as page-classifier Lambda — keep in sync)
# ---------------------------------------------------------------------------

def parse_classification(raw_text):
    """Robustly parse the LLM classification response into a dict."""
    text = raw_text.strip()

    # Strip markdown code fences
    fence_match = re.search(r'```(?:json)?\s*\n?(.*?)```', text, re.DOTALL)
    if fence_match:
        text = fence_match.group(1).strip()

    # Try direct parse
    try:
        result = json.loads(text)
        if isinstance(result, dict):
            return _normalize(result)
    except json.JSONDecodeError:
        pass

    # Extract outermost { ... } block
    brace_start = text.find('{')
    if brace_start != -1:
        depth = 0
        brace_end = -1
        in_string = False
        escape_next = False
        for i in range(brace_start, len(text)):
            ch = text[i]
            if escape_next:
                escape_next = False
                continue
            if ch == '\\' and in_string:
                escape_next = True
                continue
            if ch == '"' and not escape_next:
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    brace_end = i
                    break

        if brace_end != -1:
            candidate = text[brace_start:brace_end + 1]
            try:
                result = json.loads(candidate)
                if isinstance(result, dict):
                    return _normalize(result)
            except json.JSONDecodeError:
                pass

            # Fix trailing commas
            fixed = re.sub(r',\s*([}\]])', r'\1', candidate)
            try:
                result = json.loads(fixed)
                if isinstance(result, dict):
                    return _normalize(result)
            except json.JSONDecodeError:
                pass

    return _regex_fallback(raw_text)


def _normalize(d):
    """Ensure all expected keys exist with sensible defaults."""
    return {
        'category': d.get('category', 'other'),
        'subcategory': d.get('subcategory', ''),
        'summary': d.get('summary', ''),
        'is_useful_page': str(d.get('is_useful_page', 'no')).lower(),
        'is_high_traffic_page': str(d.get('is_high_traffic_page', 'no')).lower(),
        'facts': d.get('facts') if isinstance(d.get('facts'), list) else [],
    }


def _regex_fallback(text):
    """Extract fields individually when JSON parsing fails entirely."""
    def _extract(key):
        m = re.search(rf'"{key}"\s*:\s*"([^"]*)"', text)
        return m.group(1) if m else ''

    facts = []
    facts_match = re.search(r'"facts"\s*:\s*(\[.*?\])', text, re.DOTALL)
    if facts_match:
        try:
            facts = json.loads(facts_match.group(1))
        except json.JSONDecodeError:
            facts = []

    return {
        'category': _extract('category') or 'other',
        'subcategory': _extract('subcategory'),
        'summary': _extract('summary'),
        'is_useful_page': _extract('is_useful_page') or 'no',
        'is_high_traffic_page': _extract('is_high_traffic_page') or 'no',
        'facts': facts if isinstance(facts, list) else [],
    }
