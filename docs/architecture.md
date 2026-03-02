# University KB Crawler — Architecture Documentation

Complete low-level documentation of the Infinize University Knowledge Base pipeline.

**Stack name:** `uka-crawler-dev`
**Region:** `us-east-1`
**Account:** `251221984842`
**SAM Template:** `university-crawler/template.yaml`

---

## Table of Contents

1. [System Overview](#system-overview)
2. [Pipeline Stages](#pipeline-stages)
3. [AWS Services Inventory](#aws-services-inventory)
4. [Lambda Functions](#lambda-functions)
5. [DynamoDB Tables & GSIs](#dynamodb-tables--gsis)
6. [SQS Queues](#sqs-queues)
7. [S3 Bucket Structure](#s3-bucket-structure)
8. [Step Functions State Machine](#step-functions-state-machine)
9. [EventBridge Rules](#eventbridge-rules)
10. [API Gateway Endpoints](#api-gateway-endpoints)
11. [IAM Roles](#iam-roles)
12. [Data Lifecycle](#data-lifecycle)
13. [Configuration](#configuration)
14. [Build & Deploy](#build--deploy)
15. [Operational Scripts](#operational-scripts)
16. [Monitoring & Debugging](#monitoring--debugging)
17. [Known Issues & Caveats](#known-issues--caveats)

---

## System Overview

The University KB Crawler is a serverless pipeline that:
1. **Discovers** university website URLs via sitemaps, Certificate Transparency logs, and link crawling
2. **Crawls** each page, storing raw HTML/PDF in S3
3. **Cleans** HTML content into markdown via Trafilatura
4. **Classifies** pages into 19 categories using Claude 3 Haiku (Bedrock batch inference)
5. **Syncs** curated content to Amazon Bedrock Knowledge Base for RAG retrieval

The pipeline is fully serverless, event-driven, and designed for multi-university operation.

```
Seed Discovery → Crawler → Content Cleaner → Batch Classifier → KB Sync
                  (HTML)                        (Bedrock Batch)
               Media Processor
                  (PDF/Image/Audio/Video)
```

---

## Pipeline Stages

### Stage 1: Seed Discovery
**Lambda:** `university-seed-discovery-dev`
**Trigger:** Step Functions state machine

1. Loads university config from `configs/{university_id}.json` in S3
2. Discovers subdomains via Certificate Transparency logs (`crt.sh`)
3. Fetches and parses `robots.txt` per domain, caches in DynamoDB
4. Parses `sitemap.xml` files for each discovered domain
5. Registers discovered URLs in `url-registry` with `crawl_status: pending`
6. Pushes URLs to crawl queue (FIFO) with `MessageGroupId` based on `domain#path_prefix`

### Stage 2: Crawling
**Lambda:** `university-crawler-worker-dev`
**Trigger:** SQS FIFO `crawl-queue-dev.fifo` (BatchSize: 1, MaxConcurrency: 10)

1. Pops URL from crawl queue
2. Applies filters in order: domain allowlist → junk URL patterns → freshness check → robots.txt → file extension
3. Fetches page via HTTP(S) with conditional GET (ETag/Last-Modified)
4. Stores raw content in S3 (`raw-html/` or `raw-pdf/`)
5. Updates DynamoDB with crawl metadata (status, content_hash, s3_key, links_found)
6. Discovers new internal links and pushes them back to crawl queue
7. Pushes crawled URL to processing queue

### Stage 3: Content Cleaning (HTML)
**Lambda:** `content-cleaner-dev`
**Trigger:** SQS `processing-queue-dev` (BatchSize: 5, MaxConcurrency: 10)

1. Reads raw HTML from S3
2. Extracts clean text and heading structure via Trafilatura
3. Builds markdown with title + structured headings
4. Writes clean markdown to `clean-content/{uid}/{domain}/{hash}.md`
5. Extracts internal links, writes `links_to` to DynamoDB
6. Pushes to classification queue
7. Sets `processing_status`: `cleaned` (success), `empty_content` (no text), `s3_not_found`, or `cleaning_error`

### Stage 3B: Media Processing
**Lambda:** `pdf-processor-dev`
**Trigger:** SQS `pdf-processing-queue-dev` (BatchSize: 5, MaxConcurrency: 5)

Handles all media types: PDFs, images (jpg, png, gif, webp, bmp), audio (mp3, wav, ogg, flac, aac, m4a), and video (mp4, avi, mov, mkv, webm).

1. For manual uploads, reads `page_category` from the SQS message; falls back to URL pattern inference for crawled files
2. Derives a content type label (pdf/image/audio/video) via `_content_type_label()` helper
3. Infers title from filename, stripping all media extensions
4. Writes metadata sidecar (`.metadata.json`) to S3 with dynamic `content_type` and `subcategory` fields (Bedrock KB format)
5. Sets `processing_status: pdf_processed` in DynamoDB
6. Does NOT parse media content — relies on Bedrock KB's built-in parsers

### Stage 4: Page Classification (Batch)
**Prepare Lambda:** `batch-classifier-prepare-dev`
**Trigger:** Automatic — invoked by crawl orchestrator after crawl + cleaning complete

1. Lists `clean-content/{uid}/` for `.md` files in S3
2. Identifies unclassified pages (no `.md.metadata.json` sidecar)
3. Reads first 2000 bytes of each `.md` file (200 parallel threads)
4. Loads URL metadata from DynamoDB via GSI
5. Builds JSONL with classification prompt for Claude 3 Haiku
6. Splits into jobs of max 50,000 records each
7. Uploads `input.jsonl` + `manifest.json` to `batch-jobs/{uid}/{job_name}/`
8. Submits Bedrock batch inference job via `create_model_invocation_job()`

**Process Lambda:** `batch-classifier-process-dev`
**Trigger:** EventBridge rule `batch-classifier-completion-dev` (on Bedrock batch job completion)

1. Loads manifest from S3 to get record metadata
2. Reads `.jsonl.out` files from batch output in S3
3. Parses classification JSON (with fallback parsing for malformed responses)
4. Writes metadata sidecars to S3 (20 parallel threads)
5. Updates DynamoDB with `page_category`, `subcategory`, `processing_status: classified` (10 parallel threads)
6. Stores extracted facts in `entity-store` table via batch writer

### Stage 5: KB Sync
**Lambda:** `dashboard-api-dev` (route: `POST /v1/universities/{uid}/kb/sync`)

1. Reads `kb_config` from university config in S3
2. Calls `bedrock_agent.start_ingestion_job()` for each data source
3. Bedrock KB crawls the S3 prefixes (`clean-content/` for HTML, `raw-pdf/` for media files)
4. Reads `.metadata.json` sidecars for document attributes
5. Indexes content for RAG retrieval

### Page Classification (Real-time — DISABLED)
**Lambda:** `page-classifier-dev`
**Trigger:** SQS `classification-queue-dev` (DISABLED: `ReservedConcurrentExecutions: 0`)
Replaced by batch classifier. Classifies individual pages via synchronous `bedrock:InvokeModel()`.

---

## AWS Services Inventory

| Service | Resource Count | Purpose |
|---------|---------------|---------|
| Lambda | 10 active functions | Pipeline processing + API + crawl scheduling |
| DynamoDB | 5 tables | State tracking, caching, entity storage |
| SQS | 5 queues + 4 DLQs | Message routing between stages |
| S3 | 1 bucket | Content storage (raw, clean, batch jobs, configs) |
| Step Functions | 1 state machine | Crawl orchestration |
| API Gateway | 1 REST API | Dashboard API (23 endpoints) + Refresh trigger (2 endpoints) |
| EventBridge | 3 rules | Scheduled crawls + batch job completion |
| IAM | 3 custom roles | State machine, EventBridge, Bedrock batch |
| Bedrock | Claude 3 Haiku | Classification (batch inference) |
| Bedrock KB | Per-university | RAG knowledge bases |

---

## Lambda Functions

### 1. university-seed-discovery-dev

| Property | Value |
|----------|-------|
| **Handler** | `handler.lambda_handler` |
| **CodeUri** | `lambdas/seed-discovery/` |
| **Runtime** | Python 3.12 (arm64) |
| **Memory** | 512 MB |
| **Timeout** | 600s (10 min) |
| **Trigger** | Step Functions (DiscoverSeeds state) |
| **Log Group** | `/aws/lambda/university-seed-discovery-dev` |

**Reads:** `configs/{university_id}.json` (S3), `robots-cache` table, `url-registry` table
**Writes:** `url-registry` table (new URLs with `crawl_status: pending`), `robots-cache` table
**Sends to:** `crawl-queue-dev.fifo`
**External calls:** `crt.sh` (CT logs), HTTP fetch for `robots.txt` and `sitemap.xml`

### 2. university-crawler-worker-dev

| Property | Value |
|----------|-------|
| **Handler** | `handler.lambda_handler` |
| **CodeUri** | `lambdas/crawler-worker/` |
| **Runtime** | Python 3.12 (arm64) |
| **Memory** | 1024 MB |
| **Timeout** | 300s (5 min) |
| **Trigger** | SQS `crawl-queue-dev.fifo` (BatchSize: 1, MaxConcurrency: 10) |
| **Log Group** | `/aws/lambda/university-crawler-worker-dev` |

**Reads:** `url-registry`, `robots-cache`, `crawl-rate-limits` tables
**Writes:** `url-registry` table, S3 `raw-html/` and `raw-pdf/`
**Sends to:** `processing-queue-dev`, `pdf-processing-queue-dev`, `crawl-queue-dev.fifo` (new links)
**External calls:** HTTP(S) page fetch (urllib3, timeout 30s, 1 retry)

**Filtering pipeline (in order):**
1. Domain allowlist from config
2. Junk URL patterns (40+ regex: search, login, pagination, calendar, tracking params)
3. Freshness check (skip if crawled within 24h)
4. robots.txt disallow rules
5. File extension filter (30+ binary types)

**crawl_status transitions:**
```
pending → crawled          (success, stores HTML/PDF in S3)
        → not_modified     (304 response, only updates timestamp)
        → skipped_depth    (exceeded max_crawl_depth)
        → skipped_off_domain
        → skipped_junk     (matched junk URL pattern)
        → blocked_robots   (robots.txt disallow)
        → redirected       (followed redirect, registers new URL)
        → dead             (404/410, TTL set for 30-day cleanup)
        → error            (transient error, retry_count++)
        → failed           (3+ retries exhausted)
```

**Rate limiting:** Token bucket per domain (default 3 req/s), stored in `crawl-rate-limits` table.

### 3. university-crawl-orchestrator-dev

| Property | Value |
|----------|-------|
| **Handler** | `handler.lambda_handler` |
| **CodeUri** | `lambdas/crawl-orchestrator/` |
| **Runtime** | Python 3.12 (arm64) |
| **Memory** | 512 MB |
| **Timeout** | 900s (15 min) |
| **Trigger** | Step Functions (multiple states) |
| **Log Group** | `/aws/lambda/university-crawl-orchestrator-dev` |

**Task-based dispatch (via `event.task` field):**

| Task | Purpose |
|------|---------|
| `validate_request` | Validates university_id, refresh_mode, loads config |
| `queue_stale_urls` | Queries URLs past freshness window, re-queues for crawl |
| `check_progress` | Checks crawl queue + processing queue depths; auto-marks orphaned pending URLs as error when crawl queue fully drains |
| `trigger_classification` | Invokes batch-classifier-prepare Lambda, updates pipeline-jobs record |
| `generate_summary` | Generates crawl statistics, writes summary to S3 |

**`check_progress` orphan handling:** If the crawl queue is fully drained (`available=0, in_flight=0`) but some URLs still have `crawl_status=pending`, those messages were lost to the crawl DLQ. `_mark_orphaned_pending_urls()` marks them as `error` so the pipeline is not blocked forever.

**Completion condition:** `is_complete=true` only when all three conditions hold:
- `crawl_queue == 0` (crawl queue available + in-flight)
- `pending_count == 0` (no URLs in `crawl_status=pending`)
- `processing_queue == 0` (processing queue available + in-flight)

**Reads:** `url-registry` (via `university-status-index` GSI), `configs/` from S3, crawl queue + processing queue depths (SQS via `SQSPollerPolicy`), `entity-store-dev` (`kb_sync_status.pages_changed` in `generate_summary`)
**Writes:** `url-registry` (SET crawl_status=pending for stale URLs, SET crawl_status=error for orphans), `crawl-summaries/` to S3, `pipeline-jobs` (classify_stage update), `entity-store-dev` (see below)
**Sends to:** `crawl-queue-dev.fifo` (via `SQSSendMessagePolicy`)
**Invokes:** `batch-classifier-prepare-dev` (synchronous, via `LambdaInvokePolicy`)

**entity-store-dev writes:**
- `validate_request` task: writes `current_crawl_mode` entity (`refresh_mode` + `started_at`); resets `kb_sync_status` with `pages_changed = 0` and `status = "running"`
- `generate_summary` task: reads `pages_changed` from `kb_sync_status`; writes final `status` (`"pending_sync"` if `pages_changed > 0`, else `"no_changes"`); writes `crawl_completed_at`

**IAM policies:**
- `S3CrudPolicy` (ContentBucket)
- `DynamoDBCrudPolicy` (UrlRegistryTable, RobotsCacheTable, EntityStoreTable, PipelineJobsTable)
- `SQSSendMessagePolicy` (CrawlQueue)
- `SQSPollerPolicy` (CrawlQueue — for `GetQueueAttributes` in `check_progress`)
- `SQSPollerPolicy` (ProcessingQueue — for `GetQueueAttributes` in `check_progress`)
- `LambdaInvokePolicy` (BatchClassifierPrepareFunction)

### 4. university-refresh-trigger-dev

| Property | Value |
|----------|-------|
| **Handler** | `handler.lambda_handler` |
| **CodeUri** | `lambdas/refresh-trigger/` |
| **Runtime** | Python 3.12 (arm64) |
| **Memory** | 256 MB |
| **Timeout** | 60s |
| **Trigger** | API Gateway (2 endpoints) |
| **Log Group** | `/aws/lambda/university-refresh-trigger-dev` |

**Endpoints:**
- `POST /crawl/refresh` — Start new crawl (starts Step Functions execution)
- `GET /crawl/status/{execution_id}` — Get crawl execution status

**Reads:** `url-registry` (via `university-status-index` GSI for stats)
**External calls:** Step Functions `start_execution()`, `describe_execution()`, `list_executions()`
Prevents concurrent crawls for the same university.

### 5. content-cleaner-dev

| Property | Value |
|----------|-------|
| **Handler** | `handler.handler` |
| **CodeUri** | `lambdas/content-cleaner/` |
| **Runtime** | Python 3.12 (arm64) |
| **Memory** | 3008 MB |
| **Timeout** | 300s (5 min) |
| **Trigger** | SQS `processing-queue-dev` (BatchSize: 5, MaxConcurrency: 10, ReportBatchItemFailures) |
| **Log Group** | `/aws/lambda/content-cleaner-dev` |

**Reads:** S3 `raw-html/{uid}/{domain}/{hash}.html`, existing `clean-content/{uid}/{domain}/{hash}.md` (for change detection), `entity-store-dev` (`current_crawl_mode` entity — cached per Lambda execution context)
**Writes:** S3 `clean-content/{uid}/{domain}/{hash}.md`, `url-registry` table (processing_status, links_to), `entity-store-dev` (`kb_sync_status` — atomic `pages_changed` increment on incremental crawls)
**Does NOT send to:** `classification-queue-dev` (page-classifier is disabled; batch classifier is used instead)

**Content-change detection (incremental-aware):** Before writing the new `.md` file, the handler reads the existing `.md` from S3 and byte-compares it with the newly extracted content. Behavior on content change depends on the active crawl mode read from the `current_crawl_mode` entity in `entity-store-dev`:

- **Full crawl + content changed:** Deletes the `.md.metadata.json` classification sidecar (same as previous behavior). The page will be re-classified on the next batch-classifier run.
- **Incremental crawl + content changed:** Does NOT delete the `.md.metadata.json` sidecar (preserves existing category, avoids unnecessary re-classification). Instead, atomically increments the `pages_changed` counter in the `kb_sync_status` entity via `ADD pages_changed :1`.
- **Unchanged pages (either mode):** No sidecar deletion and no counter increment.

**IAM policies include:** `entity-store-dev (DynamoDBCrudPolicy)`

**processing_status values set:**
| Value | Meaning |
|-------|---------|
| `cleaned` | Trafilatura extracted content, .md written |
| `empty_content` | Trafilatura returned nothing (no extractable text) |
| `s3_not_found` | Raw HTML file missing from S3 |
| `cleaning_error` | Exception during processing (+ `last_error` detail) |

**Trafilatura config:** 60s timeout, 50 byte minimum output, includes tables and links.

**Build requirement:** Must be built with `--use-container` (Docker/Colima) because trafilatura/lxml include C extensions that must be compiled for Linux arm64. A native macOS build produces a ~17KB deployment package that fails at runtime; the correct Docker build produces ~33MB.

### 6. page-classifier-dev (DISABLED)

| Property | Value |
|----------|-------|
| **Handler** | `handler.handler` |
| **CodeUri** | `lambdas/page-classifier/` |
| **Runtime** | Python 3.12 (arm64) |
| **Memory** | 512 MB |
| **Timeout** | 300s |
| **Trigger** | SQS `classification-queue-dev` (DISABLED: ReservedConcurrentExecutions=0) |
| **Log Group** | `/aws/lambda/page-classifier-dev` |

Replaced by batch classifier. Classifies individual pages via synchronous `bedrock:InvokeModel()`.
**External calls:** Bedrock `invoke_model()` with Claude 3 Haiku

### 7. pdf-processor-dev (Media Processor)

| Property | Value |
|----------|-------|
| **Handler** | `handler.handler` |
| **CodeUri** | `lambdas/pdf-processor/` |
| **Runtime** | Python 3.12 (arm64) |
| **Memory** | 512 MB |
| **Timeout** | 120s (2 min) |
| **Trigger** | SQS `pdf-processing-queue-dev` (BatchSize: 5, MaxConcurrency: 5, ReportBatchItemFailures) |
| **Log Group** | `/aws/lambda/pdf-processor-dev` |

Generalized to handle all media types (PDFs, images, audio, video), not just PDFs. The gate condition is `s3_key.startswith('raw-pdf/')`.

**Reads:** S3 `raw-pdf/` via `head_object()` (verifies media file exists)
**Writes:** S3 `raw-pdf/{uid}/{domain}/{hash}.metadata.json` (dynamic sidecar with `content_type` and `subcategory` fields), `url-registry` table
For manual uploads, reads `page_category` from the SQS message; for crawled files, infers category from URL patterns. Uses `_content_type_label()` helper to derive type (pdf/image/audio/video). `infer_media_title()` strips all media extensions from filename. Does NOT parse media content — only creates metadata sidecars.

### 8. batch-classifier-prepare-dev

| Property | Value |
|----------|-------|
| **Handler** | `handler.handler` |
| **CodeUri** | `lambdas/batch-classifier-prepare/` |
| **Runtime** | Python 3.12 (arm64) |
| **Memory** | 2048 MB |
| **Timeout** | 900s (15 min) |
| **Trigger** | Automatic: invoked by crawl-orchestrator `trigger_classification` task |
| **Log Group** | `/aws/lambda/batch-classifier-prepare-dev` |

**Key env vars:** `BEDROCK_BATCH_ROLE_ARN`, `CLASSIFIER_MODEL_ID` (anthropic.claude-3-haiku-20240307-v1:0)
**Reads:** S3 `clean-content/{uid}/` listing, first 2000 bytes of each `.md` file (200 parallel threads), `url-registry` via GSIs
**Writes:** S3 `batch-jobs/{uid}/{job_name}/input.jsonl`, `batch-jobs/{uid}/{job_name}/manifest.json`
**External calls:** `bedrock.create_model_invocation_job()`, `iam:PassRole` for `BedrockBatchRole`

**Job naming convention:** `classify-{university_id}-{YYYYMMDD-HHMMSS}-b{batch_num}`
**Max records per job:** 50,000
**Payload:** `{"university_id": "gmu"}` or `{"university_id": "gmu", "skip_existing": false}`

### 9. batch-classifier-process-dev

| Property | Value |
|----------|-------|
| **Handler** | `handler.handler` |
| **CodeUri** | `lambdas/batch-classifier-process/` |
| **Runtime** | Python 3.12 (arm64) |
| **Memory** | 2048 MB |
| **Timeout** | 900s (15 min) |
| **Trigger** | EventBridge rule `batch-classifier-completion-dev` |
| **Log Group** | `/aws/lambda/batch-classifier-process-dev` |

**Reads:** S3 `batch-jobs/{uid}/{job_name}/manifest.json`, `batch-jobs/{uid}/{job_name}/output/*.jsonl.out`
**Writes:** S3 `clean-content/{uid}/{domain}/{hash}.md.metadata.json` (20 threads), `url-registry` (10 threads), `entity-store` (batch writer)
**External calls:** `bedrock.get_model_invocation_job()` to resolve output S3 URI

**Concurrency tuning:** `WRITE_THREADS=20`, `DYNAMO_THREADS=10` (reduced from 100/50 to prevent DynamoDB throttling)

### 11. crawl-scheduler-dev

| Property | Value |
|----------|-------|
| **Handler** | `handler.lambda_handler` |
| **CodeUri** | `lambdas/crawl-scheduler/` |
| **Runtime** | Python 3.12 (arm64) |
| **Memory** | 256 MB |
| **Timeout** | 60s |
| **Trigger** | EventBridge `daily-incremental-crawl-dev` + `weekly-full-crawl-dev` |
| **Log Group** | `/aws/lambda/crawl-scheduler-dev` |

Replaces the old hardcoded EventBridge → Step Functions rules that targeted a single university (`phc`). Instead, this Lambda fans out to all configured universities dynamically.

**Behavior:**
1. Lists all university configs from S3 `configs/` prefix to discover all university IDs
2. For each university: skips if a pipeline execution is already running (checks `pipeline-jobs-dev` via `university-created-index` GSI)
3. Reads `freshness_config` entity from `entity-store-dev` to check `incremental_enabled`/`full_enabled` flags for the requested `refresh_mode`
4. For incremental mode: additionally checks staleness before triggering
5. Calls Step Functions `start_execution()` only for eligible universities

**Reads:** S3 `configs/` prefix, `entity-store-dev` (`freshness_config` entity per university), `pipeline-jobs-dev` GSI (`university-created-index`) for running check
**Invokes:** Step Functions `start_execution()`
**Input event format:** `{"refresh_mode": "incremental"}` or `{"refresh_mode": "full"}`

### 10. dashboard-api-dev

| Property | Value |
|----------|-------|
| **Handler** | `handler.lambda_handler` |
| **CodeUri** | `lambdas/dashboard-api/` |
| **Runtime** | Python 3.12 (arm64) |
| **Memory** | 512 MB |
| **Timeout** | 60s |
| **Trigger** | API Gateway (23 endpoints) |
| **Log Group** | `/aws/lambda/dashboard-api-dev` |

**Code structure:**
```
lambdas/dashboard-api/
  handler.py              # FastAPI app + Mangum adapter (lambda_handler = Mangum(app))
  routes/
    __init__.py
    stats.py              # GET /v1/universities, GET /v1/universities/{uid}/stats
    config.py             # GET/POST /v1/universities/{uid}/config
    categories.py         # GET/POST/DELETE categories + pages, rename, delete, presigned upload
    pipeline.py           # POST/GET pipeline, GET pipeline/{job_id}
    reset.py              # POST /v1/universities/{uid}/reset
    kb.py                 # POST/GET kb/sync
    freshness.py          # GET/POST /v1/universities/{uid}/freshness
    classification.py     # GET /classification (Bedrock batch job status)
  utils/
    __init__.py
    response.py           # api_response() helper with CORS headers (GET,POST,DELETE,OPTIONS)
    dynamo.py             # Parallel DynamoDB COUNT queries, pagination
    pagination.py         # Base64 encode/decode for DynamoDB LastEvaluatedKey
    constants.py          # VALID_CATEGORIES, CATEGORY_LABELS
```

**Framework:** FastAPI + Mangum adapter. `handler.lambda_handler = Mangum(app, lifespan="off")`. FastAPI handles routing, request parsing, and validation; Mangum adapts the API Gateway event/context to ASGI.

**Key design pattern:** Each API Gateway path must be explicitly registered as an event in `template.yaml` — API Gateway won't forward requests to paths that have no matching resource. The FastAPI routes handle the actual dispatch internally.

**Auto-generated docs:** FastAPI serves `/docs` (Swagger UI), `/redoc`, and `/openapi.json` automatically. These paths are registered as API Gateway events pointing to the same Lambda.

---

## DynamoDB Tables & GSIs

### url-registry-dev

Primary table tracking every discovered URL's lifecycle.

| Property | Value |
|----------|-------|
| **Table Name** | `url-registry-dev` |
| **Billing** | PAY_PER_REQUEST (on-demand) |
| **Primary Key** | `url` (HASH) — full URL string |
| **TTL** | `ttl` attribute (used for dead URL cleanup) |
| **PITR** | Enabled |

**Attributes (non-exhaustive):**

| Attribute | Type | Set By | Description |
|-----------|------|--------|-------------|
| `url` | S | seed-discovery | Full URL (primary key) |
| `url_hash` | S | seed-discovery | SHA256[:16] of URL |
| `university_id` | S | seed-discovery | University identifier |
| `domain` | S | seed-discovery | Domain of URL |
| `crawl_status` | S | crawler-worker | Current crawl state |
| `page_category` | S | batch-classifier-process | Classification category |
| `subcategory` | S | batch-classifier-process | Sub-classification |
| `processing_status` | S | content-cleaner / batch-classifier | Processing state |
| `content_type` | S | seed-discovery / crawler | "html" or "pdf" |
| `content_hash` | S | crawler-worker | SHA256 of page content |
| `content_length` | N | crawler-worker | Content size in bytes |
| `s3_key` | S | crawler-worker | S3 key of raw content |
| `last_crawled_at` | S | crawler-worker | ISO timestamp |
| `classified_at` | S | batch-classifier-process | ISO timestamp |
| `discovered_at` | S | seed-discovery | ISO timestamp |
| `depth` | N | seed-discovery / crawler | Crawl depth from seed |
| `retry_count` | N | crawler-worker | Failed crawl attempts |
| `links_to` | L | content-cleaner | Internal links (max 500) |
| `etag` | S | crawler-worker | HTTP ETag header |
| `last_modified` | S | crawler-worker | HTTP Last-Modified |
| `ttl` | N | crawler-worker | TTL for dead URLs (30 days) |

**Global Secondary Indexes:**

| Index Name | Partition Key | Sort Key | Projection | Purpose |
|------------|--------------|----------|------------|---------|
| `domain-status-index` | `domain` | `crawl_status` | ALL | Domain-level queries |
| `status-lastcrawled-index` | `crawl_status` | `last_crawled_at` | ALL | Find stale pages |
| `university-status-index` | `university_id` | `crawl_status` | ALL | University-level crawl stats |
| `university-category-index` | `university_id` | `page_category` | INCLUDE* | Category cards, page listings |

*`university-category-index` includes: `url`, `url_hash`, `domain`, `subcategory`, `processing_status`, `crawl_status`, `content_type`, `content_length`, `last_crawled_at`, `s3_key`

### entity-store-dev

Structured facts extracted during classification for direct lookup.

| Property | Value |
|----------|-------|
| **Table Name** | `entity-store-dev` |
| **Billing** | PAY_PER_REQUEST |
| **Primary Key** | `university_id` (HASH) + `entity_key` (RANGE) |

**entity_key format (classification facts):** `{category}#fact#{url_hash}#{index}` — still valid for extracted facts written by `batch-classifier-process-dev`.

**Pipeline operational entity keys (new):**

| entity_key | Written By | Purpose |
|------------|-----------|---------|
| `freshness_config` | Dashboard API `POST /v1/universities/{uid}/freshness` | Per-category freshness windows (days) + schedule enable/disable flags. Fields: `windows` (dict of category→days), `incremental_enabled` (bool), `full_enabled` (bool), `updated_at` |
| `current_crawl_mode` | Crawl Orchestrator `validate_request` task | Tracks the active pipeline's `refresh_mode` so `content-cleaner` knows whether to delete classification sidecars. Fields: `refresh_mode`, `started_at` |
| `kb_sync_status` | Crawl Orchestrator `validate_request` + `generate_summary` tasks; Dashboard `POST /v1/universities/{uid}/kb/sync` | Tracks whether a KB sync is needed after a crawl. Fields: `status` (`"running"` \| `"pending_sync"` \| `"no_changes"` \| `"syncing"`), `pages_changed` (atomic counter incremented by content-cleaner on incremental crawls), `crawl_started_at`, `crawl_completed_at`, `synced_at` |

| Index Name | Partition Key | Sort Key | Projection |
|------------|--------------|----------|------------|
| `entity-type-index` | `university_id` | `entity_type` | ALL |

### crawl-rate-limits-dev

Token bucket rate limiting per domain.

| Property | Value |
|----------|-------|
| **Table Name** | `crawl-rate-limits-dev` |
| **Billing** | PAY_PER_REQUEST |
| **Primary Key** | `domain` (HASH) |

### robots-cache-dev

Cached robots.txt rules per domain.

| Property | Value |
|----------|-------|
| **Table Name** | `robots-cache-dev` |
| **Billing** | PAY_PER_REQUEST |
| **Primary Key** | `domain` (HASH) |
| **TTL** | `ttl` attribute |

### pipeline-jobs-dev

Tracks full pipeline lifecycle (crawl+clean+classify) as a single job.

| Property | Value |
|----------|-------|
| **Table Name** | `pipeline-jobs-dev` |
| **Billing** | PAY_PER_REQUEST |
| **Primary Key** | `job_id` (HASH) |
| **TTL** | `ttl` attribute (90-day auto-delete) |

**Attributes:**

| Attribute | Type | Description |
|-----------|------|-------------|
| `job_id` | S | `{university_id}-{refresh_mode}-{YYYYMMDD-HHMMSS}` |
| `university_id` | S | University identifier |
| `created_at` | S | ISO timestamp |
| `refresh_mode` | S | "full", "incremental", or "domain" |
| `overall_status` | S | "running", "completed", "failed" |
| `execution_arn` | S | Step Functions execution ARN |
| `crawl_stage` | M | `{status, started_at, completed_at, total, completed, failed, queue}` |
| `clean_stage` | M | `{status, started_at, completed_at, completed, queue}` |
| `classify_stage` | M | `{status, started_at, completed_at, completed, queue}` |
| `ttl` | N | Auto-delete timestamp |

| Index Name | Partition Key | Sort Key | Projection |
|------------|--------------|----------|------------|
| `university-created-index` | `university_id` | `created_at` | ALL |

---

## SQS Queues

### Main Queues

| Queue Name | Type | Visibility | Retention | DLQ | Consumer | Max Concurrency |
|------------|------|-----------|-----------|-----|----------|----------------|
| `crawl-queue-dev.fifo` | FIFO | 360s (6 min) | 7 days | `crawl-dlq-dev.fifo` | `crawler-worker` | 10 |
| `processing-queue-dev` | Standard | 900s (15 min) | 7 days | `processing-dlq-dev` | `content-cleaner` | 10 |
| `pdf-processing-queue-dev` | Standard | 900s (15 min) | 7 days | `pdf-processing-dlq-dev` | `pdf-processor` | 5 | Handles all media types (PDFs, images, audio, video) despite the queue name |
| `classification-queue-dev` | Standard | 600s (10 min) | 4 days | `classification-dlq-dev` | `page-classifier` (DISABLED) | 10 |

### Dead Letter Queues (DLQ)

| DLQ Name | Type | Retention | Source Queue | Max Receive Count | Purpose |
|----------|------|-----------|-------------|-------------------|---------|
| `crawl-dlq-dev.fifo` | FIFO | 14 days | `crawl-queue-dev.fifo` | 3 | URLs that failed crawling 3 times (timeouts, transient HTTP errors, Lambda crashes) |
| `processing-dlq-dev` | Standard | 14 days | `processing-queue-dev` | 3 | Clean content messages that failed 3 times (Lambda crash, timeout, malformed HTML) |
| `pdf-processing-dlq-dev` | Standard | 14 days | `pdf-processing-queue-dev` | 3 | Media processing messages (PDFs, images, audio, video) that failed 3 times |

**Note:** `classification-dlq-dev` exists in the template but is effectively unused since the real-time page classifier is disabled (`ReservedConcurrentExecutions: 0`). The batch classifier does not use SQS.

**DLQ behavior:** When a message is received from the source queue and the consumer fails to delete it (Lambda error, timeout, or explicit failure via `ReportBatchItemFailures`), the message becomes visible again after the visibility timeout. After `maxReceiveCount` (3) failed processing attempts, SQS automatically moves the message to the DLQ instead of making it visible again.

**Monitoring DLQ:**
```bash
# Check crawl DLQ depth
aws sqs get-queue-attributes \
  --queue-url https://sqs.us-east-1.amazonaws.com/251221984842/crawl-dlq-dev.fifo \
  --attribute-names ApproximateNumberOfMessages \
  --query 'Attributes.ApproximateNumberOfMessages' --output text

# Peek at DLQ messages (non-destructive, returns up to 10)
aws sqs receive-message \
  --queue-url https://sqs.us-east-1.amazonaws.com/251221984842/crawl-dlq-dev.fifo \
  --max-number-of-messages 10 \
  --visibility-timeout 0 \
  --query 'Messages[].Body' --output text | python3 -m json.tool
```

**Content-cleaner DLQ interaction with check_progress:** When messages land in `processing-dlq-dev`, the main `processing-queue-dev` depth eventually reaches 0. The crawl-orchestrator's `check_progress` only considers `is_complete=true` when `crawl_queue==0 AND pending_count==0 AND processing_queue==0`, so DLQ'd messages no longer block pipeline completion.

The content cleaner uses `ReportBatchItemFailures` to selectively retry only failed messages within a batch (partial batch failures).

### Message Flow

```
crawl-queue ──→ crawler-worker ──→ processing-queue ──→ content-cleaner ──→ (no queue; batch classifier polls S3)
   │                │              (3 failures) ──→ processing-dlq
   │                └→ pdf-processing-queue ──→ pdf-processor (all media types)
   │                   (3 failures) ──→ pdf-processing-dlq
   │
   └─ (3 failures) ──→ crawl-dlq
```

**FIFO queue deduplication:** `crawl-queue` uses content-based deduplication. `MessageGroupId` is `{domain}#{path_prefix}` to ensure per-domain ordering.

---

## S3 Bucket Structure

**Bucket:** `university-kb-content-251221984842-dev`

```
university-kb-content-251221984842-dev/
├── configs/
│   ├── gmu.json                                    # University configuration
│   └── phc.json
├── raw-html/{university_id}/{domain}/{url_hash}.html       # Raw crawled HTML
├── raw-pdf/{university_id}/{domain}/{url_hash}.pdf         # Raw crawled PDFs
├── raw-pdf/{university_id}/{domain}/{url_hash}.pdf.metadata.json  # Media metadata sidecar
├── raw-pdf/{university_id}/manual/{filename}               # Manually uploaded media (PDFs, images, audio, video)
├── clean-content/{university_id}/{domain}/{url_hash}.md    # Cleaned markdown
├── clean-content/{university_id}/{domain}/{url_hash}.md.metadata.json  # Classification sidecar
├── batch-jobs/{university_id}/{job_name}/
│   ├── input.jsonl                                 # Batch inference input
│   ├── manifest.json                               # Record metadata for post-processing
│   └── output/*.jsonl.out                          # Bedrock batch output
└── crawl-summaries/{university_id}/{timestamp}.json        # Crawl completion summaries
```

**Lifecycle rules:**
- `raw-html/` → Intelligent Tiering after 30 days
- Non-current versions expire after 90 days
- Versioning enabled

**Metadata sidecar format (Bedrock KB compatible):**
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

---

## Step Functions State Machine

**Name:** `university-crawl-orchestrator-dev`
**ARN:** `arn:aws:states:us-east-1:251221984842:stateMachine:university-crawl-orchestrator-dev`

### State Flow

```
                    ┌──────────────────┐
                    │ ValidateRequest  │
                    │ (orchestrator)   │
                    └────────┬─────────┘
                             │
                    ┌────────▼─────────┐
                    │ IsRequestValid?  │──── false ──→ CrawlFailed
                    └────────┬─────────┘
                             │ true
                    ┌────────▼─────────┐
                    │ CheckRefreshMode │
                    └───┬──────────┬───┘
                   full │          │ incremental
           ┌────────────▼┐    ┌───▼──────────┐
           │DiscoverSeeds│    │QueueStaleUrls│
           │(seed-discov)│    │(orchestrator)│
           └──────┬──────┘    └──────┬───────┘
                  │                   │
                  └────────┬──────────┘
                  ┌────────▼─────────┐
              ┌──→│  WaitForCrawl    │ (120 seconds)
              │   └────────┬─────────┘
              │   ┌────────▼─────────┐
              │   │CheckCrawlProgress│
              │   │ (orchestrator)   │
              │   └────────┬─────────┘
              │   ┌────────▼─────────┐
              │   │IsCrawlComplete?  │
              │   └───┬──────────┬───┘
              │  false│          │ true
              └───────┘ ┌────────▼─────────┐
                        │ ShouldClassify   │
                        └───┬──────────┬───┘
                 incremental│          │ full/default
                            │ ┌────────▼──────────────┐
                            │ │TriggerClassification  │
                            │ │ (orchestrator)        │
                            │ └────────┬──────────────┘
                            │          │
                            └──────────┤
                        ┌─────────────▼──┐
                        │GenerateSummary │
                        │ (orchestrator) │
                        └───────┬────────┘
                        ┌───────▼────────┐
                        │PipelineComplete│
                        └────────────────┘
```

### States Detail

| State | Type | Lambda | Task Parameter | ResultPath |
|-------|------|--------|---------------|------------|
| ValidateRequest | Task | crawl-orchestrator | `validate_request` | `$.validation` |
| IsRequestValid | Choice | — | Checks `$.validation.is_valid == "true"` | — |
| CheckRefreshMode | Choice | — | Branches on `$.validation.refresh_mode` | — |
| DiscoverSeeds | Task | seed-discovery | `university_id`, `refresh_mode`, `domain_filter` | `$.discovery` |
| QueueStaleUrls | Task | crawl-orchestrator | `queue_stale_urls` | `$.queued` |
| WaitForCrawl | Wait | — | 120 seconds | — |
| CheckCrawlProgress | Task | crawl-orchestrator | `check_progress` | `$.progress` |
| IsCrawlComplete | Choice | — | Checks `$.progress.is_complete == "true"` | — |
| ShouldClassify | Choice | — | Branches on `$.validation.refresh_mode`. If `incremental` → GenerateSummary (skips classification); default → TriggerClassification (full crawl only) | — |
| TriggerClassification | Task | crawl-orchestrator | `trigger_classification` | `$.classification` |
| GenerateSummary | Task | crawl-orchestrator | `generate_summary` | `$.summary` |
| PipelineComplete | Succeed | — | — | — |
| CrawlFailed | Fail | — | Error: "CrawlError" | — |

**Error handling:** All Task states catch `States.ALL` → `CrawlFailed` with `ResultPath: $.error`. TriggerClassification catches errors and falls through to GenerateSummary (classification failure is non-fatal).

**Polling loop:** WaitForCrawl → CheckCrawlProgress → IsCrawlComplete loops every 120 seconds until **both** the crawl queue AND processing queue are empty and no pending URLs remain. This ensures content cleaning completes before classification is triggered.

**Classification trigger:** Uses `$$.Execution.Name` (Step Functions context) to get the `job_id`, which matches the pipeline-jobs DynamoDB record. Invokes batch-classifier-prepare synchronously and updates the pipeline-jobs record with `classify_triggered: true`.

---

## EventBridge Rules

### 1. batch-classifier-completion-dev
| Property | Value |
|----------|-------|
| **Event Source** | `aws.bedrock` |
| **Detail Type** | `Batch Inference Job State Change` |
| **Status Filter** | `Completed`, `PartiallyCompleted` |
| **Target** | `batch-classifier-process-dev` Lambda |

### 2. daily-incremental-crawl-dev
| Property | Value |
|----------|-------|
| **Schedule** | `cron(0 2 * * ? *)` — 2:00 AM UTC daily |
| **Target** | `crawl-scheduler-dev` Lambda |
| **Input** | `{"refresh_mode": "incremental"}` |

Previously targeted Step Functions directly with a hardcoded `{"university_id": "phc", "refresh_mode": "incremental"}`. Now targets `crawl-scheduler-dev` which handles all configured universities.

### 3. weekly-full-crawl-dev
| Property | Value |
|----------|-------|
| **Schedule** | `cron(0 3 ? * SUN *)` — 3:00 AM UTC every Sunday |
| **Target** | `crawl-scheduler-dev` Lambda |
| **Input** | `{"refresh_mode": "full"}` |

Previously named `weekly-seed-discovery-dev` and targeted Step Functions directly with a hardcoded `{"university_id": "phc", "refresh_mode": "full"}`. Now targets `crawl-scheduler-dev` which handles all configured universities.

---

## API Gateway Endpoints

**API Name:** `university-crawler-api-dev`
**Base URL:** `https://9mwsknkorc.execute-api.us-east-1.amazonaws.com/dev`
**CORS:** Enabled (allow all origins, GET/POST/DELETE/OPTIONS)

### Refresh Trigger (refresh-trigger Lambda)

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/crawl/refresh` | Start new crawl via Step Functions |
| GET | `/crawl/status/{execution_id}` | Get crawl execution status |

### Dashboard API (dashboard-api Lambda)

| Method | Path | Handler | Purpose |
|--------|------|---------|---------|
| GET | `/v1/universities` | `stats.list_universities` | List configured universities |
| GET | `/v1/universities/{uid}/stats` | `stats.get_stats` | Crawl + processing overview |
| GET | `/v1/universities/{uid}/config` | `config.get_config` | Return full university config JSON from S3 |
| POST | `/v1/universities/{uid}/config` | `config.save_config` | Create/update university config in S3 |
| GET | `/v1/universities/{uid}/categories` | `categories.list_categories` | Category cards with counts |
| GET | `/v1/universities/{uid}/categories/{cat}/pages` | `categories.list_pages` | Paginated page list |
| POST | `/v1/universities/{uid}/categories/{cat}/pages` | `categories.add_pages` | Add URLs to category |
| DELETE | `/v1/universities/{uid}/categories/{cat}/pages` | `categories.remove_pages` | Exclude URLs from category |
| POST | `/v1/universities/{uid}/categories/{cat}/rename` | `categories.rename_category` | Move all pages in category to a new category |
| POST | `/v1/universities/{uid}/categories/{cat}/delete` | `categories.delete_category` | Mark all pages in category as `excluded` |
| POST | `/v1/universities/{uid}/categories/{cat}/media/upload-url` | `categories.get_upload_url` | Presigned S3 upload for all media types (dynamic `content_type` from request body) |
| POST | `/v1/universities/{uid}/categories/{cat}/media/process` | `categories.process_media` | Sends SQS message to pdf-processing queue with file info + explicit `page_category` |
| POST | `/v1/universities/{uid}/pipeline` | `pipeline.start_pipeline` | Start full pipeline |
| GET | `/v1/universities/{uid}/pipeline` | `pipeline.list_jobs` | Job history (newest first) |
| GET | `/v1/universities/{uid}/pipeline/{job_id}` | `pipeline.get_job_status` | Live pipeline progress |
| POST | `/v1/universities/{uid}/reset` | `reset.reset_university` | Reset all page statuses + clear S3 clean-content/batch-jobs for a fresh full re-crawl |
| POST | `/v1/universities/{uid}/kb/sync` | `kb.start_sync` | Trigger Bedrock KB ingestion |
| GET | `/v1/universities/{uid}/kb/sync` | `kb.get_sync_status` | KB sync status |
| GET | `/v1/universities/{uid}/freshness` | `freshness.get_freshness` | Per-category freshness windows + schedule enable/disable flags |
| POST | `/v1/universities/{uid}/freshness` | `freshness.save_freshness` | Save freshness windows and schedule flags (stored in entity-store `freshness_config` entity) |
| GET | `/v1/universities/{uid}/classification` | `classification.get_classification_status` | Bedrock batch job progress |
| GET | `/docs` | FastAPI auto-generated | Interactive Swagger UI for all endpoints |
| GET | `/redoc` | FastAPI auto-generated | ReDoc API documentation |
| GET | `/openapi.json` | FastAPI auto-generated | OpenAPI 3.x schema |

**Config endpoint validation (`POST /config`):** Requires `university_id`, `name`, `root_domain`, `seed_urls`. Fills defaults for `crawl_config` and `freshness_windows_days` if missing.

**Reset endpoint (`POST /reset`):** Accepts optional body `{"reset_s3": true}`. When `reset_s3=true`, deletes all objects under `clean-content/{uid}/` and `batch-jobs/{uid}/` in S3 in addition to resetting DynamoDB `crawl_status`→`pending` and clearing `processing_status`. When `reset_s3=false` (default), only resets DynamoDB. Returns `{"reset_count": N, "s3_deleted": N}`.

**rename/delete category endpoints:** Query all pages in the category via `university-category-index` GSI, then batch-update `page_category`. `rename` moves pages to any valid category; `delete` moves pages to `excluded`.

### Pagination

Paginated endpoints accept `?limit=N&next_token=TOKEN` query parameters.
- `next_token` is a base64-encoded DynamoDB `LastEvaluatedKey`
- Default limit: 50, max: 100 (varies by endpoint)

### Dashboard API Performance Notes

- **Category cards:** Fire ~19 parallel `query(Select='COUNT')` calls against `university-category-index` GSI using ThreadPoolExecutor. Returns in <1 second.
- **Stats endpoint:** Uses sum of category counts for "classified" count (fast) instead of paginated filtered scan through 100K+ items.
- **Pipeline status:** Augments stored job record with live DynamoDB counts + SQS queue depths. Detects stage transitions automatically.
- **Stats endpoint (`GET /v1/universities/{uid}/stats`):** Also returns `pending_kb_sync` (bool — true when a crawl changed pages and a KB sync is needed), `pages_changed_last_crawl` (int — count of pages changed in the last incremental crawl), and `crawl_completed_at` (str — ISO timestamp of last crawl completion). These fields come from the `kb_sync_status` entity in `entity-store-dev`.

---

## IAM Roles

### 1. crawl-statemachine-role-dev
**Trust:** `states.amazonaws.com`
**Permissions:** `lambda:InvokeFunction` on SeedDiscoveryFunction, CrawlOrchestratorFunction

### 2. eventbridge-sfn-role-dev
**Trust:** `events.amazonaws.com`
**Permissions:** `states:StartExecution` on CrawlStateMachine

### 3. bedrock-batch-role-dev
**Trust:** `bedrock.amazonaws.com`
**Permissions:** `s3:GetObject`, `s3:PutObject` on ContentBucket/*, `s3:ListBucket` on ContentBucket

---

## Data Lifecycle

### URL Lifecycle (crawl_status)

```
(not in DynamoDB)
    │
    ▼ seed-discovery registers URL
 pending ─────────────────────────────────────────────────────┐
    │                                                          │
    ├── crawled ──→ content cleaned ──→ classified             │
    │                                                          │
    ├── not_modified (304, timestamp updated)                  │
    ├── skipped_depth (exceeded max_crawl_depth)               │
    ├── skipped_off_domain                                     │
    ├── skipped_junk (junk URL pattern)                        │
    ├── blocked_robots (robots.txt disallow)                   │
    ├── redirected (follows redirect, registers new URL)       │
    ├── dead (404/410, TTL=30 days) ──→ auto-deleted           │
    ├── error (transient, retry_count++) ──→ pending (retry)   │
    └── failed (3+ retries exhausted) ─────────────────────────┘
```

### Processing Lifecycle (processing_status)

```
(not set)           ← crawled but never processed
    │
    ├── cleaned         ← Trafilatura extracted content, .md written
    ├── empty_content   ← Trafilatura returned nothing
    ├── s3_not_found    ← Raw HTML missing from S3
    ├── cleaning_error  ← Exception during cleaning
    ├── pdf_processed   ← PDF sidecar written
    └── classified      ← Classification complete (batch or real-time)
```

### Classification Categories (19)

`admissions`, `financial_aid`, `academic_programs`, `course_catalog`, `student_services`, `housing_dining`, `campus_life`, `athletics`, `faculty_staff`, `library`, `it_services`, `policies`, `events`, `news`, `about`, `careers`, `alumni`, `low_value`, `other`

Special value: `excluded` — admin-removed pages, not synced to KB.
Special value: `unknown` — default for newly registered URLs.

---

## Configuration

University configs are stored in S3 at `configs/{university_id}.json`.

### Config Structure

```json
{
  "university_id": "gmu",
  "name": "George Mason University",
  "root_domain": "gmu.edu",
  "seed_urls": ["https://www.gmu.edu", "https://catalog.gmu.edu", ...],
  "pdf_sources": ["https://www.gmu.edu", "https://catalog.gmu.edu"],
  "crawl_config": {
    "max_concurrent_requests": 20,
    "requests_per_second_per_domain": 5,
    "max_pages": 200000,
    "max_crawl_depth": 5,
    "request_timeout_seconds": 30,
    "exclude_extensions": [".zip", ".mp4", ".jpg", ".css", ".js", ...],
    "pdf_extensions": [".pdf"],
    "exclude_path_patterns": [".*/wp-admin/.*", ".*/login.*", ...],
    "exclude_domains": ["email.gmu.edu", "canvas.gmu.edu", ...],
    "include_subdomains": true,
    "discover_subdomains_via_ct": true
  },
  "freshness_windows_days": {
    "admissions": 7, "events": 3, "policies": 60, "about": 60, ...
  },
  "rate_limits": {
    "default_rps": 5,
    "subdomain_overrides": { "catalog.gmu.edu": 3, "library.gmu.edu": 2 }
  },
  "kb_config": {
    "knowledge_base_id": "Q2WE6XQJHS",
    "data_source_ids": ["AHKH3DMWAJ", "MDTRKZLJFH"]
  }
}
```

### Configured Universities

| University | ID | Max Pages | Max Depth | Seed URLs | KB ID |
|------------|----|-----------|-----------|-----------|-------|
| George Mason University | `gmu` | 200,000 | 5 | 31 | `Q2WE6XQJHS` |
| Patrick Henry College | `phc` | 5,000 | 3 | 14 | `SFY1YK3YAQ` |
| Minerva University | `minu` | — | — | — | — |

### Bedrock Knowledge Base Data Sources

| University | KB ID | HTML Data Source | PDF Data Source |
|------------|-------|-----------------|-----------------|
| GMU | `Q2WE6XQJHS` | `AHKH3DMWAJ` | `MDTRKZLJFH` |
| PHC | `SFY1YK3YAQ` | `9EGVWWJ6XY` | `S1YCKGYAAN` |

---

## Build & Deploy

### Prerequisites

- **Docker (Colima):** All Lambda functions must be built inside a Linux arm64 container to produce correct binaries. Functions like `content-cleaner` use C extensions (trafilatura, lxml) that won't work if compiled natively on macOS arm64.
- **Colima socket:** Docker's socket path under Colima is `~/.colima/default/docker.sock` (not `/var/run/docker.sock`).
- **SAM CLI** and **AWS CLI** configured with the `251221984842` account.

### Build

```bash
cd university-crawler/

# Always build ALL functions together with Docker
DOCKER_HOST=unix:///Users/atsheen/.colima/default/docker.sock \
  sam build --use-container --skip-pull-image
```

**Why all functions together?** Running `sam build FunctionName` clears the entire `.aws-sam/build/` directory and only keeps artifacts for the named function. All other functions lose their build output. The built `template.yaml` then points to source directories for unbuilt functions, causing them to be deployed without dependencies. Always omit the function name to build everything at once.

**Verification after build:**
```bash
# content-cleaner should be ~33MB (trafilatura+lxml included)
# A ~17KB result means the Docker build didn't run — packages are missing
ls -lh .aws-sam/build/ContentCleanerFunction/
```

### Deploy

```bash
cd university-crawler/

DOCKER_HOST=unix:///Users/atsheen/.colima/default/docker.sock \
  sam deploy --no-confirm-changeset
```

Key `samconfig.toml` settings:
```toml
[default.build.parameters]
use_container = true

[default.deploy.parameters]
stack_name = "uka-crawler-dev"
region = "us-east-1"
confirm_changeset = false
capabilities = "CAPABILITY_IAM CAPABILITY_NAMED_IAM"
```

### Common Build Errors

| Symptom | Cause | Fix |
|---------|-------|-----|
| `content-cleaner` CodeSize ~17KB | Built without Docker (macOS binaries) | Rebuild with `--use-container` |
| `No such file or directory: /var/run/docker.sock` | Wrong Docker socket path | Set `DOCKER_HOST=unix:///Users/atsheen/.colima/default/docker.sock` |
| Only one function rebuilt after `sam build FunctionName` | SAM clears build dir per function | Always build all: `sam build --use-container` |
| `ImportError: cannot import name 'trafilatura'` | Missing C extension package | Rebuild with Docker |

---

## Operational Scripts

Located in `university-crawler/scripts/`:

| Script | Purpose |
|--------|---------|
| `inspect_dynamo.py` | Parallel DynamoDB counts by crawl_status, processing_status, domain |
| `count_s3_objects.py` | Count objects under an S3 prefix |
| `requeue_for_processing.py` | Query DynamoDB for crawled URLs, apply filters, send to processing queue |
| `requeue_missing_content.py` | Find gap-fill candidates (crawled URLs missing clean-content) |
| `cleanup_dynamo.py` | Clean up DynamoDB entries (dead URLs, duplicates) |
| `cleanup_off_domain.py` | Remove off-domain URLs from DynamoDB |
| `backfill_links_to_dynamo.py` | Backfill `links_to` field from S3 content |
| `backfill_related_urls.py` | Compute related URL scores |
| `trim_metadata.py` | Trim oversized metadata sidecars for Bedrock KB limits |
| `test_classifier.py` | Test classification on sample pages |
| `visualize_graph.py` | Generate URL graph visualizations |
| `visualize_links_graph.py` | Generate link relationship graphs |

---

## Monitoring & Debugging

### CloudWatch Log Groups

| Log Group | Lambda |
|-----------|--------|
| `/aws/lambda/university-seed-discovery-dev` | Seed Discovery |
| `/aws/lambda/university-crawler-worker-dev` | Crawler Worker |
| `/aws/lambda/university-crawl-orchestrator-dev` | Crawl Orchestrator |
| `/aws/lambda/university-refresh-trigger-dev` | Refresh Trigger |
| `/aws/lambda/content-cleaner-dev` | Content Cleaner |
| `/aws/lambda/page-classifier-dev` | Page Classifier (disabled) |
| `/aws/lambda/pdf-processor-dev` | Media Processor (PDFs, images, audio, video) |
| `/aws/lambda/batch-classifier-prepare-dev` | Batch Classifier Prepare |
| `/aws/lambda/batch-classifier-process-dev` | Batch Classifier Process |
| `/aws/lambda/dashboard-api-dev` | Dashboard API |

### Quick Health Check Commands

```bash
# DynamoDB crawl status counts
python3 scripts/inspect_dynamo.py --university-id gmu

# S3 object counts
python3 scripts/count_s3_objects.py raw-html/gmu/
python3 scripts/count_s3_objects.py clean-content/gmu/

# Queue depths
for q in crawl-queue-dev.fifo processing-queue-dev classification-queue-dev; do
  echo -n "$q: "
  aws sqs get-queue-attributes \
    --queue-url https://sqs.us-east-1.amazonaws.com/251221984842/$q \
    --attribute-names ApproximateNumberOfMessages \
    --query 'Attributes.ApproximateNumberOfMessages' --output text
done

# Batch classification jobs
aws bedrock list-model-invocation-jobs \
  --query 'invocationJobSummaries[?contains(jobName,`gmu`)].{Name:jobName,Status:status}' \
  --output table

# Dashboard API test
curl -s https://9mwsknkorc.execute-api.us-east-1.amazonaws.com/dev/v1/universities/gmu/stats | python3 -m json.tool
```

### Common Debug Patterns

**Page missing from clean-content:**
1. Check DynamoDB: `processing_status` value
2. If NOT SET → content cleaner errored silently (pre-fix) or never processed
3. If `empty_content` → Trafilatura couldn't extract text
4. If `s3_not_found` → raw HTML missing, needs re-crawl
5. Use `scripts/requeue_missing_content.py --dry-run` to find gaps

**DynamoDB throttling:**
- Reduce parallel threads in batch-classifier-process (WRITE_THREADS, DYNAMO_THREADS)
- All tables use PAY_PER_REQUEST — throttling means burst is too fast
- Solution: reduce concurrency, not increase capacity

**Force Lambda cold start (after IAM changes):**
```bash
aws lambda update-function-configuration \
  --function-name dashboard-api-dev \
  --description "force cold start $(date +%s)"
```

---

## Known Issues & Caveats

1. **EventBridge auto-trigger for batch classification processing** may not fire reliably. Workaround: manual invocation of `batch-classifier-process-dev` with `{"manifest_key": "batch-jobs/gmu/.../manifest.json"}`. Note: batch classification *preparation* is now automatically triggered by the Step Functions orchestrator after crawl+clean complete.

2. **Content cleaner silent failures** (pre-fix): If `process_message()` threw an exception, no `processing_status` was set. Post-fix, all outcomes set a status. Gap-fill with `scripts/requeue_missing_content.py`.

3. **Batch classifier thread counts**: Originally 100/50 threads caused DynamoDB throttling (1,407 failures out of 132,836). Reduced to 20/10 threads.

4. **Page classifier disabled**: `ReservedConcurrentExecutions: 0` — using batch classifier instead. The classification-queue still exists but is unused.

5. **Bedrock KB IAM**: Both `bedrock:` and `bedrock-agent:` prefixed actions are required for KB operations.

6. **PDF processor doesn't extract content**: Only creates metadata sidecars from URL patterns. Bedrock KB handles PDF parsing during ingestion.

7. **Metadata sidecar size limit**: Bedrock KB has a ~1024 byte limit for metadata attributes. `trim_metadata.py` script handles oversized sidecars.

8. **Scheduled crawls are now multi-university**: Previously, the EventBridge schedules targeted Step Functions directly with a hardcoded `phc` university. This is now resolved — scheduled crawls are managed by the `crawl-scheduler-dev` Lambda, which handles all configured universities automatically. Each university's schedule can be independently enabled or disabled via `POST /v1/universities/{uid}/freshness` with `schedule: {"incremental_enabled": true/false, "full_enabled": true/false}` flags stored in the `freshness_config` entity.

9. **`content-cleaner` must be built with Docker**: trafilatura/lxml have C extensions that must be compiled for Linux arm64. A native macOS `sam build` (without `--use-container`) produces a ~17KB deployment package that crashes at runtime with `ImportError`. Always use `DOCKER_HOST=unix:///Users/atsheen/.colima/default/docker.sock sam build --use-container`.

10. **Orphaned pending URLs**: When crawl messages exhaust retries and land in `crawl-dlq-dev.fifo`, the corresponding DynamoDB URL rows stay `crawl_status=pending` indefinitely. `check_progress` now detects this (crawl queue empty but `pending_count > 0`) and marks those URLs `error` so the pipeline can advance. URLs in the DLQ can be inspected and replayed manually if needed.

11. **Re-crawl without classification reset**: Running a full recrawl does not automatically invalidate existing `.metadata.json` classification sidecars. Pages whose HTML content hasn't changed will keep their existing category (no Bedrock API cost). Pages whose content changes during cleaning will have their `.metadata.json` deleted by `content-cleaner`, and will be re-classified on the next batch run. If you need to force re-classification of all pages, use `POST /v1/universities/{uid}/reset` with `{"reset_s3": true}` before triggering a crawl.

9. **Orchestrator `check_progress` requires `SQSPollerPolicy`** (fixed): `SQSSendMessagePolicy` only grants `sqs:SendMessage`, not `sqs:GetQueueAttributes`. Without `SQSPollerPolicy` on both the crawl queue and processing queue, `check_progress` returns `queue_depth=-1` (AccessDenied), causing `is_complete` to never be true and the state machine to loop forever.

12. **Pipeline status — incremental completion paths**: `GET /v1/universities/{uid}/pipeline/{job_id}` calls `_augment_live_progress()`, which now has two distinct completion paths:
    - `incremental_done`: `refresh_mode == "incremental" AND clean_done AND sfn_succeeded` → sets `overall_status = "completed"`, `classify_stage.status = "skipped"` (classification is skipped for incremental crawls per the `ShouldClassify` state)
    - `classify_done`: `clean_done AND classify_triggered AND sfn_succeeded AND batch_jobs_done` → sets `overall_status = "completed"`, `classify_stage.status = "completed"` (full crawl with classification)
