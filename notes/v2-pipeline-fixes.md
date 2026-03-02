# V2 Pipeline Fixes

Issues found during GMU batch classification work and the fixes applied.

---

## Problem: Silent Message Loss

### What happened
- Requeued 182K messages to processing queue at ~600/s
- Content cleaner consumed all messages (queue went to 0)
- Only 39,273 .md files produced — 143K pages "disappeared"
- Only 301 had `processing_status: empty_content`
- The other ~142K had NO `processing_status` at all

### Root cause
Content cleaner handler catches exceptions per-message (line 184) but does NOT
set `processing_status` in the catch block. SQS still deletes the message.
Result: page is silently lost — no .md file, no status, no trace.

### Fix applied (content-cleaner/handler.py)
Set `processing_status` for ALL outcomes:

| Outcome | `processing_status` | When |
|---|---|---|
| Success | `cleaned` | Trafilatura extracted content, .md written, queued for classification |
| Empty | `empty_content` | Trafilatura returned nothing (page has no extractable text) |
| S3 not found | `s3_not_found` | Raw HTML file missing from S3 |
| Error | `cleaning_error` | Exception during processing (+ `last_error` field with detail) |
| PDF skip | _(unchanged)_ | PDFs are handled by pdf-processor |

Now every page that enters the content cleaner leaves a trace in DynamoDB.

---

## Problem: Disconnected Classification Pipeline

### What happened
```
Content Cleaner → Classification Queue → (nobody reads — old classifier disabled)
                                       ↓
                               Messages expire after 4 days

Batch Classifier Prepare → Scans S3 for .md without .metadata.json
                         → Queries ALL of DynamoDB for URL metadata
                         → Reads first 2000 bytes of each .md file
```

The classification queue was being populated but never consumed.
The batch classifier independently re-discovers what needs classification by scanning S3.

### Why batch classifier can't use queue trigger
Bedrock batch inference is inherently a batch operation — you submit a JSONL file
with up to 50K records and wait hours for results. You can't trigger it per-message.

### Current design: Classification queue as monitoring
The classification queue now serves as a **pipeline monitor**:
- Content cleaner pushes every successfully cleaned page to it
- Queue depth = pages waiting for classification
- Check before/after batch classification to verify counts

```bash
# Check how many pages are waiting for classification
aws sqs get-queue-attributes \
    --queue-url https://sqs.us-east-1.amazonaws.com/251221984842/classification-queue-dev \
    --attribute-names ApproximateNumberOfMessages \
    --query 'Attributes.ApproximateNumberOfMessages' --output text
```

**Important:** Messages expire after 4 days (queue retention). Run batch classifier
within 4 days of content cleaning, or use the `processing_status: 'cleaned'`
DynamoDB field as the durable source of truth.

### Future optimization: Queue-driven batch classifier
The classification queue message already contains everything needed for batch classification:
```json
{
  "url": "https://...",
  "university_id": "gmu",
  "domain": "catalog.gmu.edu",
  "url_hash": "fa72387ea4756d7e",
  "clean_s3_key": "clean-content/gmu/catalog.gmu.edu/fa72387ea4756d7e.md",
  "title": "Computer Science Courses",
  "description": "Complete listing of CS courses...",
  "content_preview": "First 1000 chars of markdown",
  "content_length": 81515,
  "depth": 2,
  "crawled_at": "2026-02-16T19:09:30Z"
}
```

A future v3 batch-classifier-prepare could:
1. Drain classification queue messages (instead of scanning S3)
2. Build JSONL directly from message data (no DynamoDB query needed)
3. No S3 reads needed (content_preview is in the message)
4. Submit batch job
5. Delete consumed messages

This eliminates: S3 listing, S3 reads, and the full DynamoDB query.
Not implemented yet — current S3-scan approach works and is simpler to reason about.

---

## Updated processing_status Lifecycle

