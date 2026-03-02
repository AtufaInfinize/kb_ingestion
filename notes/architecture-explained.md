# University KB Crawler — Plain English Explanation

A complete walkthrough of the system for anyone who needs to understand how it works.

---

## What this system does

You give it a university website (like gmu.edu). It discovers every page on that site, crawls them all, cleans the HTML into readable text, classifies each page into a category (like "admissions" or "financial aid"), and loads everything into a Bedrock Knowledge Base so an AI agent can answer student questions.

---

## The pipeline, step by step

### 1. Seed Discovery

It starts by figuring out *what URLs to crawl*. It looks at the university config (stored in S3 as a JSON file), takes the seed URLs you gave it, then goes and finds more:
- Checks Certificate Transparency logs (crt.sh) to find subdomains (like catalog.gmu.edu, library.gmu.edu)
- Fetches robots.txt for each domain to know what's off-limits
- Parses sitemap.xml files to find all advertised pages

Every discovered URL gets registered in DynamoDB (`url-registry` table) with `crawl_status: pending` and pushed onto the crawl queue.

### 2. Crawling

The crawl queue is a FIFO queue — meaning it processes URLs in order and deduplicates them (so if the same URL gets pushed twice, it only gets crawled once).

A crawler worker Lambda picks up one URL at a time. Before fetching, it runs through filters:
- Is this domain allowed?
- Is this a junk URL? (search pages, login pages, calendar pages — stuff that's useless for a knowledge base)
- Was this already crawled recently?
- Does robots.txt forbid it?
- Is this a binary file like .zip or .jpg?

If it passes all filters, it fetches the page via HTTP. The raw HTML gets stored in S3 at `raw-html/{university}/{domain}/{hash}.html`. DynamoDB gets updated with the crawl result. Then it pushes the URL to the processing queue for cleaning.

It also discovers new links on the page and pushes *those* back to the crawl queue — so one seed URL can lead to thousands of pages being discovered.

**Rate limiting**: There's a token bucket per domain (default 3 requests/second) stored in DynamoDB so we don't hammer any single server.

### 3. Content Cleaning

The content cleaner Lambda picks up messages from the processing queue. It reads the raw HTML from S3 and uses Trafilatura (a Python library) to extract just the meaningful text — stripping out navigation, footers, ads, scripts. It outputs a clean markdown file to `clean-content/{university}/{domain}/{hash}.md`.

It also updates DynamoDB with `processing_status`:
- `cleaned` — success
- `empty_content` — the page had no extractable text (like a page that's all images)
- `s3_not_found` — the raw HTML wasn't in S3 for some reason
- `cleaning_error` — something crashed

PDFs go to a separate queue and a separate Lambda (`pdf-processor`). The PDF processor doesn't actually extract text — it just creates a metadata sidecar file. Bedrock KB handles PDF parsing itself during ingestion.

### 4. Batch Classification

This is the expensive AI step. Instead of classifying one page at a time (slow, costly), we batch them:

**Prepare** (auto-triggered by orchestrator after crawl+clean complete): Scans all the `.md` files in S3, finds the ones that don't have a classification sidecar yet, reads the first 2000 bytes of each, builds a JSONL file with a classification prompt for each page, and submits it to Bedrock as a batch inference job. Max 50,000 per job. If there are 132,000 pages, it creates 3 jobs.

**Bedrock processes the batch**: This can take anywhere from 30 minutes to 24 hours depending on volume. Claude 3 Haiku reads each page snippet and returns: category (like "admissions"), subcategory, summary, whether it's a useful/high-traffic page, and up to 5 key facts.

**Process** (auto-triggered by EventBridge when the batch job completes): Reads the output, writes a `.metadata.json` sidecar next to each `.md` file (this is what Bedrock KB uses for document attributes), updates DynamoDB with the category, and stores extracted facts in the entity store table.

### 5. KB Sync

The admin triggers this via the dashboard API. It calls Bedrock's `start_ingestion_job()` which tells Bedrock KB to re-scan the S3 bucket, read all the `.md` files and their `.metadata.json` sidecars, parse the PDFs, and index everything for RAG retrieval. After this, an AI agent can query the knowledge base and get answers grounded in the university's actual web content.

---

## The queues and why they exist

Queues are the glue between pipeline stages. Each stage is a separate Lambda function, and they don't call each other directly. Instead, stage A puts a message on a queue, and stage B picks it up. This decouples them — if stage B is slow or crashes, messages just pile up in the queue and get processed when it recovers.

There are 4 main queues:

- **Crawl queue** (FIFO): seed discovery pushes URLs here, crawler worker picks them up
- **Processing queue**: crawler pushes crawled URLs here, content cleaner picks them up
- **PDF processing queue**: crawler pushes PDF URLs here, pdf processor picks them up
- **Classification queue**: content cleaner pushes cleaned pages here — but this one is actually unused now since we switched to batch classification instead of real-time

### What happens when processing fails

When a Lambda picks up a message from SQS and fails to process it (crashes, times out, returns an error), SQS doesn't throw the message away. It hides the message for a "visibility timeout" period, then makes it visible again so another Lambda invocation can retry it.

But what if the same message keeps failing? Maybe the URL points to a page that always returns a 500 error, or the HTML is so malformed it crashes the parser every time. Without a safety net, SQS would retry that message forever, wasting compute and blocking other messages.

### Dead Letter Queues (DLQs)

That's where DLQs come in. A DLQ is a separate queue that catches messages that have failed too many times. The rule is: if a message has been received and failed **3 times** (`maxReceiveCount: 3`), SQS automatically moves it to the DLQ instead of making it visible in the main queue again.

We have 1 active DLQ:

- **crawl-dlq.fifo** — catches URLs that failed crawling 3 times. Common reasons: the server is permanently down, DNS doesn't resolve, the page returns a server error every time, or the Lambda timed out 3 times trying to fetch a very slow page.

(There's also a `classification-dlq` in the template, but it's unused since the real-time page classifier is disabled. The batch classifier doesn't use SQS at all.)

The crawl DLQ retains messages for **14 days**. During that time you can:
1. Inspect the messages to understand what went wrong
2. Fix the underlying issue (deploy a bug fix, wait for the server to come back)
3. "Re-drive" the messages back to the main queue for another attempt
4. Or just let them expire if they're not worth retrying

**Why don't processing-queue and pdf-processing-queue have DLQs?**

The content cleaner uses a feature called `ReportBatchItemFailures`. It processes 5 messages at a time (batch), and if one fails, it tells SQS exactly which one failed. Only that one gets retried — the other 4 succeed normally. Failed messages keep retrying until the 7-day retention expires. Since content cleaning failures are usually transient (S3 hiccup, memory spike), this approach works fine without a dedicated DLQ.

---

## How the crawl is orchestrated

The system uses a Step Functions state machine to coordinate the crawl lifecycle. When triggered (manually, daily at 2 AM, or weekly on Sunday), it:

1. **Validates** the request (checks the university config exists, determines the mode)
2. **Discovers or re-queues**: If "full" mode, it runs seed discovery to find all URLs from scratch. If "incremental" mode, it just finds URLs that are past their freshness window and re-queues them.
3. **Polls until done**: It enters a loop — wait 120 seconds, check if **both** the crawl queue AND the processing queue are empty and no URLs are still pending, if not wait again. This loop covers both crawling AND content cleaning.
4. **Triggers classification**: Once crawl+clean are complete, invokes the batch-classifier-prepare Lambda to start page classification. Updates the pipeline-jobs record to mark the classify stage as running.
5. **Generates summary**: Writes crawl statistics to S3.

### Freshness windows

Not all pages need to be re-crawled at the same frequency. The config defines freshness windows per category:
- Events pages: every 3 days (they change frequently)
- Admissions pages: every 7 days
- Policy pages: every 60 days (they rarely change)

The incremental crawl only re-queues pages that are past their freshness window, saving compute and respecting the university's servers.

---

## EventBridge rules (scheduled triggers)

Three things happen automatically:

1. **Daily at 2 AM UTC**: An incremental crawl runs — only re-crawls stale pages
2. **Sunday at 3 AM UTC**: A full crawl runs — re-discovers seeds from sitemaps to catch new pages
3. **When a Bedrock batch job completes**: The batch-classifier-process Lambda auto-triggers to write the classification results

---

## The Dashboard API

A single Lambda (`dashboard-api`) serves 13 REST endpoints for the admin frontend. It's one Lambda with a regex router inside — API Gateway forwards the HTTP request, the Lambda matches the path pattern, and dispatches to the right handler function.

What it can do:
- **List universities** and their crawl/processing stats
- **Show category cards** — how many pages in "admissions", "financial aid", etc.
- **Browse pages** per category with pagination
- **Add URLs** to a category (registers in DynamoDB + queues for crawl)
- **Remove URLs** from a category (marks as "excluded")
- **Upload PDFs** via presigned S3 URLs (bypasses API Gateway's 10MB limit)
- **Start the full pipeline** (triggers Step Functions)
- **Track pipeline progress** in real-time (checks queue depths + DynamoDB counts)
- **Trigger KB sync** (starts Bedrock ingestion)
- **Check KB sync status** (lists recent ingestion jobs)
- **Check classification status** (lists Bedrock batch job progress)

---

## DynamoDB tables

### url-registry
The main table. Every URL the system has ever seen gets a row here. Tracks everything: crawl status, processing status, category, content hash, S3 location, timestamps, retry counts. Has 4 GSIs (Global Secondary Indexes) for efficient queries by university, domain, status, and category.

### entity-store
Structured facts extracted during classification. Things like "BS Computer Science requires 120 credits" or "Application deadline is January 15". Keyed by university + category + url_hash so facts from different pages don't collide.

### pipeline-jobs
Tracks full pipeline runs (crawl+clean+classify) as a single "job" with stage-level progress. Used by the dashboard API to show pipeline status.

### robots-cache
Caches robots.txt rules per domain so we don't fetch them on every single crawl request. Has a TTL so entries expire and get re-fetched periodically.

### crawl-rate-limits
Token bucket rate limiting per domain. Ensures we don't send more than N requests per second to any single domain. The crawler checks this before every fetch.

---

## S3 bucket layout

Everything lives in one bucket: `university-kb-content-{account_id}-dev`

```
configs/gmu.json                          — University configuration
raw-html/gmu/catalog.gmu.edu/abc123.html  — Raw crawled HTML
raw-pdf/gmu/www.gmu.edu/def456.pdf        — Raw crawled PDFs
raw-pdf/gmu/www.gmu.edu/def456.pdf.metadata.json  — PDF metadata sidecar
raw-pdf/gmu/manual/brochure.pdf           — Manually uploaded PDFs
clean-content/gmu/catalog.gmu.edu/abc123.md           — Cleaned markdown
clean-content/gmu/catalog.gmu.edu/abc123.md.metadata.json  — Classification sidecar
batch-jobs/gmu/classify-gmu-20260220-111209-b1/input.jsonl  — Batch input
batch-jobs/gmu/classify-gmu-20260220-111209-b1/manifest.json — Batch metadata
batch-jobs/gmu/classify-gmu-20260220-111209-b1/output/*.jsonl.out — Batch output
crawl-summaries/gmu/2026-02-20T12:00:00.json — Crawl completion summaries
```

The `url_hash` in file names is the first 16 characters of SHA256 of the URL. This keeps file names short and avoids issues with special characters in URLs.

The `.metadata.json` sidecars are what Bedrock KB reads during ingestion to get document attributes (category, source URL, summary, etc.) for RAG filtering.

---

## Configuration

Each university has a JSON config file in S3:

- **seed_urls**: Starting points for the crawl
- **crawl_config**: Max depth, max pages, rate limits, domain allowlists, exclude patterns
- **freshness_windows_days**: How often to re-crawl each category
- **kb_config**: Bedrock Knowledge Base ID and data source IDs for syncing

Currently configured:
- **GMU** (George Mason University): ~200K max pages, depth 5, KB ID `Q2WE6XQJHS`
- **PHC** (Patrick Henry College): ~5K max pages, depth 3, KB ID `SFY1YK3YAQ`

---

## How everything connects

```
Admin/Schedule
    |
    v
Step Functions orchestrator
    |
    v
Seed Discovery --> crawl-queue (FIFO)
                        |
                        v
                   Crawler Worker ---> raw HTML/PDF to S3
                        |                    |
                        |              (new links back to crawl-queue)
                        v
                   processing-queue
                     /        \
                    v          v
           Content Cleaner   PDF Processor
           (HTML -> .md)     (metadata sidecar)
                |
                v
        clean .md files in S3
                |
                v (auto-triggered by orchestrator)
        Batch Classifier Prepare
                |
                v
        Bedrock Batch Inference (Claude 3 Haiku, up to 24h)
                |
                v (EventBridge auto-trigger)
        Batch Classifier Process
        (.metadata.json sidecars + DynamoDB updates)
                |
                v (admin triggers via Dashboard API)
        Bedrock KB Sync (indexes everything for RAG)
                |
                v
        AI Agent can now answer questions using this university's content
```
