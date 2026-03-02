# Recrawl Process & Content Cleaning

## Overview

The recrawl process is how the system keeps university content up to date. It has 3 layers of deduplication to avoid re-processing unchanged content, and a category-based freshness system that re-crawls volatile pages more often than stable ones.

---

## 1. Content Cleaning Steps (Content Cleaner Lambda)

**File:** `lambdas/content-cleaner/handler.py`

When a message arrives from the processing queue, the content cleaner runs these steps in order:

### Step 1: Pre-checks (lines 220-234)
- If `action == "delete"` → delete the `.md` file and `.metadata.json` sidecar from S3, return
- If content type is PDF or other media (image, audio, video) → skip (media files go to the separate media processor queue)
- If no `s3_key` → skip

### Step 2: Fetch raw HTML from S3 (lines 237-244)
- Reads `raw-html/{university_id}/{domain}/{url_hash}.html` from S3
- If the object doesn't exist, marks URL as `s3_not_found` in DynamoDB

### Step 3: Extract clean content via Trafilatura (lines 246-253)
- Calls `trafilatura.extract()` twice:
  - `output_format='txt'` → plain text (the actual content)
  - `output_format='xml'` → structured XML (preserves heading hierarchy)
- Settings: `include_tables=True`, `include_links=True`, `include_images=False`, `include_comments=False`, `favor_precision=False` (favors recall — get more content even if slightly noisy)
- Also calls `trafilatura.extract_metadata()` → title, description, author, date, sitename
- Fallback title extraction from `<title>` or `<h1>` tags if Trafilatura misses it
- If no text extracted → marks URL as `empty_content`, returns

### Step 4: Extract internal links from raw HTML (lines 255-262)
- Regex-based href extraction from the raw HTML (not the cleaned text)
- Normalizes URLs: lowercase scheme/host, remove trailing slashes, sort query params, strip tracking parameters
- Filters by allowed domain patterns and excluded domains from university config
- Max 500 links per page

### Step 5: Build markdown with heading structure (lines 264-265)
- If XML output available → `xml_to_markdown()` reconstructs heading hierarchy (`# h1`, `## h2`, etc.)
- Falls back to plain text if XML parsing fails
- Prepends page title as `# Title`
- Appends description as `---\n*description*` footer

### Step 6: Store clean markdown in S3 (lines 267-280)
- Key: `clean-content/{university_id}/{domain}/{url_hash}.md`
- S3 metadata: `source_url`, `university_id`, `domain`, `extracted_at`
- Content-Type: `text/markdown`

### Step 7: Write links_to in DynamoDB (lines 284-285)
- Conditional write: only if `links_to` doesn't already exist on the record
- This preserves original link discovery and prevents overwrites during recrawl

### Step 8: Push to classification queue (lines 287-309)
- Sends message to SQS classification queue with:
  - url, university_id, domain, url_hash
  - clean_s3_key, raw_s3_key
  - title, description, content_preview (first 1000 chars)
  - content_length, depth, crawled_at, extracted_at
- This feeds the batch classifier prepare step later

### Step 9: Update URL registry (lines 311-312)
- Sets `processing_status = "cleaned"` in DynamoDB
- Sets `processing_updated_at` timestamp

---

## 2. Recrawl Deduplication — How We Detect Unchanged Pages

The crawler worker has **3 layers** of deduplication, checked in order. Each subsequent layer is more expensive, so cheap checks come first.

### Layer 1: Freshness Check (cheapest — 1 DynamoDB read)

**File:** `lambdas/crawler-worker/handler.py`, lines 395-415

```
is_recently_crawled(url, freshness_hours=24)
```

- Reads `crawl_status` and `last_crawled_at` from DynamoDB
- If the page was crawled within the last 24 hours, skip entirely
- No HTTP request made, no content fetched
- This prevents re-processing the same URL if it appears in the queue multiple times during a single crawl run

### Layer 2: HTTP Conditional Request (cheap — no content transfer if unchanged)

**File:** `lambdas/crawler-worker/handler.py`, lines 511-585

```
fetch_page(url)
  → sends If-None-Match: {stored_etag}
  → sends If-Modified-Since: {stored_last_modified}
```

- Before fetching, loads the stored `etag` and `last_modified` from DynamoDB (from the previous crawl)
- Sends these as HTTP conditional headers to the university server
- If the server responds 304 Not Modified → the page hasn't changed since last crawl
  - Returns immediately with no content downloaded
  - No S3 write, no processing queue message
  - Crawler marks URL as `crawled` and updates `last_crawled_at`
- This relies on the university server supporting conditional requests (most do)

### Layer 3: Content Hash Comparison (most expensive — full download + hash)

**File:** `lambdas/crawler-worker/handler.py`, lines 334-344