```
(not set)              ← Page never entered content cleaner (SQS message lost before this fix)
 │
 ├── cleaned           ← Content extracted, .md written, queued for classification  [NEW]
 │   └── classified    ← Batch classifier processed it
 │
 ├── empty_content     ← Trafilatura returned nothing (legitimate — no extractable text)
 ├── s3_not_found      ← Raw HTML missing from S3                                  [NEW]
 ├── cleaning_error    ← Exception during processing (check last_error field)       [NEW]
 └── pdf_processed     ← PDF sidecar written (from pdf-processor)
```

### Debugging with statuses

```bash
# Count pages by processing_status for a university
python3 -c "
import boto3
table = boto3.resource('dynamodb', region_name='us-east-1').Table('url-registry-dev')
# Query all crawled pages
items = []
kwargs = {
    'IndexName': 'university-status-index',
    'KeyConditionExpression': 'university_id = :uid AND crawl_status = :cs',
    'ExpressionAttributeValues': {':uid': 'gmu', ':cs': 'crawled'},
    'ProjectionExpression': 'processing_status',
}
while True:
    resp = table.query(**kwargs)
    items.extend(resp.get('Items', []))
    if 'LastEvaluatedKey' not in resp: break
    kwargs['ExclusiveStartKey'] = resp['LastEvaluatedKey']

from collections import Counter
counts = Counter(item.get('processing_status', '(not set)') for item in items)
for status, count in counts.most_common():
    print(f'  {status}: {count:,}')
print(f'  TOTAL: {len(items):,}')
"
```

---

## Pipeline Tracking: How to verify each stage

### Stage 1: Crawl → Raw HTML
```bash
# How many pages were crawled?
python3 scripts/count_s3_objects.py raw-html/gmu/
```

### Stage 2: Raw HTML → Clean Content
```bash
# How many .md files exist?
python3 scripts/count_s3_objects.py clean-content/gmu/

# How many are missing? (gap analysis)
python3 scripts/requeue_missing_content.py --university-id gmu --dry-run

# What happened to missing pages? Check DynamoDB statuses:
# - processing_status='cleaned' but no .md → S3 write failed (very rare)
# - processing_status='empty_content' → Trafilatura returned nothing
# - processing_status='cleaning_error' → Exception (check last_error)
# - processing_status='s3_not_found' → Raw HTML missing
# - processing_status=(not set) → Never processed (SQS message lost — pre-fix)
```

### Stage 3: Clean Content → Classification Queue
```bash
# How many pages are waiting for classification?
aws sqs get-queue-attributes \
    --queue-url https://sqs.us-east-1.amazonaws.com/251221984842/classification-queue-dev \
    --attribute-names ApproximateNumberOfMessages \
    --query 'Attributes.ApproximateNumberOfMessages' --output text

# Cross-check: count 'cleaned' status in DynamoDB (durable)
python3 -c "
import boto3
table = boto3.resource('dynamodb', region_name='us-east-1').Table('url-registry-dev')
kwargs = {
    'IndexName': 'university-status-index',
    'KeyConditionExpression': 'university_id = :uid AND crawl_status = :cs',
    'ExpressionAttributeValues': {':uid': 'gmu', ':cs': 'crawled'},
    'ProjectionExpression': 'processing_status',
    'FilterExpression': 'processing_status = :ps',
}
kwargs['ExpressionAttributeValues'][':ps'] = 'cleaned'
count = 0
while True:
    resp = table.query(**kwargs)
    count += resp['Count']
    if 'LastEvaluatedKey' not in resp: break
    kwargs['ExclusiveStartKey'] = resp['LastEvaluatedKey']
print(f'Pages with processing_status=cleaned: {count:,}')
"
```

### Stage 4: Classification → Metadata Sidecars
```bash
# How many metadata sidecars exist?
aws s3 ls s3://university-kb-content-251221984842-dev/clean-content/gmu/ --recursive | grep metadata.json | wc -l

# How many .md files lack sidecars? (what batch classifier will process)
aws lambda invoke --function-name batch-classifier-prepare-dev \
    --cli-binary-format raw-in-base64-out \
    --payload '{"university_id":"gmu"}' /dev/stdout
# Check the response: total_to_classify shows unclassified count
```

---

## Problem: Competing SQS Consumers (Root Cause of 130K Missing Pages)

