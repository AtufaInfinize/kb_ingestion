# Dashboard Request Flow — How Each API Call is Fulfilled

## AWS Infrastructure

### Lambda
All 21 endpoints are handled by a single Lambda: **`dashboard-api-dev`**
- CloudWatch Logs: `/aws/lambda/dashboard-api-dev`
- Runtime: Python 3.12, ARM64, 512 MB, 60s timeout
- Routes requests via regex pattern matching in `handler.py`

### DynamoDB Tables

| Table | Physical Name | Purpose |
|-------|--------------|---------|
| UrlRegistryTable | `url-registry-dev` | All discovered URLs, their crawl/processing status, category |
| PipelineJobsTable | `pipeline-jobs-dev` | Pipeline job tracking (90-day TTL) |
| EntityStoreTable | `entity-store-dev` | Extracted entities/facts from classification + per-university pipeline operational state (freshness config, crawl mode, KB sync status) |

### DynamoDB GSIs

| GSI | Table | Partition Key | Sort Key | Used By |
|-----|-------|--------------|----------|---------|
| `university-status-index` | url-registry | `university_id` | `crawl_status` | Stats, pipeline progress, reset |
| `university-category-index` | url-registry | `university_id` | `page_category` | Category cards, pages, rename, delete |
| `university-created-index` | pipeline-jobs | `university_id` | `created_at` | Job history listing, stop running pipelines |

### S3 Bucket
**`university-kb-content-251221984842-dev`**

| Prefix | Content |
|--------|---------|
| `configs/{uid}.json` | University configuration |
| `raw-html/{uid}/{domain}/{hash}.html` | Crawled HTML pages |
| `raw-pdf/{uid}/manual/{filename}` | Manually uploaded media files (PDFs, images, audio, video) |
| `clean-content/{uid}/{domain}/{hash}.md` | Cleaned markdown |
| `clean-content/{uid}/{domain}/{hash}.md.metadata.json` | Classification sidecar |
| `batch-jobs/{uid}/` | Batch classification job artifacts |
| `crawl-summaries/{uid}/` | Crawl summary reports |

### SQS Queues

| Queue | Physical Name | Type |
|-------|--------------|------|
| CrawlQueue | `crawl-queue-dev.fifo` | FIFO with content-based dedup |
| ProcessingQueue | `processing-queue-dev` | Standard |
| PdfProcessingQueue | `pdf-processing-queue-dev` | Standard |

> **Note**: The ClassificationQueue was removed. Classification is now triggered via batch-classifier-prepare Lambda invocation.

### Other Services
- **Step Functions**: `university-crawl-orchestrator-dev` — crawl pipeline state machine (crawl stage only)
- **Bedrock Agent**: KB ingestion via `start_ingestion_job`, `list_ingestion_jobs`
- **Bedrock**: Batch classification via `list_model_invocation_jobs`
- **Lambda (batch-classifier-prepare)**: Invoked asynchronously by dashboard API when clean stage completes

---

## Request Flow Per Endpoint

### 1. GET /v1/universities — List Universities

**Streamlit**: Sidebar university dropdown on page load
**Handler**: `stats.list_universities()`

```
Dashboard → API Gateway → Lambda
  └→ S3.list_objects_v2(Prefix='configs/')
  └→ S3.get_object('configs/phc.json')  ← for each config file
  └→ S3.get_object('configs/gmu.json')
  ← Returns [{university_id, name}, ...]
```

**Services**: S3 only (no DynamoDB)

---

### 2. GET /v1/universities/{uid}/stats — Dashboard Stats

**Streamlit**: Maintenance tab → Quick Stats section
**Handler**: `stats.get_stats()`

```
Dashboard → API Gateway → Lambda
  └→ S3.get_object('configs/{uid}.json')           ← load config for name
  └→ DynamoDB.query × 8 (parallel)                 ← count by crawl_status
  │    GSI: university-status-index
  │    Statuses: pending, crawled, error, failed, dead, redirected, blocked_robots, skipped_depth
  └→ DynamoDB.query × 18 (parallel)                ← count by category
  │    GSI: university-category-index
  │    All VALID_CATEGORIES except 'excluded'
  └→ DynamoDB.query × 1                            ← count crawled (for unprocessed calc)
       GSI: university-status-index
  ← Returns {total_urls, urls_by_crawl_status, urls_by_processing_status}
  └→ DynamoDB.get_item(entity-store-dev, Key={university_id, 'kb_sync_status'})  ← kb sync state
  ← Returns {total_urls, ..., pending_kb_sync: bool, pages_changed_last_crawl: int, crawl_completed_at: str}
```

