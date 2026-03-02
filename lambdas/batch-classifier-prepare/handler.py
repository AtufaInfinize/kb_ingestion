"""
Batch Classifier Prepare Lambda

Scans S3 for cleaned markdown files, generates JSONL input for
Bedrock batch inference, and submits batch job(s).

Each Bedrock batch job handles up to 50,000 records. If more pages
exist, multiple jobs are submitted in a single invocation.

Invoke:
    aws lambda invoke --function-name batch-classifier-prepare-dev \
        --cli-binary-format raw-in-base64-out \
        --payload '{"university_id":"gmu"}' /dev/stdout

    # Classify a specific domain only:
    aws lambda invoke --function-name batch-classifier-prepare-dev \
        --cli-binary-format raw-in-base64-out \
        --payload '{"university_id":"gmu","domain":"catalog.gmu.edu"}' /dev/stdout

Event:
{
    "university_id": "gmu",                                    # required
    "domain": "catalog.gmu.edu",                               # optional — scope to one domain
    "model_id": "anthropic.claude-3-haiku-20240307-v1:0",     # optional
    "skip_existing": true,                                     # optional (default: true)
    "max_records_per_job": 50000                               # optional (max: 50000)
}
"""

import json
import os
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
BEDROCK_BATCH_ROLE_ARN = os.environ.get('BEDROCK_BATCH_ROLE_ARN')
DEFAULT_MODEL_ID = os.environ.get('CLASSIFIER_MODEL_ID',
                                   'anthropic.claude-3-haiku-20240307-v1:0')

CONTENT_READ_BYTES = 2000
MAX_RECORDS_PER_JOB = 50000
READ_THREADS = 200

# Same prompt as page-classifier Lambda — keep in sync
CLASSIFICATION_PROMPT = """You are a university web page classifier. Analyze this page and return a JSON response.

Page URL: __URL__
Page Title: __TITLE__
Page Description: __DESCRIPTION__

Content (first 1000 characters):
__CONTENT_PREVIEW__

Rules:
- Pick the MOST specific category that fits the visible content.
- Use "low_value" if the page has no substantive content for prospective students, parents, or researchers.
  Examples: login/auth walls, empty search results, template placeholders (e.g. {{field.value}}), cookie notices, error pages, redirect stubs, or pages with fewer than ~50 words of real content.
- is_useful_page = "yes" only if the page contains meaningful, information-rich content.
- is_high_traffic_page = "yes" only if this is likely a major entry page (homepage, admissions overview, tuition page, financial aid, application pages, major program landing pages, campus visit pages).
- facts: Extract up to 5 SHORT strings (max 60 chars each) that identify WHAT this page is about and WHEN it applies. Only include concrete, explicitly stated values. Never invent or infer. Prioritize in this order:
  1. Academic year or semester (e.g. "Fall 2026", "2025-2026 catalog", "AY 2025-26")
  2. Program, degree, or department name (e.g. "BS Computer Science", "School of Business", "MS Data Analytics")
  3. Deadlines and dates (e.g. "Application deadline: Nov 1, 2025")
  4. Costs, tuition, or fees (e.g. "In-state tuition: $6,250/semester")
  5. Key requirements or contact (e.g. "Minimum GPA: 3.0", "703-993-2400")
  If no concrete facts exist, return an empty array.
- Output must be valid JSON only. No markdown, no explanations.

Return ONLY valid JSON with this exact structure:
{
    "category": "<one of: admissions, financial_aid, academic_programs, course_catalog, student_services, housing_dining, campus_life, athletics, faculty_staff, library, it_services, policies, events, news, about, careers, alumni, low_value, other>",
    "subcategory": "<specific sub-classification, e.g. 'transfer_admissions', 'merit_scholarships', 'computer_science_bs'>",
    "summary": "<1-2 sentence factual summary of ONLY the visible content. Be specific and literal. Do NOT infer intent or add marketing language.>",
    "is_useful_page": "<yes or no>",
    "is_high_traffic_page": "<yes or no>",
    "facts": ["<short string fact>", "<short string fact>"]
}"""


