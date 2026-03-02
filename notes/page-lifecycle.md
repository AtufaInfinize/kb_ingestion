# Page Lifecycle: Crawl to Classification

Complete journey of a page through the university KB pipeline.

---

## Pipeline Overview

```
Seed Discovery → Crawler → Content Cleaner → Page Classifier → KB Sync
                              (HTML)            (Batch/RT)
                           PDF Processor
                              (PDF)
```

---

## Phase 1: Seed Discovery

**Lambda:** `seed-discovery/handler.py`
**Trigger:** Step Functions state machine

1. Loads university config from `configs/{university_id}.json`
2. Discovers subdomains via Certificate Transparency logs (crt.sh)
3. Fetches and parses robots.txt + sitemap.xml per domain
4. Registers discovered URLs in DynamoDB with `crawl_status: pending`
5. Pushes URLs to crawl queue (FIFO)

**DynamoDB writes:**
| Field | Value |
|---|---|
| `url` | Full URL (HASH key) |
| `url_hash` | 16-char SHA256 prefix |
| `university_id` | e.g. "gmu" |
| `domain` | e.g. "catalog.gmu.edu" |
| `crawl_status` | `"pending"` |
| `discovered_at` | ISO timestamp |
| `content_type` | "html" or "pdf" (inferred from URL) |
| `page_category` | `"unknown"` |
| `retry_count` | `0` |

### Commands

```bash
# Trigger via Step Functions (usually automatic)
aws stepfunctions start-execution \
    --state-machine-arn arn:aws:states:us-east-1:251221984842:stateMachine:crawl-orchestrator-dev \
    --input '{"university_id":"gmu","mode":"full"}'
```

### Debug: Check discovered URLs

```bash
# Count pending URLs for a university
python3 -c "
import boto3
table = boto3.resource('dynamodb', region_name='us-east-1').Table('url-registry-dev')
resp = table.query(
    IndexName='university-status-index',
    KeyConditionExpression='university_id = :uid AND crawl_status = :cs',
    ExpressionAttributeValues={':uid': 'gmu', ':cs': 'pending'},
    Select='COUNT'
)
print(f'Pending URLs: {resp[\"Count\"]}')"
```

---

## Phase 2: Crawling

**Lambda:** `crawler-worker/handler.py`
**Trigger:** SQS FIFO `crawl-queue` (BatchSize: 1)

### Filtering (applied in order)
1. Domain allowlist (`allowed_domain_patterns` from config)
2. Junk URL patterns (search, login, pagination, calendar, tracking)
3. Recently crawled check (skip if crawled within 24h)
4. Robots.txt disallow rules
5. File extension filter (.zip, .mp4, .jpg, .css, .js, etc.)

### On success
- Stores raw content in S3
- Updates DynamoDB with crawl metadata
- Pushes to processing queue

**S3 keys written:**
- HTML: `raw-html/{university_id}/{domain}/{url_hash}.html`
- PDF: `raw-pdf/{university_id}/{domain}/{url_hash}.pdf`

**DynamoDB updates on success:**
| Field | Value |
|---|---|
| `crawl_status` | `"crawled"` |
| `s3_key` | S3 key of stored content |
| `content_hash` | SHA256 of content |
| `content_length` | Bytes |
| `content_type` | "html" or "pdf" |
| `etag` | HTTP ETag header |
| `last_modified` | HTTP Last-Modified header |
| `last_crawled_at` | ISO timestamp |
| `links_found` | Count of new URLs discovered |
| `links_to` | List of internal URLs (max 500) |
| `retry_count` | Reset to 0 |

### crawl_status values
```
pending
 ├── crawled          (success)
 ├── not_modified     (304, timestamp updated)
 ├── skipped_depth    (exceeded max_crawl_depth)
 ├── skipped_off_domain
 ├── skipped_junk     (junk URL pattern)
 ├── blocked_robots   (robots.txt)
 ├── redirected       (followed redirect)
 ├── dead             (404/410, TTL set for 30-day cleanup)
 ├── error            (transient, retry_count incremented)
 └── failed           (3+ retries exhausted)
```