```
content_hash = hashlib.sha256(content.encode()).hexdigest()
is_content_unchanged(url, content_hash)
```

- After downloading the full page content, computes SHA-256 hash
- Compares with stored `content_hash` in DynamoDB (from previous crawl)
- If hashes match → content is identical, skip processing
  - Marks URL as `crawled`, updates `last_crawled_at`
  - Does NOT push to processing queue, does NOT overwrite S3
- If hashes differ → content changed, proceed with S3 storage + queue message
- This catches changes the server didn't report via ETag/Last-Modified

### Summary: What happens at each layer

```
URL arrives in crawl queue
  │
  ├─ Layer 1: Crawled < 24h ago?
  │   YES → skip (no HTTP request)
  │   NO  ↓
  │
  ├─ Layer 2: HTTP 304 Not Modified?
  │   YES → skip (no content transfer)
  │   NO  ↓
  │
  ├─ Layer 3: SHA-256 hash matches stored hash?
  │   YES → skip (no S3 write, no queue message)
  │   NO  ↓
  │
  └─ Content changed → store in S3 → push to processing queue → clean → classify
```

---

## 3. Freshness Windows — When Pages Become Stale

Different page categories have different freshness windows. When running an incremental crawl, only pages past their freshness window get re-queued.

**Storage:** DynamoDB `entity-store-dev`, PK=`university_id`, SK=`freshness_config`

**Schema:**
```json
{
  "university_id": "minu",
  "entity_key": "freshness_config",
  "entity_type": "freshness_config",
  "windows": {
    "admissions": 2,
    "events": 1,
    "about": 14
  },
  "incremental_enabled": true,
  "full_enabled": true,
  "updated_at": "2026-03-01T10:00:00+00:00"
}
```

Only categories that need non-default windows need to be listed — any category not in `windows` falls back to the default.

**Default:** 1 day for all categories if no record exists.

**Configure via:** `POST /v1/universities/{uid}/freshness` — or the "Schedule & Freshness Windows" expander in the Maintenance tab of the dashboard (hit "Save Settings").

**Schedule flags:** `incremental_enabled` and `full_enabled` (both default `true`) control whether `crawl-scheduler-dev` triggers this university's daily incremental or weekly full crawl. Disabled universities are skipped entirely by the scheduler.

### Staleness detection logic (lines 265-326)

```python
freshness_days = freshness_windows.get(page_category, 14)
cutoff = now - timedelta(days=freshness_days)

if last_crawled_at < cutoff:
    mark_pending(url)
    push_to_queue(url, ...)
```

- Uses `university-status-index` GSI to query only `crawl_status = "crawled"` URLs
- Looks up each URL's `page_category` to get the right freshness window
- If `last_crawled_at` is before the cutoff → page is stale → push to crawl queue

---

## 4. Incremental vs Full Crawl

### Incremental (daily at 2 AM UTC)

```
EventBridge (cron) → crawl-scheduler-dev Lambda → (per-university check) → Step Functions → Orchestrator (task: queue_stale)
```

The `crawl-scheduler-dev` Lambda runs first and applies these checks for each university before starting anything:

1. Lists all configured universities from S3 (`configs/` prefix)
2. For each university, checks:
   - Is a pipeline already running? (`pipeline-jobs-dev` GSI query) → skip
   - Is the schedule enabled? (`freshness_config.incremental_enabled` in entity-store) → skip if disabled
   - For incremental: is content stale? (min freshness window vs last completed crawl date) → skip if fresh
3. Only then starts a Step Functions execution

Once running, the orchestrator queries all crawled URLs via `university-status-index` GSI, checks each URL's `last_crawled_at` against its category's freshness window, and re-queues only stale URLs (set to `pending`, pushed to crawl queue). The crawler worker picks them up and runs the 3-layer dedup — only changed content proceeds.

**Classification is skipped for incremental crawls.** After crawl + clean complete, Step Functions routes directly to `GenerateSummary` via a `ShouldClassify` Choice state — the batch classifier is not invoked.

- The `.md.metadata.json` sidecar is NOT deleted for changed pages — the existing category is preserved
- A `pages_changed` counter in entity-store is atomically incremented for each changed page
- At the end of the crawl, `generate_summary` writes `kb_sync_status.status = "pending_sync"` if any pages changed, or `"no_changes"` if not
- The dashboard sidebar shows a notification banner when `pending_kb_sync = true`
- The admin runs KB Sync manually from the Ingestion tab to push updated content to Bedrock KB

**Pipeline status tracking for incremental:**
The pipeline dashboard (`GET /pipeline/{job_id}`) marks incremental jobs as `completed` via a new `incremental_done` path:
- Condition: `refresh_mode == "incremental" AND clean_done AND sfn_succeeded`
- Sets `classify_stage.status = "skipped"` (instead of waiting for classification that never runs)

