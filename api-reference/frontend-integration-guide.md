# Frontend Integration Guide — University KB Crawler API

API reference for integrating the University KB Crawler backend into a frontend application.

**Base URL:** `https://{api-id}.execute-api.us-east-1.amazonaws.com/{stage}/v1`
**Stage:** `dev` (current)
**Auth:** API Key via `X-Api-Key` header (or Authorization header)
**Content-Type:** All request/response bodies are `application/json`
**CORS:** All origins allowed; methods GET, POST, DELETE, OPTIONS

---

## Table of Contents

1. [Quick Start](#quick-start)
2. [Response Envelope](#response-envelope)
3. [University Management](#1-university-management)
4. [Dashboard Stats](#2-dashboard-stats)
5. [Category Management](#3-category-management)
6. [Page Management](#4-page-management)
7. [Media Upload](#5-media-upload)
8. [Pipeline (Crawl Jobs)](#6-pipeline-crawl-jobs)
9. [Knowledge Base Sync](#7-knowledge-base-sync)
10. [Freshness & Schedule](#8-freshness--schedule-settings)
11. [Classification](#9-classification-status)
12. [DLQ Inspection](#10-dlq-inspection)
13. [Reset / Danger Zone](#11-reset--danger-zone)
14. [Enums & Constants](#enums--constants)
15. [Polling & Real-Time Patterns](#polling--real-time-patterns)
16. [Error Handling](#error-handling)
17. [TypeScript Types](#typescript-types)

---

## Quick Start

```typescript
const API_BASE = "https://{api-id}.execute-api.us-east-1.amazonaws.com/dev/v1";

async function apiFetch<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    headers: {
      "Content-Type": "application/json",
      "X-Api-Key": process.env.NEXT_PUBLIC_API_KEY!,
    },
    ...options,
  });
  if (!res.ok) throw new Error(`API ${res.status}: ${await res.text()}`);
  return res.json();
}
```

---

## Response Envelope

All endpoints return a JSON body directly (no wrapper envelope). HTTP status codes indicate success/failure:

| Status | Meaning |
|--------|---------|
| `200` | Success |
| `202` | Accepted (async operation started) |
| `400` | Validation error — check `error` field |
| `404` | Resource not found |
| `500` | Server error |

Error responses always include an `error` string:
```json
{ "error": "description of what went wrong" }
```

---

## 1. University Management

### List Universities

```
GET /v1/universities
```

Returns all configured universities. Use this to populate a university selector dropdown.

**Response:**
```json
{
  "universities": [
    { "university_id": "gmu", "name": "George Mason University" },
    { "university_id": "phc", "name": "Patrick Henry College" }
  ]
}
```

---

### Get University Config

```
GET /v1/universities/{uid}/config
```

Returns the full university configuration (seed URLs, crawl settings, domain patterns, KB config).

**Response:**
```json
{
  "university_id": "gmu",
  "name": "George Mason University",
  "root_domain": "gmu.edu",
  "seed_urls": ["https://www.gmu.edu/", "https://admissions.gmu.edu/"],
  "crawl_config": {
    "max_concurrent_requests": 10,
    "requests_per_second_per_domain": 3,
    "max_pages": 50000,
    "max_crawl_depth": 5,
    "request_timeout_seconds": 30,
    "allowed_domain_patterns": ["*.gmu.edu"],
    "exclude_extensions": [".zip", ".mp4", ".css", ".js"],
    "pdf_extensions": [".pdf"],
    "exclude_path_patterns": [".*\\/wp-admin\\/.*"],
    "exclude_domains": [],
    "include_subdomains": true
  },
  "freshness_windows_days": {
    "admissions": 7,
    "events": 3,
    "about": 60
  },
  "rate_limits": { "default_rps": 3 },
  "kb_config": {
    "knowledge_base_id": "XXXXXXXXXX",
    "data_source_ids": ["YYYYYYYYYY"]
  }
}
```

**Errors:**
- `404` — Config not found for this university

---

### Save University Config

```
POST /v1/universities/{uid}/config
```

Create or update a university configuration. Omitted sections get defaults filled in.

**Request Body:**
```json
{
  "name": "George Mason University",       // required
  "root_domain": "gmu.edu",                // required
  "seed_urls": ["https://www.gmu.edu/"],   // required, array of strings
  "crawl_config": { ... },                 // optional — defaults applied
  "freshness_windows_days": { ... },       // optional — defaults applied
  "rate_limits": { "default_rps": 3 },     // optional
  "kb_config": {                           // optional
    "knowledge_base_id": "...",
    "data_source_ids": ["..."]
  }
}
```

**Response:**
```json
{
  "status": "saved",
  "university_id": "gmu",
  "config": { ... }
}
```

**Errors:**
- `400` — `name`, `root_domain`, or `seed_urls` missing

---

## 2. Dashboard Stats

### Get Stats

```
GET /v1/universities/{uid}/stats
```

Returns a comprehensive overview: URL counts by crawl status, processing status, staleness info, and KB sync state. This is the primary data source for a dashboard overview page.

**Response:**
```json
{
  "university_id": "gmu",
  "name": "George Mason University",
  "total_urls": 12450,
  "total_discovered_urls": 12200,
  "urls_by_crawl_status": {
    "crawled": 11200,
    "pending": 50,
    "error": 180,
    "failed": 20,
    "dead": 500,
    "redirected": 300,
    "blocked_robots": 150,
    "skipped_depth": 50
  },
  "urls_by_processing_status": {
    "classified": 10500,
    "unprocessed": 100
  },
  "total_content_pages": 10800,
  "classified_pages": 10500,
  "unclassified_pages": 300,
  "total_media_files": 45,
  "media_by_type": {
    "pdf": 30,
    "image": 10,
    "audio": 0,
    "video": 0,
    "other": 5
  },
  "kb_ingestion": {
    "ingested_pages": 10200,
    "failed_pages": 88,
    "scanned_pages": 10500,
    "new_indexed": 150,
    "modified_indexed": 50,
    "deleted": 3,
    "last_sync_status": "COMPLETE",
    "last_sync_at": "2026-02-28T04:00:00+00:00"
  },
  "pages_changed": 127,
  "pending_kb_sync": true,
  "dead_urls": 500,
  "last_crawled_at": "2026-02-28T02:15:00+00:00",
  "days_since_crawl": 2,
  "stale_categories": ["events", "news"],
  "crawl_completed_at": "2026-02-28T04:30:00+00:00",
  "config": {
    "seed_urls_count": 5,
    "max_crawl_depth": 5,
    "allowed_domain_patterns": ["*.gmu.edu"]
  }
}
```

**Frontend usage hints:**
- Show `pending_kb_sync` + `pages_changed` as a warning banner: "127 pages changed — KB sync needed"
- `kb_ingestion.ingested_pages` = total pages successfully in KB; `kb_ingestion.failed_pages` = pages that failed ingestion (separate from pending sync)
- `pages_changed` = new/modified pages from the latest crawl (tracked by content-cleaner, independent of Bedrock ingestion failures)
- Show `stale_categories` as badges on category cards
- Use `urls_by_crawl_status` for a donut/bar chart
- `days_since_crawl` drives "Last crawled X days ago" text
- `total_content_pages` (S3 .md files) vs `total_urls` (DynamoDB registry) — content pages are source of truth for what can go into KB
- `media_by_type` for media breakdown (pdf, image, audio, video, other)

---

## 3. Category Management

### List Categories (Cards)

```
GET /v1/universities/{uid}/categories
```

Returns non-zero categories with page counts. Use for rendering category cards/tiles.

**Response:**
```json
{
  "university_id": "gmu",
  "total_pages": 10500,
  "categories": [
    { "category": "admissions", "label": "Admissions", "count": 1200 },
    { "category": "academic_programs", "label": "Academic Programs", "count": 890 },
    { "category": "financial_aid", "label": "Financial Aid", "count": 450 }
  ]
}
```

**Note:** Only categories with count > 0 are returned. The `excluded` category is never shown.

---

### Rename Category

```
POST /v1/universities/{uid}/categories/{cat}/rename
```

Move all pages from one category to another.

**Request Body:**
```json
{
  "new_category": "student_services"
}
```

**Response:**
```json
{
  "old_category": "other",
  "new_category": "student_services",
  "pages_moved": 234,
  "errors": []
}
```

**Errors:**
- `400` — Invalid source or target category, or same as current
- **Warning:** For large categories (1000+ pages), this can take several seconds.

---

### Delete Category

```
POST /v1/universities/{uid}/categories/{cat}/delete
```

Move all pages in a category to a target (default: `excluded`).

**Request Body:**
```json
{
  "target_category": "excluded"    // optional, default "excluded"
}
```

**Response:**
```json
{
  "category": "low_value",
  "target_category": "excluded",
  "pages_deleted": 500,
  "errors": []
}
```

---

## 4. Page Management

### List Pages in Category

```
GET /v1/universities/{uid}/categories/{cat}/pages?limit=50&domain=admissions.gmu.edu&processing_status=classified
```

Paginated list of pages within a category.

**Query Parameters:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `limit` | int | 50 | Page size (max 200) |
| `next_token` | string | — | Pagination cursor from previous response |
| `domain` | string | — | Filter by domain |
| `processing_status` | string | — | Filter: `cleaned`, `classified`, `processed_media` |

**Response:**
```json
{
  "university_id": "gmu",
  "category": "admissions",
  "pages": [
    {
      "url": "https://admissions.gmu.edu/apply",
      "url_hash": "a1b2c3d4e5f6g7h8",
      "domain": "admissions.gmu.edu",
      "subcategory": "application_process",
      "processing_status": "classified",
      "crawl_status": "crawled",
      "content_type": "text/html",
      "content_length": 45200,
      "last_crawled_at": "2026-02-28T02:30:00+00:00"
    }
  ],
  "count": 50,
  "next_token": "eyJsYXN0X2V2YWx1YX..."
}
```

**Pagination pattern:**
```typescript
let allPages: Page[] = [];
let token: string | null = null;

do {
  const params = new URLSearchParams({ limit: "200" });
  if (token) params.set("next_token", token);

  const data = await apiFetch(`/v1/universities/${uid}/categories/${cat}/pages?${params}`);
  allPages.push(...data.pages);
  token = data.next_token;
} while (token);
```

---

### Add URLs to Category

```
POST /v1/universities/{uid}/categories/{cat}/pages
```

Register URLs and optionally trigger crawling.

**Request Body:**
```json
{
  "urls": [
    "https://admissions.gmu.edu/apply",
    "https://admissions.gmu.edu/visit"
  ],
  "trigger_crawl": true    // optional, default true
}
```

**Response:**
```json
{
  "added": [
    { "url": "https://admissions.gmu.edu/apply", "status": "category_updated" },
    { "url": "https://admissions.gmu.edu/visit", "status": "registered_and_queued" }
  ],
  "errors": []
}
```

**Status values:**

| Status | Meaning |
|--------|---------|
| `category_updated` | URL existed and was already crawled — category reassigned |
| `queued_for_crawl` | URL existed but not yet crawled — re-queued |
| `registered_and_queued` | New URL registered and queued for crawling |
| `registered` | New URL registered (trigger_crawl was false) |

---

### Remove URLs from Category

```
DELETE /v1/universities/{uid}/categories/{cat}/pages
```

Mark URLs as excluded. Optionally delete their clean content from S3.

**Request Body:**
```json
{
  "urls": ["https://admissions.gmu.edu/old-page"],
  "action": "delete_content"    // optional: "mark_excluded" (default) or "delete_content"
}
```

- `mark_excluded` — Sets `page_category = "excluded"` but keeps the S3 content
- `delete_content` — Also deletes the clean markdown + metadata sidecar from S3

**Response:**
```json
{
  "removed": [
    { "url": "https://admissions.gmu.edu/old-page", "action": "delete_content" }
  ],
  "errors": []
}
```

---

## 5. Media Upload

Media upload uses a **3-step flow**: get presigned URL, upload directly to S3, then trigger processing.

### Step 1: Get Presigned Upload URL

```
POST /v1/universities/{uid}/categories/{cat}/media/upload-url
```

**Request Body:**
```json
{
  "filename": "Fall 2026 Brochure.pdf",           // required
  "content_type": "application/pdf"                // optional, default "application/pdf"
}
```

**Response:**
```json
{
  "upload_url": "https://university-kb-content-....s3.amazonaws.com/raw-pdf/gmu/manual/Fall_2026_Brochure.pdf?X-Amz-...",
  "s3_key": "raw-pdf/gmu/manual/Fall_2026_Brochure.pdf",
  "category": "admissions",
  "filename": "Fall_2026_Brochure.pdf"
}
```

**Notes:**
- Filename is sanitized: spaces → `_`, slashes → `_`
- Presigned URL expires in 1 hour
- All media stored under `raw-pdf/` prefix regardless of type

### Step 2: Upload File to S3

```
PUT {upload_url}
Content-Type: {same content_type used in step 1}
Body: <raw file bytes>
```

```typescript
await fetch(uploadUrl, {
  method: "PUT",
  headers: { "Content-Type": file.type },
  body: file,
});
```

**Important:** The `Content-Type` header MUST match what was passed in step 1, or S3 will reject the upload with a `403 SignatureDoesNotMatch`.

### Step 3: Trigger Processing

```
POST /v1/universities/{uid}/categories/{cat}/media/process
```

**Request Body:**
```json
{
  "s3_key": "raw-pdf/gmu/manual/Fall_2026_Brochure.pdf",   // required — from step 1
  "filename": "Fall_2026_Brochure.pdf",                      // optional
  "content_type": "application/pdf"                          // optional, default "application/pdf"
}
```

**Response:**
```json
{
  "status": "processing",
  "s3_key": "raw-pdf/gmu/manual/Fall_2026_Brochure.pdf"
}
```

This sends an SQS message that triggers the Media Processor Lambda to create a `.metadata.json` sidecar and register the file in the URL registry.

### Supported File Types

| Type | Extensions | MIME Examples |
|------|-----------|--------------|
| PDF | `.pdf` | `application/pdf` |
| Image | `.jpg`, `.jpeg`, `.png`, `.gif`, `.webp`, `.bmp` | `image/jpeg`, `image/png` |
| Audio | `.mp3`, `.wav`, `.ogg`, `.flac`, `.aac`, `.m4a` | `audio/mpeg`, `audio/wav` |
| Video | `.mp4`, `.avi`, `.mov`, `.mkv`, `.webm` | `video/mp4`, `video/webm` |

### Complete Upload Example

```typescript
async function uploadMedia(uid: string, category: string, file: File) {
  // Step 1: Get presigned URL
  const urlData = await apiFetch<UploadUrlResponse>(
    `/v1/universities/${uid}/categories/${category}/media/upload-url`,
    {
      method: "POST",
      body: JSON.stringify({
        filename: file.name,
        content_type: file.type || "application/octet-stream",
      }),
    }
  );

  // Step 2: Upload to S3
  const uploadRes = await fetch(urlData.upload_url, {
    method: "PUT",
    headers: { "Content-Type": file.type || "application/octet-stream" },
    body: file,
  });
  if (!uploadRes.ok) throw new Error(`S3 upload failed: ${uploadRes.status}`);

  // Step 3: Trigger processing
  await apiFetch(`/v1/universities/${uid}/categories/${category}/media/process`, {
    method: "POST",
    body: JSON.stringify({
      filename: urlData.filename,
      s3_key: urlData.s3_key,
      content_type: file.type || "application/octet-stream",
    }),
  });

  return urlData.s3_key;
}
```

---

## 6. Pipeline (Crawl Jobs)

### Start Pipeline

```
POST /v1/universities/{uid}/pipeline
```

**Request Body:**
```json
{
  "refresh_mode": "incremental",   // "full" | "incremental" | "domain"
  "domain": ""                     // required only if refresh_mode is "domain"
}
```

**Refresh modes:**

| Mode | What it does |
|------|-------------|
| `full` | Re-crawl all known URLs + discover new seeds + run classification |
| `incremental` | Re-crawl only stale URLs (per freshness window), skip classification |
| `domain` | Crawl/re-crawl only URLs matching this specific domain |

**Response (202 Accepted):**
```json
{
  "job_id": "gmu-incremental-20260302-021500",
  "university_id": "gmu",
  "refresh_mode": "incremental",
  "overall_status": "running",
  "crawl_stage": { "status": "running", "started_at": "2026-03-02T02:15:00+00:00" },
  "clean_stage": { "status": "pending" },
  "classify_stage": { "status": "pending" }
}
```

---

### List Jobs

```
GET /v1/universities/{uid}/pipeline?limit=20&next_token=...
```

**Query Parameters:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `limit` | int | 20 | Jobs per page (max 100) |
| `next_token` | string | — | Pagination cursor |

**Response:**
```json
{
  "jobs": [
    {
      "job_id": "gmu-incremental-20260302-021500",
      "university_id": "gmu",
      "created_at": "2026-03-02T02:15:00+00:00",
      "refresh_mode": "incremental",
      "domain": "",
      "triggered_by": "api",
      "overall_status": "completed",
      "execution_arn": "arn:aws:states:...",
      "crawl_stage": {
        "status": "completed",
        "started_at": "...",
        "completed_at": "...",
        "total": 450,
        "completed": 430,
        "pending": 0,
        "failed": 20,
        "queue": { "available": 0, "in_flight": 0 }
      },
      "clean_stage": {
        "status": "completed",
        "started_at": "...",
        "completed_at": "...",
        "completed": 430,
        "queue": { "available": 0, "in_flight": 0 }
      },
      "classify_stage": {
        "status": "skipped",
        "completed": 10500,
        "total": 430,
        "classify_triggered": false,
        "batch_jobs": [],
        "batch_jobs_done": true
      },
      "ttl": 1717343700
    }
  ],
  "count": 1,
  "next_token": null
}
```

Jobs are returned **newest first**.

---

### Get Live Job Progress

```
GET /v1/universities/{uid}/pipeline/{job_id}
```

Returns the same job object as above, but for running jobs it augments with **real-time data**: live crawl counts, queue depths, stage transition detection.

**Response:** Same shape as a single job in the list above.

**Stage status values:**

| Status | Meaning |
|--------|---------|
| `pending` | Stage not started yet |
| `running` | Stage actively processing |
| `completed` | Stage finished successfully |
| `waiting` | Clean→classify transition: waiting for orchestrator to trigger classification |
| `skipped` | Incremental crawl: classification was skipped |

**Overall status values:**

| Status | Meaning |
|--------|---------|
| `running` | Pipeline actively executing |
| `completed` | All stages finished |
| `failed` | Step Functions execution failed/aborted/timed out |
| `cancelled` | Stopped via reset or manual cancellation |

**Stage transition flow:**

```
crawl: running → completed (when queue empty + no pending + total > 0)
clean: pending → running → completed (when crawl done + processing queue empty)
classify:
  [full]        pending → waiting → completed (when batch jobs done + SFN succeeded)
  [incremental] pending → skipped (when clean done + SFN succeeded)
```

---

### Polling Pattern for Pipeline Progress

```typescript
function usePipelineProgress(uid: string, jobId: string) {
  const [job, setJob] = useState<PipelineJob | null>(null);

  useEffect(() => {
    if (!jobId) return;

    const poll = async () => {
      const data = await apiFetch<PipelineJob>(
        `/v1/universities/${uid}/pipeline/${jobId}`
      );
      setJob(data);

      // Stop polling when job is no longer running
      if (data.overall_status !== "running") return;

      // Poll every 10 seconds for running jobs
      setTimeout(poll, 10_000);
    };

    poll();
  }, [uid, jobId]);

  return job;
}
```

**Recommended poll interval:** 10 seconds. This endpoint makes ~30 AWS API calls per request, so avoid polling faster than every 5 seconds.

---

## 7. Knowledge Base Sync

### Start KB Sync

```
POST /v1/universities/{uid}/kb/sync
```

Triggers Bedrock Knowledge Base ingestion for all configured data sources.

**Request Body:** `{}` (empty object)

**Response (202 Accepted):**
```json
{
  "university_id": "gmu",
  "knowledge_base_id": "XXXXXXXXXX",
  "ingestion_jobs": [
    {
      "data_source_id": "YYYYYYYYYY",
      "ingestion_job_id": "abc-123",
      "status": "STARTING"
    }
  ]
}
```

**Side effect:** Clears the `pending_kb_sync` flag — the "pages changed" warning banner should disappear after calling this.

**Errors:**
- `400` — No `kb_config` in university config, or missing `knowledge_base_id` / `data_source_ids`

---

### Get KB Sync Status

```
GET /v1/universities/{uid}/kb/sync
```

Returns the 3 most recent ingestion jobs per data source.

**Response:**
```json
{
  "university_id": "gmu",
  "knowledge_base_id": "XXXXXXXXXX",
  "data_sources": [
    {
      "data_source_id": "YYYYYYYYYY",
      "recent_jobs": [
        {
          "ingestion_job_id": "abc-123",
          "status": "COMPLETE",
          "started_at": "2026-03-01T10:00:00+00:00",
          "updated_at": "2026-03-01T10:15:00+00:00",
          "statistics": {
            "numberOfDocumentsScanned": 10500,
            "numberOfDocumentsFailed": 3,
            "numberOfNewDocumentsIndexed": 127,
            "numberOfModifiedDocumentsIndexed": 45,
            "numberOfDocumentsDeleted": 10
          }
        }
      ]
    }
  ]
}
```

**Ingestion status values:** `STARTING`, `IN_PROGRESS`, `COMPLETE`, `FAILED`

---

## 8. Freshness & Schedule Settings

### Get Freshness Windows

```
GET /v1/universities/{uid}/freshness
```

Returns per-category freshness windows (days) and schedule enable/disable flags.

**Response:**
```json
{
  "university_id": "gmu",
  "windows": {
    "admissions": 7,
    "financial_aid": 7,
    "events": 3,
    "news": 3,
    "about": 60,
    "policies": 60,
    "academic_programs": 14,
    "course_catalog": 14,
    "student_services": 14,
    "housing_dining": 14,
    "campus_life": 30,
    "athletics": 7,
    "faculty_staff": 30,
    "library": 30,
    "other": 30,
    "unknown": 14
  },
  "schedule": {
    "incremental_enabled": true,
    "full_enabled": true
  }
}
```

**Freshness windows:** number of days after which a page in that category is considered stale and eligible for re-crawling.

**Schedule flags:**
- `incremental_enabled: false` → daily incremental crawl is skipped for this university
- `full_enabled: false` → weekly full crawl is skipped

---

### Save Freshness Settings

```
POST /v1/universities/{uid}/freshness
```

**Request Body:**
```json
{
  "windows": {
    "admissions": 3,
    "events": 1,
    "about": 30
  },
  "schedule": {
    "incremental_enabled": true,
    "full_enabled": false
  }
}
```

**Validation:**
- Each window value must be an integer between 1 and 3650
- Schedule flags default to `true` if omitted

**Response:**
```json
{
  "status": "saved",
  "university_id": "gmu",
  "windows": { "admissions": 3, "events": 1, "about": 30 },
  "schedule": { "incremental_enabled": true, "full_enabled": false }
}
```

---

## 9. Classification Status

### Get Classification Jobs

```
GET /v1/universities/{uid}/classification?limit=10
```

Returns batch classification job history (Bedrock batch inference).

**Query Parameters:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `limit` | int | 10 | Max jobs to return (max 50) |

**Response:**
```json
{
  "university_id": "gmu",
  "total_jobs": 3,
  "status_summary": {
    "Completed": 2,
    "InProgress": 1
  },
  "jobs": [
    {
      "job_name": "classify-gmu-20260228-021500",
      "job_arn": "arn:aws:bedrock:...",
      "status": "Completed",
      "model_id": "anthropic.claude-3-haiku-20240307-v1:0",
      "submitted_at": "2026-02-28T02:15:00+00:00",
      "last_modified_at": "2026-02-28T04:30:00+00:00",
      "end_time": "2026-02-28T04:30:00+00:00",
      "message": "",
      "input_s3_uri": "s3://university-kb-content-.../batch-jobs/gmu/input.jsonl",
      "output_s3_uri": "s3://university-kb-content-.../batch-jobs/gmu/"
    }
  ]
}
```

**Batch job status values:** `Submitted`, `Validating`, `Scheduled`, `InProgress`, `Completed`, `PartiallyCompleted`, `Failed`, `Stopped`

---

## 10. DLQ Inspection

### Get DLQ Report

```
GET /v1/universities/{uid}/dlq
```

Non-destructive peek at dead letter queue messages. Messages remain in the queue after reading.

**Response:**
```json
{
  "university_id": "gmu",
  "total_failed": 5,
  "queues": [
    {
      "name": "crawl-dlq",
      "depth": 3,
      "messages": [
        {
          "url": "https://admissions.gmu.edu/broken-page",
          "university_id": "gmu",
          "receive_count": 3,
          "sent_at": "1709337600000",
          "body_preview": "{\"url\":\"https://admissions.gmu.edu/broken-page\",\"university_id\":\"gmu\"...}"
        }
      ]
    },
    {
      "name": "processing-dlq",
      "depth": 2,
      "messages": [...]
    },
    {
      "name": "pdf-processing-dlq",
      "depth": 0,
      "messages": []
    }
  ]
}
```

**Notes:**
- `depth` is approximate (SQS provides approximate counts)
- Up to 10 messages sampled per queue
- `sent_at` is a Unix timestamp in milliseconds
- `receive_count` shows how many times the message was attempted before landing in DLQ (always >= 3)

---

## 11. Reset / Danger Zone

### Reset University Data

```
POST /v1/universities/{uid}/reset
```

**Request Body:**
```json
{
  "scope": "all"    // "all" | "classification"
}
```

| Scope | Deletes | Keeps |
|-------|---------|-------|
| `all` | All crawled data, URL registry entries, entity store, pipeline jobs, raw HTML, clean content, summaries | University config (`configs/{uid}.json`) |
| `classification` | Classification results, metadata sidecars, entity store, batch job artifacts. Resets `page_category` and `processing_status → "cleaned"` | Raw HTML, clean markdown, config, pipeline jobs |

**Response:**
```json
{
  "scope": "all",
  "university_id": "gmu",
  "pipelines_stopped": 1,
  "url_registry_deleted": 12450,
  "entity_store_deleted": 5200,
  "pipeline_jobs_deleted": 15,
  "s3_objects_deleted": 35000
}
```

Or for `scope: "classification"`:
```json
{
  "scope": "classification",
  "university_id": "gmu",
  "pipelines_stopped": 0,
  "entity_store_deleted": 5200,
  "sidecars_deleted": 10500,
  "batch_jobs_deleted": 24,
  "urls_reset": 10500
}
```

**Warning:** Both scopes automatically stop any running pipelines first. This is a destructive operation — confirm with the user before calling.

---

## Enums & Constants

### Valid Categories (20)

```typescript
const VALID_CATEGORIES = [
  "admissions", "financial_aid", "academic_programs", "course_catalog",
  "student_services", "housing_dining", "campus_life", "athletics",
  "faculty_staff", "library", "it_services", "policies",
  "events", "news", "about", "careers", "alumni", "other",
  "low_value", "excluded"
] as const;

type Category = typeof VALID_CATEGORIES[number];
```

### Category Display Labels

```typescript
const CATEGORY_LABELS: Record<Category, string> = {
  academic_programs: "Academic Programs",
  admissions: "Admissions",
  financial_aid: "Financial Aid",
  course_catalog: "Course Catalog",
  student_services: "Student Services",
  housing_dining: "Housing & Dining",
  campus_life: "Campus Life",
  athletics: "Athletics",
  faculty_staff: "Faculty & Staff",
  library: "Library",
  it_services: "IT Services",
  policies: "Policies",
  events: "Events",
  news: "News",
  about: "About",
  careers: "Careers",
  alumni: "Alumni",
  other: "Other",
  low_value: "Low Value",
  excluded: "Excluded",
};
```

### Crawl Status Values

```typescript
type CrawlStatus =
  | "pending"          // Registered, waiting to be crawled
  | "crawled"          // Successfully fetched
  | "error"            // Temporary error (may retry)
  | "failed"           // Permanent failure
  | "dead"             // 404 / gone
  | "redirected"       // HTTP 3xx
  | "blocked_robots"   // Blocked by robots.txt
  | "skipped_depth";   // Exceeded max crawl depth
```

### Processing Status Values

```typescript
type ProcessingStatus =
  | "cleaned"          // HTML → markdown complete
  | "classified"       // Batch classification assigned page_category
  | "processed_media"  // Media processor created metadata sidecar
  | "unprocessed";     // Not yet cleaned
```

### Pipeline Overall Status

```typescript
type PipelineStatus = "running" | "completed" | "failed" | "cancelled";
```

### Refresh Modes

```typescript
type RefreshMode = "full" | "incremental" | "domain";
```

---

## Polling & Real-Time Patterns

### Recommended Caching Strategy

| Endpoint | Cache? | TTL | Notes |
|----------|--------|-----|-------|
| GET /universities | Yes | 60s | Rarely changes |
| GET /stats | Yes | 15s | Changes during active crawl |
| GET /categories | Yes | 15s | Changes during classification |
| GET /.../pages | Yes | 15s | Paginated, changes during curation |
| GET /pipeline | Yes | 15s | Changes when jobs start/complete |
| GET /pipeline/{id} | No | — | Must be fresh for live progress |
| GET /kb/sync | Yes | 30s | Only changes during ingestion |
| GET /freshness | Yes | 60s | Admin-only setting |
| GET /classification | Yes | 30s | Batch jobs run for hours |
| GET /dlq | No | — | Peek should be fresh |

### Mutation → Cache Invalidation

After any POST/DELETE that modifies data, invalidate related caches:

| Mutation | Invalidate |
|----------|-----------|
| POST /pipeline | GET /pipeline, GET /pipeline/{id}, GET /stats |
| POST /.../pages | GET /.../pages, GET /categories, GET /stats |
| DELETE /.../pages | GET /.../pages, GET /categories, GET /stats |
| POST /media/upload-url + /media/process | GET /.../pages, GET /categories |
| POST /.../rename | GET /categories, GET /.../pages (both old and new) |
| POST /.../delete | GET /categories, GET /.../pages |
| POST /kb/sync | GET /kb/sync, GET /stats (clears pending_kb_sync) |
| POST /freshness | GET /freshness |
| POST /reset | ALL caches for this university |

---

## Error Handling

### Common Error Patterns

```typescript
async function safeApiFetch<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    headers: {
      "Content-Type": "application/json",
      "X-Api-Key": process.env.NEXT_PUBLIC_API_KEY!,
    },
    ...options,
  });

  if (res.status === 400) {
    const body = await res.json();
    // Show validation error to user
    throw new ValidationError(body.error);
  }

  if (res.status === 404) {
    throw new NotFoundError(`Resource not found`);
  }

  if (res.status === 500) {
    const body = await res.json();
    // Log for debugging, show generic message to user
    console.error("API 500:", body.error);
    throw new ServerError("Something went wrong. Please try again.");
  }

  if (!res.ok) {
    throw new Error(`Unexpected API error: ${res.status}`);
  }

  return res.json();
}
```

### Partial Success Handling

Several endpoints return partial results (some succeed, some fail):
- `POST /.../pages` → `{ added: [...], errors: [...] }`
- `DELETE /.../pages` → `{ removed: [...], errors: [...] }`
- `POST /.../rename` → `{ pages_moved: N, errors: [...] }`

Always check the `errors` array and surface them to the user.

---

## TypeScript Types

Complete type definitions for all API responses:

```typescript
// ─── University ─────────────────────────────────────────
interface University {
  university_id: string;
  name: string;
}

interface UniversityConfig {
  university_id: string;
  name: string;
  root_domain: string;
  seed_urls: string[];
  crawl_config: {
    max_concurrent_requests: number;
    requests_per_second_per_domain: number;
    max_pages: number;
    max_crawl_depth: number;
    request_timeout_seconds: number;
    allowed_domain_patterns: string[];
    exclude_extensions: string[];
    pdf_extensions: string[];
    exclude_path_patterns: string[];
    exclude_domains: string[];
    include_subdomains: boolean;
  };
  freshness_windows_days: Record<string, number>;
  rate_limits: { default_rps: number };
  kb_config?: {
    knowledge_base_id: string;
    data_source_ids: string[];
  };
}

// ─── Stats ──────────────────────────────────────────────
interface KBIngestionStats {
  ingested_pages: number;
  failed_pages: number;
  scanned_pages: number;
  new_indexed: number;
  modified_indexed: number;
  deleted: number;
  last_sync_status: string | null;
  last_sync_at: string | null;
}

interface DashboardStats {
  university_id: string;
  name: string;
  total_urls: number;
  total_discovered_urls: number;
  urls_by_crawl_status: Record<string, number>;
  urls_by_processing_status: Record<string, number>;
  total_content_pages: number;
  classified_pages: number;
  unclassified_pages: number;
  total_media_files: number;
  media_by_type: Record<string, number>;
  kb_ingestion: KBIngestionStats;
  pages_changed: number;
  pending_kb_sync: boolean;
  dead_urls: number;
  last_crawled_at: string | null;
  days_since_crawl: number | null;
  stale_categories: string[];
  crawl_completed_at: string | null;
  config: {
    seed_urls_count: number;
    max_crawl_depth: number;
    allowed_domain_patterns: string[];
  } | null;
}

// ─── Categories ─────────────────────────────────────────
interface CategoryCard {
  category: string;
  label: string;
  count: number;
}

interface CategoriesResponse {
  university_id: string;
  total_pages: number;
  categories: CategoryCard[];
}

// ─── Pages ──────────────────────────────────────────────
interface Page {
  url: string;
  url_hash: string;
  domain: string;
  subcategory: string | null;
  processing_status: string;
  crawl_status: string;
  content_type: string | null;
  content_length: number | null;
  last_crawled_at: string | null;
}

interface PagesResponse {
  university_id: string;
  category: string;
  pages: Page[];
  count: number;
  next_token: string | null;
}

// ─── Media Upload ───────────────────────────────────────
interface UploadUrlResponse {
  upload_url: string;
  s3_key: string;
  category: string;
  filename: string;
}

interface ProcessMediaResponse {
  status: "processing";
  s3_key: string;
}

// ─── Pipeline ───────────────────────────────────────────
interface QueueDepth {
  available: number;
  in_flight: number;
}

interface CrawlStage {
  status: "pending" | "running" | "completed";
  started_at?: string;
  completed_at?: string;
  total?: number;
  completed?: number;
  pending?: number;
  failed?: number;
  queue?: QueueDepth;
}

interface CleanStage {
  status: "pending" | "running" | "completed";
  started_at?: string;
  completed_at?: string;
  completed?: number;
  queue?: QueueDepth;
}

interface ClassifyStage {
  status: "pending" | "waiting" | "running" | "completed" | "skipped";
  started_at?: string;
  completed_at?: string;
  completed?: number;
  total?: number;
  classify_triggered?: boolean;
  batch_jobs?: Array<{ job_arn: string; status: string }>;
  batch_jobs_done?: boolean;
}

interface PipelineJob {
  job_id: string;
  university_id: string;
  created_at: string;
  refresh_mode: "full" | "incremental" | "domain";
  domain: string;
  triggered_by: string;
  overall_status: "running" | "completed" | "failed" | "cancelled";
  execution_arn: string;
  crawl_stage: CrawlStage;
  clean_stage: CleanStage;
  classify_stage: ClassifyStage;
  ttl: number;
}

// ─── KB Sync ────────────────────────────────────────────
interface IngestionJob {
  ingestion_job_id: string;
  status: string;
  started_at: string;
  updated_at: string;
  statistics: {
    numberOfDocumentsScanned?: number;
    numberOfDocumentsFailed?: number;
    numberOfNewDocumentsIndexed?: number;
    numberOfModifiedDocumentsIndexed?: number;
    numberOfDocumentsDeleted?: number;
  };
}

interface KBSyncResponse {
  university_id: string;
  knowledge_base_id: string;
  data_sources: Array<{
    data_source_id: string;
    recent_jobs?: IngestionJob[];
    error?: string;
  }>;
}

// ─── Freshness ──────────────────────────────────────────
interface FreshnessResponse {
  university_id: string;
  windows: Record<string, number>;
  schedule: {
    incremental_enabled: boolean;
    full_enabled: boolean;
  };
}

// ─── Classification ─────────────────────────────────────
interface ClassificationJob {
  job_name: string;
  job_arn: string;
  status: string;
  model_id: string;
  submitted_at: string | null;
  last_modified_at: string | null;
  end_time: string | null;
  message: string;
  input_s3_uri: string;
  output_s3_uri: string;
}

interface ClassificationResponse {
  university_id: string;
  total_jobs: number;
  status_summary: Record<string, number>;
  jobs: ClassificationJob[];
}

// ─── DLQ ────────────────────────────────────────────────
interface DLQMessage {
  url: string;
  university_id: string;
  receive_count: number;
  sent_at: string;
  body_preview: string;
}

interface DLQQueue {
  name: string;
  depth: number;
  messages: DLQMessage[];
  error?: string;
}

interface DLQResponse {
  university_id: string;
  total_failed: number;
  queues: DLQQueue[];
}

// ─── Reset ──────────────────────────────────────────────
interface ResetResponse {
  scope: "all" | "classification";
  university_id: string;
  pipelines_stopped: number;
  // scope=all:
  url_registry_deleted?: number;
  entity_store_deleted?: number;
  pipeline_jobs_deleted?: number;
  s3_objects_deleted?: number;
  // scope=classification:
  sidecars_deleted?: number;
  batch_jobs_deleted?: number;
  urls_reset?: number;
}
```

---

## Complete Endpoint Summary

| # | Method | Path | Description |
|---|--------|------|-------------|
| 1 | GET | `/v1/universities` | List configured universities |
| 2 | GET | `/v1/universities/{uid}/stats` | Dashboard overview stats |
| 3 | GET | `/v1/universities/{uid}/config` | Get university config |
| 4 | POST | `/v1/universities/{uid}/config` | Save university config |
| 5 | GET | `/v1/universities/{uid}/categories` | Category cards with counts |
| 6 | GET | `/v1/universities/{uid}/categories/{cat}/pages` | Paginated pages list |
| 7 | POST | `/v1/universities/{uid}/categories/{cat}/pages` | Add URLs to category |
| 8 | DELETE | `/v1/universities/{uid}/categories/{cat}/pages` | Remove URLs |
| 9 | POST | `/v1/universities/{uid}/categories/{cat}/media/upload-url` | Get presigned upload URL |
| 10 | POST | `/v1/universities/{uid}/categories/{cat}/media/process` | Trigger media processing |
| 11 | POST | `/v1/universities/{uid}/categories/{cat}/rename` | Rename category |
| 12 | POST | `/v1/universities/{uid}/categories/{cat}/delete` | Delete category |
| 13 | POST | `/v1/universities/{uid}/pipeline` | Start crawl pipeline |
| 14 | GET | `/v1/universities/{uid}/pipeline` | List pipeline jobs |
| 15 | GET | `/v1/universities/{uid}/pipeline/{job_id}` | Live job progress |
| 16 | POST | `/v1/universities/{uid}/kb/sync` | Trigger KB sync |
| 17 | GET | `/v1/universities/{uid}/kb/sync` | KB sync status |
| 18 | GET | `/v1/universities/{uid}/freshness` | Get freshness windows |
| 19 | POST | `/v1/universities/{uid}/freshness` | Save freshness windows |
| 20 | GET | `/v1/universities/{uid}/classification` | Classification job status |
| 21 | GET | `/v1/universities/{uid}/dlq` | DLQ inspection |
| 22 | POST | `/v1/universities/{uid}/reset` | Reset university data |