### Commands

```bash
# Check crawl queue depth
aws sqs get-queue-attributes \
    --queue-url https://sqs.us-east-1.amazonaws.com/251221984842/crawl-queue-dev.fifo \
    --attribute-names ApproximateNumberOfMessages ApproximateNumberOfMessagesNotVisible

# Count crawled pages for a university
python3 -c "
import boto3
table = boto3.resource('dynamodb', region_name='us-east-1').Table('url-registry-dev')
resp = table.query(
    IndexName='university-status-index',
    KeyConditionExpression='university_id = :uid AND crawl_status = :cs',
    ExpressionAttributeValues={':uid': 'gmu', ':cs': 'crawled'},
    Select='COUNT'
)
print(f'Crawled: {resp[\"Count\"]}')"

# Count raw-html files in S3
python3 scripts/count_s3_objects.py raw-html/gmu/
```

### Debug: Check a specific URL

```bash
# Look up a URL in DynamoDB
python3 -c "
import boto3, json
table = boto3.resource('dynamodb', region_name='us-east-1').Table('url-registry-dev')
resp = table.get_item(Key={'url': 'https://catalog.gmu.edu/courses/cs/'})
for k, v in sorted(resp.get('Item', {}).items()):
    val = str(v)[:150]
    print(f'  {k}: {val}')"

# Check if raw HTML exists in S3
aws s3 ls s3://university-kb-content-251221984842-dev/raw-html/gmu/catalog.gmu.edu/ | head -20
```

### Debug: Crawler errors

```bash
# Check CloudWatch for crawler errors
aws logs filter-log-events \
    --log-group-name /aws/lambda/crawler-worker-dev \
    --filter-pattern "ERROR" \
    --start-time $(date -v-1H +%s000) \
    --query 'events[].message' --output text | head -50

# Check crawl errors in DynamoDB
python3 -c "
import boto3
table = boto3.resource('dynamodb', region_name='us-east-1').Table('url-registry-dev')
resp = table.query(
    IndexName='university-status-index',
    KeyConditionExpression='university_id = :uid AND crawl_status = :cs',
    ExpressionAttributeValues={':uid': 'gmu', ':cs': 'error'},
    Select='COUNT'
)
print(f'Error URLs: {resp[\"Count\"]}')"
```

---

## Phase 3: Requeue for Processing (Manual)

**Script:** `scripts/requeue_for_processing.py`

Queries DynamoDB for `crawl_status=crawled`, filters, and sends to processing queue.

### Filtering stages
1. **Domain allowlist** — removes off-domain URLs
2. **HTML/PDF separation** — PDFs skip junk filter
3. **Junk URL filter** (HTML only) — search, pagination, login, calendar, tracking params
4. **Content hash dedup** — groups by `content_hash`, keeps best URL per group
   - Best = shallowest depth > shortest URL > alphabetical

### SQS message format (processing queue)
```json
{
  "url": "https://catalog.gmu.edu/courses/cs/",
  "university_id": "gmu",
  "domain": "catalog.gmu.edu",
  "s3_key": "raw-html/gmu/catalog.gmu.edu/fa72387ea4756d7e.html",
  "content_type": "text/html",
  "url_hash": "fa72387ea4756d7e",
  "depth": 2,
  "crawled_at": "2026-02-16T19:09:30Z",
  "action": "process"
}
```

### Commands

```bash
# Dry run (see what would be queued)
python3 scripts/requeue_for_processing.py \
    --university-id gmu --dry-run

# Actually send to queue
python3 scripts/requeue_for_processing.py \
    --university-id gmu \
    --queue-url https://sqs.us-east-1.amazonaws.com/251221984842/processing-queue-dev

# Requeue only missing content (gap-fill)
python3 scripts/requeue_missing_content.py --university-id gmu --dry-run
python3 scripts/requeue_missing_content.py --university-id gmu --domain catalog.gmu.edu \
    --queue-url https://sqs.us-east-1.amazonaws.com/251221984842/processing-queue-dev

# Check processing queue depth
aws sqs get-queue-attributes \
    --queue-url https://sqs.us-east-1.amazonaws.com/251221984842/processing-queue-dev \
    --attribute-names ApproximateNumberOfMessages ApproximateNumberOfMessagesNotVisible
```