**Services**: S3, DynamoDB (~27 parallel GSI COUNT queries + 1 entity-store get_item)

- `pending_kb_sync=true` when the last incremental crawl changed pages and KB Sync hasn't been run yet
- Dashboard sidebar shows a warning banner with the page count when this is true

---

### 3. GET /v1/universities/{uid}/config — Get Config

**Streamlit**: Setup tab form load
**Handler**: `config.get_config()`

```
Dashboard → API Gateway → Lambda
  └→ S3.get_object('configs/{uid}.json')
  ← Returns full config JSON
```

**Services**: S3 only

---

### 4. POST /v1/universities/{uid}/config — Save Config

**Streamlit**: Setup tab → "Save Configuration" button
**Handler**: `config.save_config()`

```
Dashboard → API Gateway → Lambda
  └→ Validate body (name, root_domain, seed_urls required)
  └→ Fill defaults (crawl_config, freshness_windows, rate_limits)
  └→ S3.put_object('configs/{uid}.json', JSON)
  ← Returns {status: 'saved', config: {...}}
```

**Services**: S3 only

---

### 5. GET /v1/universities/{uid}/categories — Category Cards

**Streamlit**: Review tab → category card grid
**Handler**: `categories.list_categories()`

```
Dashboard → API Gateway → Lambda
  └→ DynamoDB.query × 19 (parallel, ThreadPoolExecutor)
       GSI: university-category-index
       For each category: SELECT COUNT WHERE university_id=:uid AND page_category=:cat
  ← Returns [{category, label, count}, ...] (only non-zero categories)
```

**Services**: DynamoDB only (19 parallel COUNT queries)

---

### 6. GET /v1/universities/{uid}/categories/{cat}/pages — Pages List

**Streamlit**: Review tab → click category card → pages table
**Handler**: `categories.list_pages()`

```
Dashboard → API Gateway → Lambda
  └→ DynamoDB.query
       GSI: university-category-index
       KeyCondition: university_id=:uid AND page_category=:cat
       FilterExpression: domain=:d AND/OR processing_status=:ps  (optional)
       Limit: 50-200, pagination via next_token
  ← Returns {pages: [{url, url_hash, domain, processing_status, ...}], next_token}
```

**Services**: DynamoDB only (single paginated GSI query)

---

### 7. POST /v1/universities/{uid}/categories/{cat}/pages — Add URLs

**Streamlit**: Review tab → "Add URLs" form
**Handler**: `categories.add_pages()`

```
Dashboard → API Gateway → Lambda
  For each URL:
    └→ DynamoDB.get_item(Key={url})                ← check if exists
    ├─ If exists + crawled:
    │    └→ DynamoDB.update_item                    ← SET page_category, manually_curated, curated_at
    │    ← {status: 'category_updated'}
    ├─ If exists + not crawled + trigger_crawl:
    │    └→ DynamoDB.update_item                    ← SET page_category
    │    └→ SQS.send_message(crawl-queue-dev.fifo) ← queue for crawl
    │    ← {status: 'queued_for_crawl'}
    └─ If new:
         └→ DynamoDB.put_item                       ← register new URL with all fields
         └→ SQS.send_message(crawl-queue-dev.fifo)  ← queue for crawl (if trigger_crawl)
         ← {status: 'registered_and_queued'}
```

**Services**: DynamoDB (get + put/update per URL), SQS (send to crawl FIFO queue)
**SQS MessageGroupId**: `{domain}#manual`

---

### 8. DELETE /v1/universities/{uid}/categories/{cat}/pages — Remove URLs

**Streamlit**: Review tab → "Remove" button on page row
**Handler**: `categories.remove_pages()`

```
Dashboard → API Gateway → Lambda
  For each URL:
    └→ DynamoDB.update_item                         ← SET page_category='excluded', manually_curated=true
    If action == 'delete_content':
      └→ DynamoDB.get_item(Key={url})               ← get url_hash, domain
      └→ S3.delete_object('clean-content/{uid}/{domain}/{hash}.md')
      └→ S3.delete_object('clean-content/{uid}/{domain}/{hash}.md.metadata.json')
```

**Services**: DynamoDB (update per URL), optionally S3 (delete 2 objects per URL)

---

### 9. POST /v1/universities/{uid}/categories/{cat}/media/upload-url — Presigned Upload

