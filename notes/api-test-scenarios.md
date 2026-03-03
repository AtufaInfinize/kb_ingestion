# API Test Scenarios — Dashboard API

Manual test script for verifying all Dashboard API endpoints, including the new sync-status health check, operation guards (409), and KB sync tracking.

**Base URL:** `https://9mwsknkorc.execute-api.us-east-1.amazonaws.com/dev`
**Test University:** `minu` (Minerva University)

---

## Setup

```bash
API="https://9mwsknkorc.execute-api.us-east-1.amazonaws.com/dev"
UID="minu"

# Helper: pretty-print JSON
jp() { python3 -c "import sys,json; print(json.dumps(json.loads(sys.stdin.read()), indent=2))"; }
```

---

## 1. University Management

### 1.1 List Universities

```bash
curl -s "$API/v1/universities" | jp
```

**Expect:**
```json
{
  "universities": [
    { "university_id": "minu", "name": "Minerva University" },
    { "university_id": "phc", "name": "Patrick Henry College" }
  ]
}
```

### 1.2 Get University Config

```bash
curl -s "$API/v1/universities/$UID/config" | jp
```

**Expect:** Full config with `university_id`, `name`, `root_domain`, `seed_urls`, `crawl_config`, `kb_config`.

### 1.3 Save University Config

```bash
# Only test on a throwaway university or verify fields first
curl -s -X POST "$API/v1/universities/$UID/config" \
  -H "Content-Type: application/json" \
  -d '{"name": "Minerva University", "root_domain": "minerva.edu", "seed_urls": ["https://www.minerva.edu/"]}' | jp
```

**Expect:** `{ "status": "saved", "university_id": "minu", "config": { ... } }`

---

## 2. Dashboard Stats

### 2.1 Get Stats (full overview)

```bash
curl -s "$API/v1/universities/$UID/stats" | jp
```

**Expect:** All these fields present:
- `university_id`, `name`
- `total_urls`, `total_discovered_urls`
- `urls_by_crawl_status` (8 keys: pending, crawled, error, failed, dead, redirected, blocked_robots, skipped_depth)
- `urls_by_processing_status` (classified, unprocessed)
- `total_content_pages`, `classified_pages`, `unclassified_pages`
- `total_media_files`, `media_by_type` (pdf, image, audio, video, other)
- `kb_ingestion` (ingested_pages, failed_pages, scanned_pages, new_indexed, modified_indexed, deleted, last_sync_status, last_sync_at)
- `pages_changed` (integer >= 0)
- `categories_modified` (integer >= 0)
- `data_reset` (boolean)
- `pending_kb_sync` (boolean — true when any of: pages_changed > 0, categories_modified > 0, data_reset, or status == "pending_sync")
- `dead_urls`
- `last_crawled_at`, `days_since_crawl`, `stale_categories`
- `crawl_completed_at`
- `config` (seed_urls_count, max_crawl_depth, allowed_domain_patterns)

**Verify:** `pending_kb_sync` matches the formula: `pages_changed > 0 OR categories_modified > 0 OR data_reset`.

---

## 3. Sync Status Health Check

### 3.1 Basic sync-status call

```bash
curl -s "$API/v1/universities/$UID/sync-status" | jp
```

**Expect:**
```json
{
  "university_id": "minu",
  "sync_needed": false,
  "message": null,
  "reasons": {
    "pages_changed": 0,
    "categories_modified": 0,
    "data_reset": false
  },
  "status": "syncing",
  "crawl_completed_at": "...",
  "synced_at": "...",
  "pipeline_running": false
}
```

**Verify:**
- `sync_needed` matches: `pages_changed > 0 OR categories_modified > 0 OR data_reset OR status == "pending_sync"`
- `message` is `null` when `sync_needed` is false and no pipeline running; human-readable string otherwise
- `pipeline_running` is boolean
- Response time < 500ms (only 2 DynamoDB calls vs 8 for stats)

### 3.2 sync-status after category change