### Debug: Messages not arriving in queue

```bash
# Check if SQS send had errors (look at script output for error count)
# If messages were sent but queue shows 0, the content-cleaner Lambda is consuming them immediately

# Verify content cleaner Lambda is active (concurrency > 0)
aws lambda get-function-concurrency --function-name content-cleaner-dev
# No output = no reserved concurrency = uses account default (active)
# Output with value = capped at that value

# Check content cleaner is triggered by processing queue
aws lambda list-event-source-mappings --function-name content-cleaner-dev \
    --query 'EventSourceMappings[].{Queue:EventSourceArn,State:State}'
```

---

## Phase 4A: Content Cleaning (HTML)

**Lambda:** `content-cleaner/handler.py`
**Trigger:** SQS `processing-queue` (filtered: HTML only)
**Config:** BatchSize=5, MaxConcurrency=10

### Processing steps
1. Fetch raw HTML from S3 (`raw-html/{uid}/{domain}/{hash}.html`)
2. Extract clean text via Trafilatura (`output_format='txt'`, timeout=30s, min_output=50 chars)
3. Extract heading structure via Trafilatura XML output
4. Build markdown with title + structured headings
5. Store clean markdown in S3
6. Extract internal links from raw HTML
7. Write `links_to` to DynamoDB (conditional, skip if exists)
8. Push to classification queue

**S3 written:** `clean-content/{university_id}/{domain}/{url_hash}.md`

**DynamoDB updates:**
| Field | Value | When |
|---|---|---|
| `links_to` | List of internal URLs (max 500) | Always (conditional) |
| `processing_status` | `"empty_content"` | Only if Trafilatura returns nothing |
| `processing_updated_at` | ISO timestamp | Only on empty_content |

### Known issue
If `process_message()` throws an exception, the handler-level try/except catches it but does NOT set `processing_status`. SQS still deletes the message. These pages appear as "never processed" — no `.md` file and no `processing_status`.

### Commands

```bash
# Count clean-content files
python3 scripts/count_s3_objects.py clean-content/gmu/

# Count for a specific domain
aws s3 ls s3://university-kb-content-251221984842-dev/clean-content/gmu/catalog.gmu.edu/ --recursive | wc -l

# Check a specific cleaned file
aws s3 cp s3://university-kb-content-251221984842-dev/clean-content/gmu/catalog.gmu.edu/fa72387ea4756d7e.md - | head -50

# Test Trafilatura locally on a raw HTML file
python3 -c "
import boto3, trafilatura
s3 = boto3.client('s3', region_name='us-east-1')
html = s3.get_object(Bucket='university-kb-content-251221984842-dev',
    Key='raw-html/gmu/catalog.gmu.edu/fa72387ea4756d7e.html')['Body'].read().decode()
text = trafilatura.extract(html, include_tables=True, include_links=True)
print(f'Extracted {len(text)} chars' if text else 'No content extracted')
print(text[:500] if text else '')"
```

### Debug: Page missing from clean-content

```bash
# 1. Check if raw HTML exists
aws s3 ls s3://university-kb-content-251221984842-dev/raw-html/gmu/catalog.gmu.edu/fa72387ea4756d7e.html

# 2. Check DynamoDB for processing_status
python3 -c "
import boto3
table = boto3.resource('dynamodb', region_name='us-east-1').Table('url-registry-dev')
item = table.get_item(Key={'url': 'https://catalog.gmu.edu/courses/cs/'}).get('Item', {})
print(f'processing_status: {item.get(\"processing_status\", \"NOT SET\")}')
print(f'crawl_status: {item.get(\"crawl_status\", \"\")}')"
# If processing_status=NOT SET and no .md file → page was never processed (SQS message lost or Lambda errored silently)
# If processing_status=empty_content → Trafilatura returned nothing

# 3. Check CloudWatch for processing attempts (search by URL, not url_hash)
aws logs filter-log-events \
    --log-group-name /aws/lambda/content-cleaner-dev \
    --filter-pattern "catalog.gmu.edu/courses/cs" \
    --start-time $(date -v-7d +%s000) \
    --query 'events[].message' --output text | head -20

# 4. Find gap-fill candidates
python3 scripts/requeue_missing_content.py --university-id gmu --domain catalog.gmu.edu --dry-run
```