**Streamlit**: Review tab → "Upload Media" popover
**Handler**: `categories.get_upload_url()`

```
Dashboard → API Gateway → Lambda
  └→ S3.generate_presigned_url('put_object')
       Key: raw-pdf/{uid}/manual/{sanitized_filename}
       ContentType: dynamic (from body `content_type` field, default `application/pdf`)
       ExpiresIn: 3600s (1 hour)
  ← Returns {upload_url, s3_key, filename}

Then Streamlit performs a 3-step flow:
  1. Get presigned URL with content_type
  2. HTTP PUT to presigned URL with file bytes (direct upload to S3)
  3. Call POST .../media/process to trigger processing
```

**Services**: S3 (presigned URL generation, then direct upload from browser)

---

### 10. POST /v1/universities/{uid}/categories/{cat}/rename — Rename Category

**Streamlit**: Review tab → "Rename Category" popover
**Handler**: `categories.rename_category()`

```
Dashboard → API Gateway → Lambda
  Loop (paginated):
    └→ DynamoDB.query                               ← get all URLs in old category
         GSI: university-category-index
         ProjectionExpression: url (minimal read)
    For each URL in page:
      └→ DynamoDB.update_item                       ← SET page_category=:new, curated_at=:ts
  ← Returns {old_category, new_category, pages_moved}
```

**Services**: DynamoDB only (paginated query + update per item)
**Note**: For large categories (1000s of pages), this can take several seconds due to sequential updates.

---

### 11. POST /v1/universities/{uid}/categories/{cat}/delete — Delete Category

**Streamlit**: Review tab → "Delete Category" popover
**Handler**: `categories.delete_category()`

```
Same as rename, but target_category defaults to 'excluded'
```

**Services**: DynamoDB only (same pattern as rename)

---

### 12. POST /v1/universities/{uid}/pipeline — Start Pipeline

**Streamlit**: Setup tab → "Save & Start Full Crawl", or Maintenance tab → recrawl buttons
**Handler**: `pipeline.start_pipeline()`

```
Dashboard → API Gateway → Lambda
  └→ StepFunctions.start_execution(
       stateMachineArn: university-crawl-orchestrator-dev,
       name: '{uid}-{mode}-{timestamp}',
       input: {university_id, refresh_mode, domain, triggered_at, triggered_by: 'api'}
     )
  └→ DynamoDB.put_item(pipeline-jobs-dev)           ← create job record
       Fields: job_id, university_id, created_at, refresh_mode, overall_status='running',
               execution_arn, crawl_stage={status:'running'}, clean_stage={status:'pending'},
               classify_stage={status:'pending'}, ttl=now+90days
  ← Returns HTTP 202 {job_id, overall_status: 'running', stages...}
```

**Services**: Step Functions (start execution), DynamoDB (put job record)

---

### 13. GET /v1/universities/{uid}/pipeline — List Jobs

**Streamlit**: Crawling tab → job selector, Maintenance tab → recent jobs table
**Handler**: `pipeline.list_jobs()`

```
Dashboard → API Gateway → Lambda
  └→ DynamoDB.query(pipeline-jobs-dev)
       GSI: university-created-index
       KeyCondition: university_id=:uid
       ScanIndexForward: false (newest first)
       Limit: 10-100
  ← Returns {jobs: [...], count, next_token}
```

**Services**: DynamoDB only (single GSI query on pipeline-jobs table)

---

### 14. GET /v1/universities/{uid}/pipeline/{job_id} — Live Job Progress

**Streamlit**: Crawling tab → progress bars (polled on manual refresh or auto-refresh checkbox)
**Handler**: `pipeline.get_job_status()` → `_augment_live_progress()`