def handler(event, context):
    """Prepare and submit Bedrock batch inference job(s) for page classification."""
    university_id = event['university_id']
    domain = event.get('domain')  # optional — scope to a single domain
    model_id = event.get('model_id', DEFAULT_MODEL_ID)
    skip_existing = event.get('skip_existing', True)
    max_per_job = min(event.get('max_records_per_job', MAX_RECORDS_PER_JOB),
                      MAX_RECORDS_PER_JOB)

    # 1. List .md files and identify unclassified ones
    scope = f"{university_id}/{domain}" if domain else university_id
    logger.info(f"Listing clean-content files for {scope}...")
    md_keys, classified_keys = list_clean_content(university_id, domain=domain)
    logger.info(f"Found {len(md_keys)} .md files, {len(classified_keys)} already classified")

    if skip_existing:
        to_classify = [k for k in md_keys if k not in classified_keys]
    else:
        to_classify = md_keys

    if not to_classify:
        logger.info("No unclassified pages found")
        return {'status': 'done', 'message': 'All pages already classified',
                'total_md': len(md_keys), 'already_classified': len(classified_keys)}

    logger.info(f"Pages to classify: {len(to_classify)}")

    # 2. Load URL metadata from DynamoDB
    logger.info(f"Loading URL metadata from DynamoDB for {scope}...")
    url_lookup = load_url_metadata(university_id, domain=domain)
    logger.info(f"Loaded {len(url_lookup)} URL records")

    # 3. Process in batches of max_per_job, submit one Bedrock job per batch
    jobs = []
    for batch_start in range(0, len(to_classify), max_per_job):
        batch_keys = to_classify[batch_start:batch_start + max_per_job]
        batch_num = batch_start // max_per_job + 1

        logger.info(f"Batch {batch_num}: reading {len(batch_keys)} files...")
        contents = read_files_parallel(batch_keys)
        logger.info(f"Batch {batch_num}: read {len(contents)} files successfully")

        records, manifest_entries = build_records(
            contents, url_lookup, university_id
        )

        if not records:
            logger.warning(f"Batch {batch_num}: no valid records, skipping")
            continue

        job_info = upload_and_submit(
            university_id, model_id, batch_num, records, manifest_entries
        )
        jobs.append(job_info)

    return {
        'status': 'submitted',
        'university_id': university_id,
        'total_to_classify': len(to_classify),
        'jobs': jobs,
    }


def list_clean_content(university_id, domain=None):
    """List .md files and identify which already have metadata sidecars."""
    prefix = f"clean-content/{university_id}/{domain}/" if domain else f"clean-content/{university_id}/"
    md_keys = []
    classified = set()

    paginator = s3.get_paginator('list_objects_v2')
    count = 0
    for page in paginator.paginate(Bucket=BUCKET, Prefix=prefix):
        for obj in page.get('Contents', []):
            key = obj['Key']
            if key.endswith('.md.metadata.json'):
                # Strip .metadata.json suffix to get the .md key
                classified.add(key[:-len('.metadata.json')])
            elif key.endswith('.md'):
                md_keys.append(key)
            count += 1
            if count % 50000 == 0:
                logger.info(f"  Listed {count} objects so far...")

    return md_keys, classified


def load_url_metadata(university_id, domain=None):
    """Build url_hash -> metadata lookup from DynamoDB.

    When domain is specified, uses domain-status-index for faster queries.
    """
    table = dynamodb.Table(URL_REGISTRY_TABLE)
    lookup = {}

    if domain:
        query_kwargs = {
            'IndexName': 'domain-status-index',
            'KeyConditionExpression': '#d = :domain AND crawl_status = :cs',
            'ExpressionAttributeValues': {':domain': domain, ':cs': 'crawled'},
            'ProjectionExpression': '#u, url_hash, #d, #dep, crawled_at, last_crawled_at',
            'ExpressionAttributeNames': {'#u': 'url', '#d': 'domain', '#dep': 'depth'},
        }
    else:
        query_kwargs = {
            'IndexName': 'university-status-index',
            'KeyConditionExpression': 'university_id = :uid',
            'ExpressionAttributeValues': {':uid': university_id},
            'ProjectionExpression': '#u, url_hash, #d, #dep, crawled_at, last_crawled_at',
            'ExpressionAttributeNames': {'#u': 'url', '#d': 'domain', '#dep': 'depth'},
        }

    while True:
        resp = table.query(**query_kwargs)
        for item in resp.get('Items', []):
            h = item.get('url_hash', '')
            if h:
                lookup[h] = item
        if 'LastEvaluatedKey' not in resp:
            break
        query_kwargs['ExclusiveStartKey'] = resp['LastEvaluatedKey']

    return lookup


def read_files_parallel(keys):
    """Read first CONTENT_READ_BYTES of each S3 key using a thread pool."""
    results = {}

    def _read(key):
        try:
            resp = s3.get_object(
                Bucket=BUCKET, Key=key,
                Range=f'bytes=0-{CONTENT_READ_BYTES - 1}'
            )
            return key, resp['Body'].read().decode('utf-8', errors='replace')
        except Exception as e:
            logger.warning(f"Failed to read {key}: {e}")
            return key, None

    with ThreadPoolExecutor(max_workers=READ_THREADS) as pool:
        futures = [pool.submit(_read, k) for k in keys]
        done = 0
        for f in as_completed(futures):
            key, content = f.result()
            if content:
                results[key] = content
            done += 1
            if done % 10000 == 0:
                logger.info(f"  Read {done}/{len(keys)} files...")

    return results