### Debug: Content cleaner errors

```bash
# Check for Lambda errors
aws logs filter-log-events \
    --log-group-name /aws/lambda/content-cleaner-dev \
    --filter-pattern "ERROR" \
    --start-time $(date -v-1H +%s000) \
    --query 'events[].message' --output text | head -50

# Check for "No content extracted" (Trafilatura returned nothing)
aws logs filter-log-events \
    --log-group-name /aws/lambda/content-cleaner-dev \
    --filter-pattern "No content extracted" \
    --start-time $(date -v-1d +%s000) \
    --query 'events[].message' --output text | wc -l

# Check Lambda timeout/memory issues
aws logs filter-log-events \
    --log-group-name /aws/lambda/content-cleaner-dev \
    --filter-pattern "Task timed out" \
    --start-time $(date -v-1d +%s000) \
    --query 'events[].message' --output text
```

---

## Phase 4B: PDF Processing

**Lambda:** `pdf-processor/handler.py`
**Trigger:** SQS `processing-queue` (filtered: PDF only)

1. Infers category from filename/URL patterns
2. Writes metadata sidecar to S3

**S3 written:** `raw-pdf/{uid}/{domain}/{hash}.pdf.metadata.json`
**DynamoDB:** Sets `processing_status: "pdf_processed"`

### Commands

```bash
# Count processed PDFs
aws s3 ls s3://university-kb-content-251221984842-dev/raw-pdf/gmu/ --recursive | grep metadata.json | wc -l
```

---

## Phase 5A: Page Classification (Real-time — DISABLED)

**Lambda:** `page-classifier/handler.py`
**Trigger:** SQS `classification-queue`
**Status:** Disabled (`ReservedConcurrentExecutions: 0`)

Classifies individual pages via Bedrock `invoke_model()` (sync).
Replaced by batch classifier for efficiency.

### Commands

```bash
# Verify it's disabled
aws lambda get-function-concurrency --function-name page-classifier-dev
# Should show: ReservedConcurrentExecutions: 0

# To re-enable (if needed)
aws lambda delete-function-concurrency --function-name page-classifier-dev

# To disable
aws lambda put-function-concurrency --function-name page-classifier-dev \
    --reserved-concurrent-executions 0
```

---

## Phase 5B: Page Classification (Batch — PREFERRED)

### Prepare: `batch-classifier-prepare/handler.py`
**Trigger:** Manual invocation

1. Lists `clean-content/{uid}/` for `.md` files
2. Identifies unclassified pages (no `.md.metadata.json` sidecar)
3. Reads first 2000 bytes of each `.md` file (200 threads)
4. Loads URL metadata from DynamoDB via GSI
5. Builds JSONL records with classification prompt
6. Uploads `input.jsonl` + `manifest.json` to S3
7. Submits Bedrock batch inference job (max 50K records per job)

**S3 written:**
- `batch-jobs/{uid}/{job_name}/input.jsonl`
- `batch-jobs/{uid}/{job_name}/manifest.json`

### Process: `batch-classifier-process/handler.py`
**Trigger:** EventBridge (Bedrock batch job completion)

1. Loads manifest from S3
2. Reads `.jsonl.out` files from batch output
3. Parses classification JSON (with fallback parsing)
4. Writes metadata sidecars to S3 (100 threads)
5. Updates DynamoDB with category (50 threads)
6. Stores facts in entity store (batch writer)

**S3 written:** `clean-content/{uid}/{domain}/{hash}.md.metadata.json`

**DynamoDB updates:**
| Field | Value |
|---|---|
| `page_category` | Classification category |
| `subcategory` | Sub-classification |
| `processing_status` | `"classified"` |
| `classified_at` | ISO timestamp |