```
Dashboard → API Gateway → Lambda
  └→ DynamoDB.get_item(pipeline-jobs-dev, Key={job_id})  ← load stored job
  If overall_status == 'running':
    └→ DynamoDB.query × 8 (parallel)                     ← crawl status counts
         GSI: university-status-index
    └→ DynamoDB.query × 18 (parallel)                    ← category counts (for classified total)
         GSI: university-category-index
    └→ SQS.get_queue_attributes × 2                      ← queue depths
         Queues: crawl-queue, processing-queue
         Attributes: ApproximateNumberOfMessages, ApproximateNumberOfMessagesNotVisible
    └→ StepFunctions.describe_execution(execution_arn)    ← state machine status

    Stage transition detection (with guards):
      crawl_done = queue_empty AND pending==0 AND total>0  ← total>0 prevents false completion on fresh start
      clean_done = crawl_done AND processing_queue_empty
      classify_done = clean_done AND classify_triggered AND classified > 0 AND classified >= crawled - errors

    # Two completion paths:
    # 1. incremental_done: refresh_mode=="incremental" AND clean_done AND sfn_succeeded
    #    → overall_status="completed", classify_stage.status="skipped"
    # 2. classify_done: clean_done AND classify_triggered AND sfn_succeeded AND batch_jobs_done
    #    → overall_status="completed", classify_stage.status="completed"

    If clean→classify transition detected:
      └→ Lambda.invoke(batch-classifier-prepare, async)   ← trigger batch classification
      └→ classify_stage.classify_triggered = true

    If stages transitioned:
      └→ DynamoDB.update_item(pipeline-jobs-dev)          ← persist new stage statuses
  ← Returns {job_id, overall_status, crawl_stage{status,total,completed,queue}, ...}
```

**Services**: DynamoDB (~27 queries), SQS (2 queue depth checks), Step Functions (describe), Lambda (invoke batch classifier on transition), DynamoDB (update if transition)
**This is the most expensive endpoint** — ~30 AWS API calls per request.

**Race condition guard**: `crawl_stage['total'] > 0` prevents false crawl completion when the pipeline just started (before seeds are queued, all counts are 0 and queues are empty).

---

### 15. POST /v1/universities/{uid}/kb/sync — Start KB Ingestion

**Streamlit**: Ingestion tab → "Start KB Sync Now" button
**Handler**: `kb.start_sync()`

```
Dashboard → API Gateway → Lambda
  └→ S3.get_object('configs/{uid}.json')                 ← load kb_config
  For each data_source_id in kb_config:
    └→ BedrockAgent.start_ingestion_job(
         knowledgeBaseId, dataSourceId
       )
  └→ DynamoDB.update_item(entity-store-dev)          ← clear pending_kb_sync notification
       Key: {university_id, 'kb_sync_status'}
       SET status='syncing', synced_at=now
  ← Returns HTTP 202 {knowledge_base_id, ingestion_jobs: [{data_source_id, job_id, status}]}
```

**Services**: S3 (config read), Bedrock Agent (start ingestion per data source), DynamoDB (entity-store update)

---

### 16. GET /v1/universities/{uid}/kb/sync — KB Sync Status

**Streamlit**: Ingestion tab → status cards (polled on load + refresh button)
**Handler**: `kb.get_sync_status()`

```
Dashboard → API Gateway → Lambda
  └→ S3.get_object('configs/{uid}.json')                 ← load kb_config
  For each data_source_id:
    └→ BedrockAgent.list_ingestion_jobs(
         knowledgeBaseId, dataSourceId,
         maxResults: 3, sortBy: 'STARTED_AT', sortOrder: 'DESCENDING'
       )
  ← Returns {data_sources: [{data_source_id, recent_jobs: [{status, statistics}]}]}
```

**Services**: S3 (config read), Bedrock Agent (list ingestion jobs per data source)

---

### 17. GET /v1/universities/{uid}/freshness — Per-Category Freshness Windows + Schedule

**Streamlit**: Maintenance tab → "Schedule & Freshness Windows" expander on load
**Handler**: `freshness.get_freshness()`

```
Dashboard → API Gateway → Lambda
  └→ DynamoDB.get_item(entity-store-dev, Key={university_id, 'freshness_config'})
  └→ load_freshness_windows(uid)  ← returns windows dict (or 1-day defaults)
  ← Returns {university_id, windows: {cat: days, ...}, schedule: {incremental_enabled, full_enabled}}
```

**Services**: DynamoDB only (single get_item on entity-store)

---

### 18. POST /v1/universities/{uid}/freshness — Save Freshness Settings

**Streamlit**: Maintenance tab → "Schedule & Freshness Windows" expander → "Save Settings" button
**Handler**: `freshness.save_freshness()`

```
Dashboard → API Gateway → Lambda
  └→ Validate windows dict (each value must be int 1–3650)
  └→ Extract schedule flags (incremental_enabled, full_enabled; default true)
  └→ DynamoDB.put_item(entity-store-dev)
       entity_key: 'freshness_config'
       Fields: windows, incremental_enabled, full_enabled, updated_at
  ← Returns {status: 'saved', university_id, windows, schedule}
```

**Services**: DynamoDB only (single put_item on entity-store)