### Full (weekly on Sunday at 3 AM UTC)

```
EventBridge (cron) → crawl-scheduler-dev Lambda → (per-university check) → Step Functions → Orchestrator (task: discover_seeds + queue_all)
```

The scheduler applies the same per-university checks as above (already running? `full_enabled` flag set?), then starts a Step Functions execution for each eligible university.

1. Re-runs seed discovery: parses sitemaps, discovers any new URLs
2. Re-queues ALL previously crawled URLs regardless of freshness
3. Crawler worker still runs 3-layer dedup — unchanged pages are skipped efficiently
4. This catches structural changes (new pages, removed pages, new sitemap entries)

---

## 5. Does Bedrock KB Handle Content Deduplication?

**Short answer: Yes, Bedrock handles it internally.**

**File:** `lambdas/dashboard-api/routes/kb.py`

When we trigger KB sync (`start_ingestion_job()`), we do NOT filter or diff content ourselves. We point Bedrock at the S3 prefix (`clean-content/{university_id}/`) and it ingests everything.

Bedrock KB manages deduplication internally:
- It tracks which documents it has already ingested by S3 key
- On re-ingestion, it compares the S3 object's ETag/LastModified
- If the S3 object hasn't changed → Bedrock skips re-embedding it
- If the content changed → Bedrock re-chunks, re-embeds, and updates the vector store

So the flow for recrawled content is:
```
Page content changed → new .md written to S3 (same key, new content)
  → KB sync triggered → Bedrock detects S3 object changed
  → Re-chunks and re-embeds the updated content
  → Old vectors replaced with new ones

Page content unchanged → no .md write (Layer 3 hash match skips it)
  → S3 object untouched → Bedrock skips it during next sync
```

Our 3-layer dedup on the crawler side also helps Bedrock — if content didn't change, we don't overwrite the S3 object, so Bedrock sees no change and skips it. No wasted embedding compute.

**Incremental crawl + KB Sync flow:**

For incremental crawls, content-cleaner does NOT delete the `.md.metadata.json` sidecar when a page's content changes. This means:
- The page's existing category is preserved (no re-classification needed)
- The `.md` file is updated with fresh content
- Bedrock KB, on the next sync, detects the `.md` S3 object's ETag changed and re-embeds it
- The `.md.metadata.json` sidecar still matches the correct category → KB vectors are updated correctly

The `pages_changed` counter in entity-store tracks how many pages were updated, and the dashboard notifies the admin to run KB Sync when pages have changed.

---

## 6. What Gets Stored After Each Stage

### After crawl (crawler worker writes to DynamoDB)
```
content_hash      — SHA-256 of raw content (for Layer 3 dedup)
etag              — HTTP ETag header (for Layer 2 dedup)
last_modified     — HTTP Last-Modified header (for Layer 2 dedup)
last_crawled_at   — timestamp (for Layer 1 dedup + freshness checks)
crawl_status      — "crawled"
s3_key            — raw-html/... or raw-pdf/... (also raw-image/, raw-audio/, raw-video/ for other media)
content_type      — "html", "pdf", "image", "audio", or "video"
content_length    — bytes
links_found       — count of new URLs discovered
links_to          — list of internal links on this page
```

### After cleaning (content cleaner writes to DynamoDB + entity-store)
```
processing_status     — "cleaned"
processing_updated_at — timestamp
links_to              — internal links (conditional write, won't overwrite)
```

entity-store writes (incremental crawls only):
```
entity-store kb_sync_status  — pages_changed atomically incremented for each changed page
```

### After media processing (media processor Lambda writes to DynamoDB + S3)

**File:** `lambdas/pdf-processor/handler.py` (handles all media types, not just PDFs)

```
processing_status     — "processed_media" (was "processed_pdf" before generalization)
processing_updated_at — timestamp
```

The `.md.metadata.json` sidecar written by the media processor includes a `content_type` field that is now dynamic — set to `"pdf"`, `"image"`, `"audio"`, or `"video"` depending on the file type (previously hardcoded to `"pdf"`). For manually uploaded files via the `/media/process` API endpoint, the `page_category` is read from the SQS message rather than inferred.

### entity-store entries written by orchestrator
```
entity-store current_crawl_mode  — written by validate_request, holds refresh_mode for content-cleaner to read
entity-store kb_sync_status      — reset to status="running", pages_changed=0 at pipeline start
                                   finalized to status="pending_sync"|"no_changes" by generate_summary
```

### After classification (batch classifier writes to DynamoDB)
```
page_category         — e.g. "admissions", "financial_aid"
entities              — extracted entities (programs, deadlines, etc.)
classification_metadata — confidence score, model version
```

The `page_category` field is what the freshness system reads on the next incremental crawl to determine how soon this page should be re-crawled.