### Classification categories
`admissions`, `financial_aid`, `academic_programs`, `course_catalog`,
`student_services`, `housing_dining`, `campus_life`, `athletics`,
`faculty_staff`, `library`, `it_services`, `policies`, `events`,
`news`, `about`, `careers`, `alumni`, `low_value`, `other`

### Metadata sidecar format (Bedrock KB compatible)
```json
{
  "metadataAttributes": {
    "source_url": "https://catalog.gmu.edu/courses/cs/",
    "category": "course_catalog",
    "subcategory": "computer_science_courses",
    "summary": "Complete listing of CS courses...",
    "title": "Computer Science Courses",
    "domain": "catalog.gmu.edu",
    "university_id": "gmu",
    "depth": 2,
    "content_length": 81515,
    "last_updated": "2026-02-16T19:09:30Z",
    "is_useful_page": "yes",
    "is_high_traffic_page": "yes",
    "facts": ["BS Computer Science", "Fall 2025-2026 catalog"]
  }
}
```

### Commands

```bash
# Deploy (after code changes)
cd university-crawler
sam build && sam deploy --capabilities CAPABILITY_NAMED_IAM

# Submit batch classification job
aws lambda invoke --function-name batch-classifier-prepare-dev \
    --cli-binary-format raw-in-base64-out \
    --payload '{"university_id":"gmu"}' /dev/stdout

# Submit with skip_existing=false (re-classify all)
aws lambda invoke --function-name batch-classifier-prepare-dev \
    --cli-binary-format raw-in-base64-out \
    --payload '{"university_id":"gmu","skip_existing":false}' /dev/stdout

# Check batch job status
aws bedrock list-model-invocation-jobs \
    --query 'invocationJobSummaries[?contains(jobName,`gmu`)].{Name:jobName,Status:status,Created:submitTime}' \
    --output table

# Check a specific job
aws bedrock get-model-invocation-job --job-identifier <job-arn>

# Manually trigger processing (if EventBridge didn't fire)
aws lambda invoke --function-name batch-classifier-process-dev \
    --cli-binary-format raw-in-base64-out \
    --payload '{"manifest_key":"batch-jobs/gmu/<job_name>/manifest.json"}' /dev/stdout

# Count classified pages (have metadata sidecars)
aws s3 ls s3://university-kb-content-251221984842-dev/clean-content/gmu/ --recursive | grep metadata.json | wc -l
```

### Debug: Batch job issues

```bash
# Check batch job errors
aws bedrock get-model-invocation-job --job-identifier <job-arn> \
    --query '{Status:status,FailureMessage:failureMessage,InputCount:inputDataConfig}'

# Check batch-classifier-prepare Lambda logs
aws logs filter-log-events \
    --log-group-name /aws/lambda/batch-classifier-prepare-dev \
    --filter-pattern "ERROR" \
    --start-time $(date -v-1d +%s000) \
    --query 'events[].message' --output text

# Check batch-classifier-process Lambda logs
aws logs filter-log-events \
    --log-group-name /aws/lambda/batch-classifier-process-dev \
    --filter-pattern "ERROR" \
    --start-time $(date -v-1d +%s000) \
    --query 'events[].message' --output text

# Inspect batch output files
aws s3 ls s3://university-kb-content-251221984842-dev/batch-jobs/gmu/ --recursive | tail -20

# Read a sample output record
aws s3 cp s3://university-kb-content-251221984842-dev/batch-jobs/gmu/<job_name>/output/<file>.jsonl.out - | head -3 | python3 -m json.tool

# Check manifest
aws s3 cp s3://university-kb-content-251221984842-dev/batch-jobs/gmu/<job_name>/manifest.json - | python3 -c "import json,sys; d=json.load(sys.stdin); print(f'Records: {d[\"total_records\"]}, Job: {d[\"job_name\"]}')"
```

---

## processing_status Lifecycle