def build_records(contents, url_lookup, university_id):
    """Build JSONL records and manifest entries from file contents."""
    records = []
    manifest = {}
    skipped_no_url = 0

    for s3_key, content in contents.items():
        # Parse key: clean-content/{uid}/{domain}/{hash}.md
        parts = s3_key.split('/')
        if len(parts) < 4:
            continue
        domain = parts[2]
        url_hash = parts[3].replace('.md', '')

        # Look up URL from DynamoDB
        meta = url_lookup.get(url_hash, {})
        url = meta.get('url', '')
        if not url:
            skipped_no_url += 1
            continue

        # Extract title from first line of markdown
        title = ''
        lines = content.split('\n', 2)
        if lines and lines[0].startswith('# '):
            title = lines[0][2:].strip()

        # Content preview (skip title line)
        body = '\n'.join(lines[1:]).strip() if len(lines) > 1 else content.strip()
        preview = body[:1000]

        # Build the classification prompt
        prompt = (CLASSIFICATION_PROMPT
                  .replace('__URL__', url)
                  .replace('__TITLE__', title)
                  .replace('__DESCRIPTION__', '')
                  .replace('__CONTENT_PREVIEW__', preview))

        records.append({
            'recordId': url_hash,
            'modelInput': {
                'anthropic_version': 'bedrock-2023-05-31',
                'max_tokens': 1024,
                'temperature': 0,
                'messages': [{'role': 'user', 'content': prompt}],
            }
        })

        manifest[url_hash] = {
            'url': url,
            'domain': domain,
            'university_id': university_id,
            'clean_s3_key': s3_key,
            'title': title,
            'content_length': len(content),
            'depth': int(meta.get('depth', 0)),
            'crawled_at': meta.get('crawled_at', meta.get('last_crawled_at', '')),
        }

    if skipped_no_url:
        logger.warning(f"Skipped {skipped_no_url} files with no URL in DynamoDB")

    return records, manifest


def upload_and_submit(university_id, model_id, batch_num, records, manifest_entries):
    """Upload JSONL + manifest to S3 and submit Bedrock batch job."""
    ts = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
    job_name = f"classify-{university_id}-{ts}-b{batch_num}"
    base = f"batch-jobs/{university_id}/{job_name}"
    input_key = f"{base}/input.jsonl"
    manifest_key = f"{base}/manifest.json"
    output_uri = f"s3://{BUCKET}/{base}/output/"

    # Upload JSONL — one compact JSON per line
    body = '\n'.join(json.dumps(r, separators=(',', ':')) for r in records)
    s3.put_object(Bucket=BUCKET, Key=input_key, Body=body.encode('utf-8'))
    logger.info(f"Uploaded {input_key} ({len(records)} records, {len(body):,} bytes)")

    # Upload manifest (maps recordId -> page metadata for the process Lambda)
    manifest_doc = {
        'university_id': university_id,
        'model_id': model_id,
        'job_name': job_name,
        'created_at': datetime.now(timezone.utc).isoformat(),
        'total_records': len(records),
        'input_key': input_key,
        'manifest_key': manifest_key,
        'output_uri': output_uri,
        'records': manifest_entries,
    }
    s3.put_object(
        Bucket=BUCKET, Key=manifest_key,
        Body=json.dumps(manifest_doc).encode('utf-8'),
        ContentType='application/json'
    )
    logger.info(f"Uploaded manifest: {manifest_key}")

    # Submit Bedrock batch inference job
    input_uri = f"s3://{BUCKET}/{input_key}"
    try:
        resp = bedrock.create_model_invocation_job(
            jobName=job_name,
            modelId=model_id,
            roleArn=BEDROCK_BATCH_ROLE_ARN,
            inputDataConfig={
                's3InputDataConfig': {
                    's3Uri': input_uri,
                    's3InputFormat': 'JSONL',
                }
            },
            outputDataConfig={
                's3OutputDataConfig': {
                    's3Uri': output_uri,
                }
            },
            tags=[
                {'key': 'university_id', 'value': university_id},
                {'key': 'batch', 'value': str(batch_num)},
            ]
        )
        job_arn = resp['jobArn']
        logger.info(f"Submitted batch job: {job_name} -> {job_arn}")

        return {
            'job_name': job_name,
            'job_arn': job_arn,
            'records': len(records),
            'manifest_key': manifest_key,
        }

    except Exception as e:
        logger.error(f"Failed to submit batch job {job_name}: {e}")
        return {
            'job_name': job_name,
            'error': str(e),
            'records': len(records),
            'manifest_key': manifest_key,
            'input_key': input_key,
        }