**Effect on scheduler**: The `crawl-scheduler-dev` Lambda reads these flags before triggering any crawl. If `incremental_enabled=false`, the university's daily crawl is skipped. If `full_enabled=false`, the weekly full crawl is skipped.

---

### 19. GET /v1/universities/{uid}/classification — Classification Status

**Streamlit**: Maintenance tab → Classification Jobs section
**Handler**: `classification.get_classification_status()`

```
Dashboard → API Gateway → Lambda
  └→ Bedrock.list_model_invocation_jobs(
       maxResults: 100,
       sortBy: 'CreationTime',
       sortOrder: 'Descending'
     )
  └→ Filter by job name containing '-{uid}-' or starting with 'classify-{uid}-'
  ← Returns {total_jobs, status_summary: {Completed: N, ...}, jobs: [...]}
```

**Services**: Bedrock only (list batch inference jobs, client-side filter)

---

### 20. POST /v1/universities/{uid}/reset — Reset University Data

**Streamlit**: Maintenance tab → "Reset All Data" / "Reset Classification" buttons
**Handler**: `reset.reset_university()`

```
Dashboard → API Gateway → Lambda
  Body: {"scope": "all" | "classification"}

  Step 1 — Stop running pipelines (always runs first):
    └→ DynamoDB.query(pipeline-jobs-dev)                  ← find running jobs
         GSI: university-created-index
    For each running job:
      └→ StepFunctions.stop_execution(executionArn, cause='Reset requested via dashboard')
      └→ DynamoDB.update_item(pipeline-jobs-dev)          ← SET overall_status='cancelled'

  Step 2a — scope="all":
    └→ DynamoDB.query + batch_writer.delete_item          ← delete all url-registry items
         GSI: university-status-index (per status: pending, crawled, error, ...)
    └→ DynamoDB.query + batch_writer.delete_item          ← delete all entity-store items
         KeyCondition: university_id=:uid
    └→ DynamoDB.query + batch_writer.delete_item          ← delete all pipeline-jobs
         GSI: university-created-index
    └→ S3.list_objects_v2 + delete_objects                ← delete S3 prefixes:
         raw-html/{uid}/, clean-content/{uid}/, batch-jobs/{uid}/, crawl-summaries/{uid}/
    Keeps: configs/{uid}.json

  Step 2b — scope="classification":
    └→ DynamoDB.query + batch_writer.delete_item          ← delete entity-store items
    └→ S3.list_objects_v2 + delete_objects                ← delete .metadata.json sidecars
         clean-content/{uid}/ (suffix_filter='.metadata.json')
    └→ S3.list_objects_v2 + delete_objects                ← delete batch-jobs/{uid}/
    └→ DynamoDB.query + update_item                       ← reset url-registry classification fields
         GSI: university-status-index (crawl_status='crawled')
         REMOVE page_category, subcategory, classified_at
         SET processing_status='cleaned'
    Keeps: raw HTML, clean markdown, config, jobs

  ← Returns {scope, university_id, pipelines_stopped, url_registry_deleted|urls_reset,
             entity_store_deleted, s3_objects_deleted|sidecars_deleted, pipeline_jobs_deleted}
```

**Services**: Step Functions (stop execution), DynamoDB (query + batch delete on 3 tables), S3 (list + batch delete)
**IAM**: Requires `DynamoDBCrudPolicy` on entity-store (not just read) + `StepFunctionsExecutionPolicy`

---

### 21. POST /v1/universities/{uid}/categories/{cat}/media/process — Trigger Media Processing

**Streamlit**: Called automatically after successful "Upload Media" S3 upload
**Handler**: `categories.process_uploaded_media()`

```
Dashboard → API Gateway → Lambda
  └→ Build SQS message: url=manual-upload://{uid}/{filename}, university_id, domain='manual',
     s3_key, content_type, url_hash=md5(s3_key)[:8], page_category=cat
  └→ SQS.send_message(pdf-processing-queue-dev)
  ← Returns {status: 'processing', s3_key}

Then PdfProcessorFunction picks up the SQS message:
  └→ S3.head_object (verify file exists)
  └→ S3.put_object (create .metadata.json sidecar next to the file)
  └→ DynamoDB.update_item (url-registry: page_category, processing_status='processed_media')
```

**Services**: SQS (send message), then asynchronously: S3 (head + put), DynamoDB (update)

---

## Streamlit Dashboard Caching

The dashboard uses `@st.cache_data(ttl=15)` for all read-only API calls to avoid redundant fetches across tabs (Streamlit re-renders all 5 tabs on every interaction).