### What happened
- Requeued 614 catalog.gmu.edu pages after deploying content cleaner fix
- Only 42 of 584 messages were actually processed
- Queue showed 0 (all consumed) but most pages had no `processing_status`

### Root cause: TWO bugs
**Bug 1: Competing consumers on same queue**

Both `ContentCleanerFunction` and `PdfProcessorFunction` had SQS event source
mappings on the **same** `ProcessingQueue`. With Standard SQS, each message is
delivered to only one consumer. When PdfProcessor's poller picked up an HTML
message, the filter didn't match → message silently deleted.

**Bug 2: Filter value mismatch**

Crawler worker sends `content_type: "html"` and `"pdf"`.
Content cleaner filter expected `"text/html"`.
PDF processor filter expected `"application/pdf"`.
Neither filter matched crawler messages. Only `{"exists": false}` fallback in
the content cleaner filter caught messages without `content_type` field.

Together these bugs caused ~90% message loss during content cleaning.

### Fix applied

1. **Separate queues**: Created `PdfProcessingQueue` for PDF processing
   - `ProcessingQueue` → ContentCleaner only (HTML)
   - `PdfProcessingQueue` → PdfProcessor only (PDFs)
2. **Crawler routing**: Crawler worker routes PDFs to PDF queue, HTML to processing queue
3. **Removed filters**: No longer needed since each queue has a single consumer
4. **Content cleaner PDF check**: Now handles both `"pdf"` and `"application/pdf"`

### Architecture (before → after)

```
BEFORE (broken):
  Crawler → ProcessingQueue → ContentCleaner (filter: "text/html")
                            → PdfProcessor   (filter: "application/pdf")
  Messages with content_type="html" matched NEITHER → silently deleted

AFTER (fixed):
  Crawler → ProcessingQueue      → ContentCleaner (no filter, sole consumer)
          → PdfProcessingQueue   → PdfProcessor   (no filter, sole consumer)
```

---

## Deployment

After making these changes, deploy:

```bash
cd university-crawler
sam build && sam deploy --capabilities CAPABILITY_NAMED_IAM
```

---

## Fix: EventBridge Auto-Trigger for Batch Classifier

### What happened
EventBridge triggered `batch-classifier-process` when the Bedrock job completed,
but the Lambda exited immediately without processing. The job ARN was empty.

### Root cause
Bedrock sends `batchJobArn` in the EventBridge event detail, but the handler
was looking for `jobArn` and `invocationJobArn`.

```json
// Actual Bedrock EventBridge event detail:
{
  "batchJobArn": "arn:aws:bedrock:...:model-invocation-job/...",
  "batchJobName": "classify-gmu-...",
  "status": "Completed"
}
```

### Fix applied (batch-classifier-process/handler.py)
Updated to check `batchJobArn` first, with fallbacks for other field names.

---

## Batch Classification Pipeline

### How it works

Three components work together:

1. **batch-classifier-prepare** (Lambda, 15min timeout)
   - Lists `.md` files under `clean-content/{university_id}/` (or scoped to a domain)
   - Identifies unclassified pages (no `.metadata.json` sidecar)
   - Reads first 2KB of each file + loads URL metadata from DynamoDB
   - Builds JSONL with Claude Haiku classification prompts
   - Uploads JSONL + manifest to `batch-jobs/{university_id}/{job_name}/`
   - Submits Bedrock batch inference job (up to 50K records per job)

2. **Bedrock batch inference** (async, ~30-60 min)
   - Processes JSONL with Claude 3 Haiku
   - Writes `.jsonl.out` results to S3

3. **batch-classifier-process** (Lambda, auto-triggered by EventBridge)
   - EventBridge rule fires on `"Batch Inference Job State Change"` → `"Completed"`
   - Loads manifest + batch output from S3
   - Parses classification results (category, subcategory, summary, facts)
   - Writes `.metadata.json` sidecars next to each `.md` file
   - Updates DynamoDB: `processing_status → 'classified'`, `page_category`, `classified_at`
   - Stores extracted facts in entity-store table

### Commands