```bash
# Step 1: Record baseline
curl -s "$API/v1/universities/$UID/sync-status" | jp

# Step 2: Rename a category (triggers record_kb_sync_event)
curl -s -X POST "$API/v1/universities/$UID/categories/alumni/rename" \
  -H "Content-Type: application/json" \
  -d '{"new_category": "other"}' | jp

# Step 3: Check sync-status again
curl -s "$API/v1/universities/$UID/sync-status" | jp
```

**Expect after rename:**
- `sync_needed: true`
- `reasons.categories_modified` incremented by 1
- Other reason fields unchanged

```bash
# Step 4: Rename back to restore
curl -s -X POST "$API/v1/universities/$UID/categories/other/rename" \
  -H "Content-Type: application/json" \
  -d '{"new_category": "alumni"}' | jp
```

**Note:** Renaming back also increments the counter (it's a new category operation).

### 3.3 sync-status counter is atomic (concurrent safe)

```bash
# Fire two category changes in quick succession
curl -s -X POST "$API/v1/universities/$UID/categories/alumni/rename" \
  -H "Content-Type: application/json" \
  -d '{"new_category": "other"}' &
curl -s -X POST "$API/v1/universities/$UID/categories/careers/rename" \
  -H "Content-Type: application/json" \
  -d '{"new_category": "other"}' &
wait

curl -s "$API/v1/universities/$UID/sync-status" | jp
```

**Expect:** `categories_modified` incremented by 2 (DynamoDB ADD is atomic).

```bash
# Restore
curl -s -X POST "$API/v1/universities/$UID/categories/other/rename" \
  -H "Content-Type: application/json" \
  -d '{"new_category": "alumni"}' > /dev/null
```

### 3.4 sync-status after KB sync (counters cleared)

```bash
# Step 1: Ensure there are pending changes
curl -s "$API/v1/universities/$UID/sync-status" | jp
# Expect: sync_needed: true, categories_modified > 0

# Step 2: Trigger KB sync
curl -s -X POST "$API/v1/universities/$UID/kb/sync" \
  -H "Content-Type: application/json" -d '{}' | jp

# Step 3: Check sync-status
curl -s "$API/v1/universities/$UID/sync-status" | jp
```

**Expect after sync:**
- `sync_needed: false`
- `reasons.categories_modified: 0`
- `reasons.data_reset: false`
- `reasons.pages_changed: 0`
- `status: "syncing"`
- `synced_at` updated to current time

### 3.5 sync-status shows pipeline_running

```bash
# Step 1: Start a pipeline
curl -s -X POST "$API/v1/universities/$UID/pipeline" \
  -H "Content-Type: application/json" \
  -d '{"refresh_mode": "incremental"}' | jp

# Step 2: Check sync-status immediately
curl -s "$API/v1/universities/$UID/sync-status" | jp
```

**Expect:** `pipeline_running: true`

### 3.6 sync-status for university with no kb_sync_status entity

```bash
curl -s "$API/v1/universities/nonexistent/sync-status" | jp
```

**Expect:**
```json
{
  "university_id": "nonexistent",
  "sync_needed": false,
  "reasons": { "pages_changed": 0, "categories_modified": 0, "data_reset": false },
  "status": null,
  "crawl_completed_at": null,
  "synced_at": null,
  "pipeline_running": false
}
```

---

## 4. Category Management

### 4.1 List Categories

```bash
curl -s "$API/v1/universities/$UID/categories" | jp
```

**Expect:** Array of `{ category, label, count }` — only categories with count > 0, `excluded` never shown.

### 4.2 Rename Category

```bash
curl -s -X POST "$API/v1/universities/$UID/categories/alumni/rename" \
  -H "Content-Type: application/json" \
  -d '{"new_category": "other"}' | jp
```

**Expect:** `{ "old_category": "alumni", "new_category": "other", "pages_moved": N, "errors": [] }`
**Side effect:** `categories_modified` incremented in sync-status.

```bash
# Verify and restore
curl -s "$API/v1/universities/$UID/sync-status" | python3 -c "import sys,json; d=json.loads(sys.stdin.read()); print('categories_modified:', d['reasons']['categories_modified'])"
```

### 4.3 Rename — invalid category

```bash
curl -s -X POST "$API/v1/universities/$UID/categories/bogus/rename" \
  -H "Content-Type: application/json" \
  -d '{"new_category": "news"}' | jp
```

**Expect:** `400` with `{ "error": "Invalid source category: bogus" }`

### 4.4 Rename — same category

```bash
curl -s -X POST "$API/v1/universities/$UID/categories/news/rename" \
  -H "Content-Type: application/json" \
  -d '{"new_category": "news"}' | jp
```

**Expect:** `400` with `{ "error": "new_category must differ from current category" }`

### 4.5 Delete Category

```bash
curl -s -X POST "$API/v1/universities/$UID/categories/low_value/delete" \
  -H "Content-Type: application/json" -d '{}' | jp
```

**Expect:** `{ "category": "low_value", "target_category": "excluded", "pages_deleted": N, "errors": [] }`
**Side effect:** `categories_modified` incremented (only if deleted > 0).

---

## 5. Page Management

### 5.1 List Pages

```bash
curl -s "$API/v1/universities/$UID/categories/academic_programs/pages?limit=5" | jp
```

**Expect:** `{ pages: [...], count: 5, next_token: "..." }`

### 5.2 List Pages with filters

```bash
curl -s "$API/v1/universities/$UID/categories/academic_programs/pages?limit=10&processing_status=classified" | jp
```

**Expect:** All returned pages have `processing_status: "classified"`.

### 5.3 Add URLs

```bash
curl -s -X POST "$API/v1/universities/$UID/categories/other/pages" \
  -H "Content-Type: application/json" \
  -d '{"urls": ["https://www.minerva.edu/test-page"], "trigger_crawl": false}' | jp
```

**Expect:** `{ "added": [{ "url": "...", "status": "registered" }], "errors": [] }`
**Side effect:** `categories_modified` incremented in sync-status.

### 5.4 Remove URLs

```bash
curl -s -X DELETE "$API/v1/universities/$UID/categories/other/pages" \
  -H "Content-Type: application/json" \
  -d '{"urls": ["https://www.minerva.edu/test-page"], "action": "mark_excluded"}' | jp
```

**Expect:** `{ "removed": [{ "url": "...", "action": "mark_excluded" }], "errors": [] }`
**Side effect:** `categories_modified` incremented in sync-status.

---

## 6. Pipeline (Crawl Jobs)

### 6.1 Start Pipeline — success

```bash
curl -s -X POST "$API/v1/universities/$UID/pipeline" \
  -H "Content-Type: application/json" \
  -d '{"refresh_mode": "incremental"}' | jp
```

**Expect (202):**
```json
{
  "job_id": "minu-incremental-YYYYMMDD-HHMMSS",
  "university_id": "minu",
  "refresh_mode": "incremental",
  "overall_status": "running",
  "crawl_stage": { "status": "running", "started_at": "..." },
  "clean_stage": { "status": "pending" },
  "classify_stage": { "status": "pending" }
}
```

### 6.2 Start Pipeline — 409 guard (duplicate)

```bash
# While the pipeline from 6.1 is still running:
curl -s -X POST "$API/v1/universities/$UID/pipeline" \
  -H "Content-Type: application/json" \
  -d '{"refresh_mode": "incremental"}' | jp
```

**Expect (409):**
```json
{
  "error": "A pipeline is already running for this university",
  "running_job_id": "minu-incremental-YYYYMMDD-HHMMSS"
}
```

### 6.3 Start Pipeline — invalid refresh_mode

```bash
curl -s -X POST "$API/v1/universities/$UID/pipeline" \
  -H "Content-Type: application/json" \
  -d '{"refresh_mode": "invalid"}' | jp
```

**Expect (400):** `{ "error": "refresh_mode must be one of: full, incremental, domain" }`

### 6.4 Start Pipeline — domain mode without domain

```bash
curl -s -X POST "$API/v1/universities/$UID/pipeline" \
  -H "Content-Type: application/json" \
  -d '{"refresh_mode": "domain"}' | jp
```

**Expect (400):** `{ "error": "domain is required for domain refresh_mode" }`

### 6.5 List Pipeline Jobs

```bash
curl -s "$API/v1/universities/$UID/pipeline?limit=5" | jp
```

**Expect:** `{ "jobs": [...], "count": N, "next_token": "..." | null }` — jobs newest first.

### 6.6 Get Live Job Progress

```bash
# Use a job_id from 6.1 or 6.5
JOB_ID="minu-incremental-20260303-084540"
curl -s "$API/v1/universities/$UID/pipeline/$JOB_ID" | jp
```

**Expect:** Full job object with live `crawl_stage.queue`, `clean_stage.queue`, stage transitions.

### 6.7 Get Job — not found

```bash
curl -s "$API/v1/universities/$UID/pipeline/nonexistent-job" | jp
```

**Expect (404):** `{ "error": "Job not found: nonexistent-job" }`

---

## 7. Knowledge Base Sync

### 7.1 Start KB Sync — success

```bash
# Ensure no pipeline is running first
curl -s -X POST "$API/v1/universities/$UID/kb/sync" \
  -H "Content-Type: application/json" -d '{}' | jp
```

**Expect (202):**
```json
{
  "university_id": "minu",
  "knowledge_base_id": "...",
  "ingestion_jobs": [
    { "data_source_id": "...", "ingestion_job_id": "...", "status": "STARTING" }
  ]
}
```

**Side effects:**
- `sync-status` shows `status: "syncing"`, all counters cleared to 0
- `sync-status.synced_at` updated

### 7.2 Start KB Sync — 409 guard (pipeline running)

```bash
# Start a pipeline first
curl -s -X POST "$API/v1/universities/$UID/pipeline" \
  -H "Content-Type: application/json" \
  -d '{"refresh_mode": "incremental"}' > /dev/null

# Then try KB sync
curl -s -X POST "$API/v1/universities/$UID/kb/sync" \
  -H "Content-Type: application/json" -d '{}' | jp
```

**Expect (409):**
```json
{
  "error": "A pipeline is currently running — sync after it completes",
  "running_job_id": "minu-incremental-..."
}
```

### 7.3 Start KB Sync — no KB configured

```bash
curl -s -X POST "$API/v1/universities/phc/kb/sync" \
  -H "Content-Type: application/json" -d '{}' | jp
```

**Expect (400):** Error about missing `kb_config` or `knowledge_base_id`.

### 7.4 Get KB Sync Status (ingestion history)

```bash
curl -s "$API/v1/universities/$UID/kb/sync" | jp
```

**Expect:** `{ "university_id": "...", "knowledge_base_id": "...", "data_sources": [{ "data_source_id": "...", "recent_jobs": [...] }] }`

---

## 8. Freshness & Schedule

### 8.1 Get Freshness Windows

```bash
curl -s "$API/v1/universities/$UID/freshness" | jp
```

**Expect:** `{ "university_id": "...", "windows": { "admissions": 7, ... }, "schedule": { "incremental_enabled": true, "full_enabled": true } }`

### 8.2 Save Freshness Windows

```bash
curl -s -X POST "$API/v1/universities/$UID/freshness" \
  -H "Content-Type: application/json" \
  -d '{"windows": {"admissions": 7, "events": 3}, "schedule": {"incremental_enabled": true, "full_enabled": true}}' | jp
```

**Expect:** `{ "status": "saved", ... }`

---

## 9. Classification

### 9.1 Get Classification Jobs

```bash
curl -s "$API/v1/universities/$UID/classification?limit=5" | jp
```

**Expect:** `{ "university_id": "...", "total_jobs": N, "status_summary": { ... }, "jobs": [...] }`

---

## 10. DLQ Inspection

### 10.1 Get DLQ Report

```bash
curl -s "$API/v1/universities/$UID/dlq" | jp
```

**Expect:** `{ "university_id": "...", "total_failed": N, "queues": [{ "name": "crawl-dlq", "depth": N, "messages": [...] }, ...] }`

---

## 11. Reset / Danger Zone

### 11.1 Reset — classification only

```bash
# WARNING: This deletes classification data. Only run on test universities.
curl -s -X POST "$API/v1/universities/$UID/reset" \
  -H "Content-Type: application/json" \
  -d '{"scope": "classification"}' | jp
```

**Expect:**
```json
{
  "scope": "classification",
  "university_id": "minu",
  "pipelines_stopped": 0,
  "entity_store_deleted": N,
  "sidecars_deleted": N,
  "batch_jobs_deleted": N,
  "urls_reset": N
}
```

**Side effects:**
- Running pipelines stopped first
- `sync-status` shows `data_reset: true`, `sync_needed: true`

### 11.2 Reset — verify sync tracking

```bash
# After reset, check sync-status
curl -s "$API/v1/universities/$UID/sync-status" | jp
```

**Expect:** `data_reset: true`, `sync_needed: true`

### 11.3 Reset — invalid scope

```bash
curl -s -X POST "$API/v1/universities/$UID/reset" \
  -H "Content-Type: application/json" \
  -d '{"scope": "invalid"}' | jp
```

**Expect (400):** `{ "error": "scope must be \"all\" or \"classification\"" }`

---

## 12. End-to-End Scenarios

These test realistic workflows combining multiple endpoints.

### Scenario A: Category curation → KB sync

Tests the full flow: admin curates categories, sync-status tracks changes, KB sync clears them.

```bash
echo "=== Step 1: Baseline ==="
curl -s "$API/v1/universities/$UID/sync-status" | jp

echo "=== Step 2: Add a URL ==="
curl -s -X POST "$API/v1/universities/$UID/categories/other/pages" \
  -H "Content-Type: application/json" \
  -d '{"urls": ["https://www.minerva.edu/e2e-test"], "trigger_crawl": false}' | jp

echo "=== Step 3: Check sync-status (should show categories_modified +1) ==="
curl -s "$API/v1/universities/$UID/sync-status" | jp

echo "=== Step 4: Remove the URL ==="
curl -s -X DELETE "$API/v1/universities/$UID/categories/other/pages" \
  -H "Content-Type: application/json" \
  -d '{"urls": ["https://www.minerva.edu/e2e-test"]}' | jp

echo "=== Step 5: Check sync-status (categories_modified +2 now) ==="
curl -s "$API/v1/universities/$UID/sync-status" | jp

echo "=== Step 6: KB sync (clears counters) ==="
curl -s -X POST "$API/v1/universities/$UID/kb/sync" \
  -H "Content-Type: application/json" -d '{}' | jp

echo "=== Step 7: Verify counters cleared ==="
curl -s "$API/v1/universities/$UID/sync-status" | jp
```

**Expected progression:**
| Step | categories_modified | sync_needed |
|------|-------------------|-------------|
| 1 | 0 (or previous value) | depends on prior state |
| 3 | previous + 1 | true |
| 5 | previous + 2 | true |
| 7 | 0 | false |

### Scenario B: Pipeline guard prevents concurrent operations

Tests that starting a pipeline blocks both duplicate pipelines and KB sync.

```bash
echo "=== Step 1: Start pipeline ==="
curl -s -X POST "$API/v1/universities/$UID/pipeline" \
  -H "Content-Type: application/json" \
  -d '{"refresh_mode": "incremental"}' | jp

echo "=== Step 2: Try second pipeline (should get 409) ==="
curl -s -X POST "$API/v1/universities/$UID/pipeline" \
  -H "Content-Type: application/json" \
  -d '{"refresh_mode": "full"}' | jp

echo "=== Step 3: Try KB sync while pipeline running (should get 409) ==="
curl -s -X POST "$API/v1/universities/$UID/kb/sync" \
  -H "Content-Type: application/json" -d '{}' | jp

echo "=== Step 4: sync-status shows pipeline_running ==="
curl -s "$API/v1/universities/$UID/sync-status" | jp

echo "=== Step 5: Category changes still allowed during pipeline ==="
curl -s -X POST "$API/v1/universities/$UID/categories/alumni/rename" \
  -H "Content-Type: application/json" \
  -d '{"new_category": "other"}' | jp
# Restore
curl -s -X POST "$API/v1/universities/$UID/categories/other/rename" \
  -H "Content-Type: application/json" \
  -d '{"new_category": "alumni"}' > /dev/null
```

**Expected results:**
| Step | Status | Key field |
|------|--------|-----------|
| 1 | 202 | `overall_status: "running"` |
| 2 | 409 | `error: "A pipeline is already running..."` |
| 3 | 409 | `error: "A pipeline is currently running..."` |
| 4 | 200 | `pipeline_running: true` |
| 5 | 200 | Category rename succeeds (no guard on category ops) |

### Scenario C: Reset triggers data_reset tracking

Tests that resetting data sets the `data_reset` flag in sync-status.

```bash
echo "=== Step 1: Baseline sync-status ==="
curl -s "$API/v1/universities/$UID/sync-status" | jp

echo "=== Step 2: Reset classification ==="
# WARNING: destructive — only run on test university
curl -s -X POST "$API/v1/universities/$UID/reset" \
  -H "Content-Type: application/json" \
  -d '{"scope": "classification"}' | jp

echo "=== Step 3: sync-status shows data_reset ==="
curl -s "$API/v1/universities/$UID/sync-status" | jp

echo "=== Step 4: KB sync clears data_reset ==="
curl -s -X POST "$API/v1/universities/$UID/kb/sync" \
  -H "Content-Type: application/json" -d '{}' | jp

echo "=== Step 5: Verify cleared ==="
curl -s "$API/v1/universities/$UID/sync-status" | jp
```

**Expected progression:**
| Step | data_reset | sync_needed |
|------|-----------|-------------|
| 1 | false | depends |
| 3 | true | true |
| 5 | false | false |

### Scenario D: Stats and sync-status consistency

Verify that the stats endpoint and sync-status agree on pending_kb_sync.

```bash
echo "=== Stats ==="
curl -s "$API/v1/universities/$UID/stats" | python3 -c "
import sys, json
d = json.loads(sys.stdin.read())
print(f\"pending_kb_sync: {d['pending_kb_sync']}\")
print(f\"pages_changed: {d['pages_changed']}\")
print(f\"categories_modified: {d['categories_modified']}\")
print(f\"data_reset: {d['data_reset']}\")
"

echo "=== Sync Status ==="
curl -s "$API/v1/universities/$UID/sync-status" | python3 -c "
import sys, json
d = json.loads(sys.stdin.read())
print(f\"sync_needed: {d['sync_needed']}\")
print(f\"pages_changed: {d['reasons']['pages_changed']}\")
print(f\"categories_modified: {d['reasons']['categories_modified']}\")
print(f\"data_reset: {d['reasons']['data_reset']}\")
"
```

**Expect:** Both endpoints report identical values for `pages_changed`, `categories_modified`, `data_reset`, and the `pending_kb_sync`/`sync_needed` boolean matches.

---

## Quick Reference: HTTP Status Codes

| Code | Where | Meaning |
|------|-------|---------|
| 200 | All GETs, most POSTs | Success |
| 202 | POST /pipeline, POST /kb/sync | Async operation started |
| 400 | Validation errors | Missing/invalid fields |
| 404 | GET /pipeline/{job_id} | Job not found |
| 409 | POST /pipeline, POST /kb/sync | Operation blocked — pipeline already running |
| 500 | Any | Server error |

## Quick Reference: Sync Status Fields

| Field | Source | Cleared by |
|-------|--------|-----------|
| `pages_changed` | Content-cleaner Lambda (during crawl) | KB sync (`POST /kb/sync`) |
| `categories_modified` | Category endpoints (rename/delete/add/remove pages) | KB sync |
| `data_reset` | Reset endpoint (`POST /reset`) | KB sync |
| `status` | Crawl orchestrator / KB sync | Crawl sets "pending_sync", sync sets "syncing" |
| `pipeline_running` | Live check against pipeline-jobs table | Automatic (pipeline completes) |

## Quick Reference: Operation Guard Matrix

| Operation | Guarded by | Guard response |
|-----------|-----------|---------------|
| Start pipeline | `find_running_pipeline()` | 409 with `running_job_id` |
| Start KB sync | `find_running_pipeline()` | 409 with `running_job_id` |
| Category ops | No guard | Always allowed |
| Reset | Stops running pipelines first | No 409 (self-resolving) |