| Call type | Function | Caching |
|-----------|----------|---------|
| Read-only GETs | `cached_get(path)` | 15s TTL cache |
| Job status poll | `api_get(path)` | No cache (needs fresh data) |
| POST/DELETE mutations | `api_post()` / `api_delete()` | Auto-clears cache after success |
| Manual refresh buttons | Click handler | Calls `cached_get.clear()` then `st.rerun()` |

---

## CloudWatch Logs

Every Lambda writes to its own log group. The dashboard API logs include the HTTP method and path for each request.

| Lambda | Log Group | What to look for |
|--------|-----------|-----------------|
| Dashboard API | `/aws/lambda/dashboard-api-dev` | `Dashboard API: GET /v1/universities/phc/categories` |
| Crawler Worker | `/aws/lambda/crawler-worker-dev` | `Processing: https://...`, `Content unchanged:`, `ERROR` |
| Content Cleaner | `/aws/lambda/content-cleaner-dev` | `Stored clean content:`, `No content extracted` |
| PDF Processor | `/aws/lambda/pdf-processor-dev` | PDF processing logs |
| Batch Classifier Prepare | `/aws/lambda/batch-classifier-prepare-dev` | `Submitting batch job:`, job ARNs |
| Batch Classifier Process | `/aws/lambda/batch-classifier-process-dev` | `Classified:`, `Sidecar written:`, DynamoDB update counts |
| Crawl Orchestrator | `/aws/lambda/crawl-orchestrator-dev` | `URLs queued:`, `Crawl complete:`, summary JSON |
| Seed Discovery | `/aws/lambda/seed-discovery-dev` | `Discovered seeds:`, sitemap parsing |
| Refresh Trigger | `/aws/lambda/refresh-trigger-dev` | Manual crawl trigger logs |

### Useful log queries

```bash
# Tail dashboard API logs live
aws logs tail /aws/lambda/dashboard-api-dev --follow

# Dashboard errors only
aws logs tail /aws/lambda/dashboard-api-dev --filter-pattern "ERROR"

# Content cleaner for PHC in last hour
aws logs tail /aws/lambda/content-cleaner-dev --since 1h --filter-pattern "phc"

# Crawler worker failures
aws logs tail /aws/lambda/crawler-worker-dev --filter-pattern "Failed"

# Batch classifier results
aws logs tail /aws/lambda/batch-classifier-process-dev --since 2h

# Reset endpoint logs
aws logs tail /aws/lambda/dashboard-api-dev --filter-pattern "Reset" --since 30m
```

---

## Summary: AWS Resources Per Endpoint

| Endpoint | DynamoDB | S3 | SQS | Step Functions | Bedrock | Lambda |
|----------|----------|-----|-----|----------------|---------|--------|
| GET /universities | | read configs | | | | |
| GET /stats | url-registry (27 queries) + entity-store (1 get) | read config | | | | |
| GET /config | | read config | | | | |
| POST /config | | write config | | | | |
| GET /categories | url-registry (19 queries) | | | | | |
| GET /.../pages | url-registry (1 query) | | | | | |
| POST /.../pages | url-registry (get+put per URL) | | send to crawl queue | | | |
| DELETE /.../pages | url-registry (update per URL) | delete clean content | | | | |
| POST /.../upload-url | | presigned URL (dynamic content type) | | | | |
| POST /.../media/process | url-registry (update) | head + put sidecar | send to pdf queue | | | |
| POST /.../rename | url-registry (query+update loop) | | | | | |
| POST /.../delete | url-registry (query+update loop) | | | | | |
| POST /pipeline | pipeline-jobs (put) | | | start_execution | | |
| GET /pipeline | pipeline-jobs (query) | | | | | |
| GET /pipeline/{id} | url-registry (27), pipeline-jobs (get+update) | | 2 queue depths | describe_execution | | invoke batch-classifier (on transition); two completion paths (incremental_done, classify_done) |
| POST /kb/sync | entity-store (1 update) | read config | | | start_ingestion (per DS) | |
| GET /kb/sync | | read config | | | list_ingestion (per DS) | |
| GET /freshness | entity-store (1 get) | | | | | |
| POST /freshness | entity-store (1 put) | | | | | |
| GET /classification | | | | | list_model_invocation_jobs | |
| POST /reset | url-registry + entity-store + pipeline-jobs (query+delete) | list+delete objects | | stop_execution | | |