```bash
# Step 1: Submit batch classification for a domain
aws lambda invoke --function-name batch-classifier-prepare-dev \
    --cli-binary-format raw-in-base64-out \
    --payload '{"university_id":"gmu","domain":"catalog.gmu.edu"}' /dev/stdout

# Step 1 (alt): Submit for entire university
aws lambda invoke --function-name batch-classifier-prepare-dev \
    --cli-binary-format raw-in-base64-out \
    --payload '{"university_id":"gmu"}' /dev/stdout

# Step 2: Monitor batch job status
aws bedrock list-model-invocation-jobs \
    --query 'invocationJobSummaries[?contains(jobName,`gmu`)].{Name:jobName,Status:status,Created:submitTime}' \
    --output table --region us-east-1

# Step 2 (alt): Check specific job
aws bedrock get-model-invocation-job \
    --job-identifier <JOB_ARN> \
    --query '{Status:status,Records:inputDataConfig.s3InputDataConfig.s3Uri}' \
    --output json --region us-east-1

# Step 3: Auto-triggered by EventBridge, but can also invoke manually:
aws lambda invoke --function-name batch-classifier-process-dev \
    --cli-binary-format raw-in-base64-out \
    --payload '{"job_arn":"<JOB_ARN>"}' /dev/stdout

# Verify: Count classified pages for a domain
python3 -c "
import boto3
from collections import Counter
table = boto3.resource('dynamodb', region_name='us-east-1').Table('url-registry-dev')
items = []
kwargs = {
    'IndexName': 'domain-status-index',
    'KeyConditionExpression': '#d = :domain AND crawl_status = :cs',
    'ExpressionAttributeValues': {':domain': 'catalog.gmu.edu', ':cs': 'crawled'},
    'ExpressionAttributeNames': {'#d': 'domain'},
    'ProjectionExpression': 'processing_status',
}
while True:
    resp = table.query(**kwargs)
    items.extend(resp.get('Items', []))
    if 'LastEvaluatedKey' not in resp: break
    kwargs['ExclusiveStartKey'] = resp['LastEvaluatedKey']
counts = Counter(item.get('processing_status', '(not set)') for item in items)
for status, count in counts.most_common():
    print(f'  {status}: {count:,}')
print(f'  TOTAL: {len(items):,}')
"
```

### Output artifacts

```
S3:
  batch-jobs/{uid}/{job-name}/input.jsonl       ← Classification prompts
  batch-jobs/{uid}/{job-name}/manifest.json     ← Maps recordId → page metadata
  batch-jobs/{uid}/{job-name}/output/*.jsonl.out ← Bedrock responses

  clean-content/{uid}/{domain}/{hash}.md.metadata.json  ← Sidecar per page

DynamoDB (url-registry):
  processing_status: 'classified'
  page_category: 'course_catalog' | 'academic_programs' | ...
  subcategory: 'computer_science_bs' | ...
  classified_at: ISO timestamp

DynamoDB (entity-store):
  facts extracted per page (up to 5 per page, max 60 chars each)
```

---

## Requeue Workflow: Full Gap-Fill for Missing Pages

### Step 1: Cache missing URLs locally (dry run)

Build the full picture of what's missing across ALL domains. This saves
S3 listings and DynamoDB metadata to `data/gmu/` for fast re-use.

```bash
# All domains — caches to data/gmu/missing_urls.json
# DynamoDB scan takes a few minutes (160K+ items)
python3 scripts/requeue_missing_content.py --university-id gmu --dry-run --refresh

# Single domain — much faster (uses domain-status-index)
python3 scripts/requeue_missing_content.py --university-id gmu --domain catalog.gmu.edu --dry-run --refresh
```

Output saved to:
- `data/gmu/s3_raw_html.json` — all raw HTML hashes (cached)
- `data/gmu/s3_clean_content.json` — all clean content hashes (cached)
- `data/gmu/dynamo_url_metadata.json` — DynamoDB metadata for all pages (cached)
- `data/gmu/missing_urls.json` — final list of pages to requeue

### Step 2: Requeue one domain at a time (recommended)