```
(not set)           ← Page never processed OR content cleaner errored silently
 │
 ├── empty_content  ← Trafilatura returned nothing (legit — page has no extractable text)
 ├── pdf_processed  ← PDF sidecar written
 └── classified     ← Classification complete (batch or real-time)
```

**Gap:** If content cleaner errors (exception), `processing_status` is NOT set.
These pages look identical to "never processed" pages.

---

## S3 Bucket Structure

```
university-kb-content-251221984842-dev/
├── configs/{university_id}.json
├── raw-html/{university_id}/{domain}/{url_hash}.html
├── raw-pdf/{university_id}/{domain}/{url_hash}.pdf
├── raw-pdf/{university_id}/{domain}/{url_hash}.pdf.metadata.json
├── clean-content/{university_id}/{domain}/{url_hash}.md
├── clean-content/{university_id}/{domain}/{url_hash}.md.metadata.json
└── batch-jobs/{university_id}/{job_name}/
    ├── input.jsonl
    ├── manifest.json
    └── output/*.jsonl.out
```

---

## DynamoDB Tables & GSIs

### url-registry-{env}
| Index | Partition Key | Sort Key | Purpose |
|---|---|---|---|
| Primary | `url` | — | Direct lookup |
| `university-status-index` | `university_id` | `crawl_status` | All pages for a university by status |
| `domain-status-index` | `domain` | `crawl_status` | Domain-level queries |
| `status-lastcrawled-index` | `crawl_status` | `last_crawled_at` | Find stale pages |

### entity-store-{env}
| Index | Partition Key | Sort Key | Purpose |
|---|---|---|---|
| Primary | `university_id` | `entity_key` | Facts per university |

---

## SQS Queues

| Queue | Type | Visibility | Retention | Consumer |
|---|---|---|---|---|
| `crawl-queue-{env}.fifo` | FIFO | 5 min | 7 days | crawler-worker |
| `processing-queue-{env}` | Standard | 15 min | 7 days | content-cleaner, pdf-processor |
| `classification-queue-{env}` | Standard | 10 min | 4 days | page-classifier (disabled) |

### Queue URLs (dev)
```
crawl:          https://sqs.us-east-1.amazonaws.com/251221984842/crawl-queue-dev.fifo
processing:     https://sqs.us-east-1.amazonaws.com/251221984842/processing-queue-dev
classification: https://sqs.us-east-1.amazonaws.com/251221984842/classification-queue-dev
```

---

## Quick Health Check

Run these to get overall pipeline status for a university:

```bash
# S3 object counts
python3 scripts/count_s3_objects.py raw-html/gmu/
python3 scripts/count_s3_objects.py clean-content/gmu/

# DynamoDB status counts
python3 -c "
import boto3
table = boto3.resource('dynamodb', region_name='us-east-1').Table('url-registry-dev')
for status in ['pending', 'crawled', 'error', 'failed', 'dead']:
    resp = table.query(
        IndexName='university-status-index',
        KeyConditionExpression='university_id = :uid AND crawl_status = :cs',
        ExpressionAttributeValues={':uid': 'gmu', ':cs': status},
        Select='COUNT'
    )
    print(f'  {status}: {resp[\"Count\"]:,}')"

# Queue depths
for q in processing-queue-dev classification-queue-dev; do
  echo -n "$q: "
  aws sqs get-queue-attributes \
    --queue-url https://sqs.us-east-1.amazonaws.com/251221984842/$q \
    --attribute-names ApproximateNumberOfMessages \
    --query 'Attributes.ApproximateNumberOfMessages' --output text
done

# Gap analysis (missing clean-content)
python3 scripts/requeue_missing_content.py --university-id gmu --dry-run
```

---

## Pending Tasks

- [ ] Requeue ~130K missing pages (saved in `data/gmu/missing_urls.json`)
  - Test with catalog.gmu.edu first (667 pages sent, processing now)
  - Then decide on remaining ~129K based on success rate
- [ ] Fix content cleaner to set `processing_status: "error"` on exception
- [ ] Deploy batch-classifier-prepare with `depth` alias fix
- [ ] Run batch classification after content cleaning completes