```bash
# Requeue a specific domain
python3 scripts/requeue_missing_content.py --university-id gmu --domain catalog.gmu.edu \
    --queue-url https://sqs.us-east-1.amazonaws.com/251221984842/processing-queue-dev

# Check processing status after Lambda finishes
python3 -c "
import boto3
from collections import Counter
table = boto3.resource('dynamodb', region_name='us-east-1').Table('url-registry-dev')
items = []
kwargs = {
    'IndexName': 'domain-status-index',
    'KeyConditionExpression': '#d = :domain AND crawl_status = :cs',
    'ExpressionAttributeValues': {':domain': 'catalog.gmu.edu', ':cs': 'crawled'},
    'ExpressionAttributeNames': {'#d': 'domain'},
    'ProjectionExpression': 'processing_status',
}
while True:
    resp = table.query(**kwargs)
    items.extend(resp.get('Items', []))
    if 'LastEvaluatedKey' not in resp: break
    kwargs['ExclusiveStartKey'] = resp['LastEvaluatedKey']
counts = Counter(item.get('processing_status', '(not set)') for item in items)
for status, count in counts.most_common():
    print(f'  {status}: {count:,}')
print(f'  TOTAL: {len(items):,}')
"
```

### Step 3: Requeue all remaining domains at once

```bash
# After validating single domain works, requeue everything
python3 scripts/requeue_missing_content.py --university-id gmu \
    --queue-url https://sqs.us-east-1.amazonaws.com/251221984842/processing-queue-dev
```

### Notes
- Script only requeues pages with NO `processing_status` (skips already-processed pages)
- Use `--refresh` to force re-fetch from AWS (otherwise uses local cache in `data/`)
- Content cleaner max concurrency = 10, batch size = 5 — processes ~50 pages/cycle
- Every page now gets a `processing_status` so we can track what happened

---

## Dashboard API

### Architecture
- Single Lambda (`dashboard-api`) with regex-based route dispatch in `handler.py`
- Each endpoint needs an explicit API Gateway event in `template.yaml` — API Gateway won't forward requests to the Lambda unless the path is registered
- Routes: `routes/stats.py`, `routes/categories.py`, `routes/pipeline.py`, `routes/kb.py`, `routes/classification.py`

### Endpoints (13 total)
| Method | Path | Purpose |
|--------|------|---------|
| GET | `/v1/universities` | List configured universities |
| GET | `/v1/universities/{uid}/stats` | Crawl + processing stats |
| GET | `/v1/universities/{uid}/categories` | Category cards with counts |
| GET | `/v1/universities/{uid}/categories/{cat}/pages` | Paginated page list |
| POST | `/v1/universities/{uid}/categories/{cat}/pages` | Add URLs to category |
| DELETE | `/v1/universities/{uid}/categories/{cat}/pages` | Remove URLs (set excluded) |
| POST | `/v1/universities/{uid}/categories/{cat}/media/upload-url` | Presigned S3 upload for PDFs |
| POST | `/v1/universities/{uid}/pipeline` | Start full pipeline via Step Functions |
| GET | `/v1/universities/{uid}/pipeline` | List job history |
| GET | `/v1/universities/{uid}/pipeline/{job_id}` | Live job progress |
| POST | `/v1/universities/{uid}/kb/sync` | Trigger Bedrock KB ingestion |
| GET | `/v1/universities/{uid}/kb/sync` | KB sync status |
| GET | `/v1/universities/{uid}/classification` | Bedrock batch job progress |

### Key IAM notes
- Bedrock KB operations need both `bedrock:` and `bedrock-agent:` prefixed actions
- `bedrock:ListModelInvocationJobs` needed for classification status endpoint
- Force Lambda cold start after IAM changes: `aws lambda update-function-configuration --function-name dashboard-api-dev --description "force cold start"`

### Performance
- Stats endpoint: uses `university-category-index` GSI sum for classified count (fast) instead of paginated filtered scan (slow, 100K+ items)
- Category counts: parallel `COUNT` queries via ThreadPoolExecutor, one per category
- Lambda timeout: 60s (increased from 30s for stats queries)
