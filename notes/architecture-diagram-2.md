# Architecture Diagram v2 — University KB Crawler (Single Canvas)

Copy the entire block below and paste into [eraser.io](https://app.eraser.io/) to render all flows on one canvas.

```
title University KB Crawler — Full Architecture (Detailed)

// ==========================================================================
// GROUP A: Scheduling & Triggers
// ==========================================================================
Scheduling [icon: zap, color: yellow] {
  Daily Rule [icon: aws-eventbridge, label: "EventBridge\ncron(0 2 * * ? *)\nDaily 2AM UTC"]
  Weekly Rule [icon: aws-eventbridge, label: "EventBridge\ncron(0 3 ? * SUN *)\nSunday 3AM UTC"]
  Manual Trigger [icon: aws-api-gateway, label: "POST /pipeline\nor POST /crawl/refresh"]

  Crawl Scheduler [icon: aws-lambda, label: "crawl-scheduler", color: yellow] {
    List Unis [icon: list, label: "List universities\nfrom S3 configs/"]
    Check Running [icon: alert-circle, label: "Pipeline\nalready running?\n(pipeline-jobs GSI)"]
    Check Enabled [icon: toggle-right, label: "Schedule enabled?\n(freshness_config\n.incremental_enabled\n.full_enabled)"]
    Check Stale [icon: clock, label: "Content stale?\n(min freshness window\nvs last crawl date)"]
  }
}

// ==========================================================================
// GROUP B: State Machine Orchestration
// ==========================================================================
State Machine [icon: aws-step-functions, color: purple] {
  Validate [icon: check-circle, label: "ValidateRequest\n• Check university_id\n• Set refresh_mode\n• Write current_crawl_mode"]
  Mode Choice [icon: git-branch, label: "CheckRefreshMode"]

  Full Path [icon: search, color: purple] {
    Discover Seeds [icon: aws-lambda, label: "seed-discovery\n• Parse sitemaps\n• Homepage links\n• Config seed URLs\n• Register new URLs"]
  }

  Incremental Path [icon: clock, color: purple] {
    Queue Stale [icon: aws-lambda, label: "orchestrator\ntask: queue_stale_urls\n• Query url-registry GSI\n• Check last_crawled_at\n  vs freshness_windows\n• Re-queue stale only"]
  }

  Progress Loop [icon: loader, color: purple] {
    Wait [icon: pause, label: "Wait 120s"]
    Check Progress [icon: activity, label: "CheckCrawlProgress\n• SQS queue depths\n• Pending URL count"]
    Is Complete [icon: help-circle, label: "IsCrawlComplete?"]
  }

  Classify Choice [icon: git-branch, label: "ShouldClassify?\n• incremental → skip\n• full → classify"]
  Generate Summary [icon: file-text, label: "GenerateSummary\n• kb_sync_status:\n  pending_sync | no_changes\n• pages_changed count"]
}

// ==========================================================================
// GROUP C: Crawl & 3-Layer Deduplication
// ==========================================================================
Crawl Stage [icon: globe, color: blue] {
  Crawl Queue [icon: aws-sqs, label: "crawl-queue.fifo\nFIFO | Vis: 6min | Ret: 7d\nContentBasedDedup: true"]

  Crawler Worker [icon: aws-lambda, label: "crawler-worker", color: blue] {
    Dedup Layer 1 [icon: database, label: "Layer 1: Freshness\nDynamoDB read\ncrawl_status + last_crawled_at\nSkip if < 24h old"]
    Dedup Layer 2 [icon: globe, label: "Layer 2: HTTP 304\nIf-None-Match: etag\nIf-Modified-Since: date\nSkip if 304 Not Modified"]
    Dedup Layer 3 [icon: hash, label: "Layer 3: Content Hash\nSHA-256(content)\nCompare vs stored hash\nSkip if identical"]
    Store Content [icon: upload, label: "Content Changed\n→ Store raw in S3\n→ Route to queue"]
  }

  Link Discovery [icon: link, label: "Discover links\n→ register_and_queue_url\n(full crawl only)"]
}

// ==========================================================================
// GROUP D: Content Processing
// ==========================================================================
Processing Stage [icon: cpu, color: green] {
  HTML Processing [icon: file-text, color: green] {
    Processing Queue [icon: aws-sqs, label: "processing-queue\nStandard | Vis: 15min | Ret: 7d"]
    Content Cleaner [icon: aws-lambda, label: "content-cleaner\n• Trafilatura extract\n• XML → Markdown\n• Heading hierarchy\n• Link extraction\n• Max 500 links/page"]
  }

  Media Processing [icon: image, color: green] {
    Media Queue [icon: aws-sqs, label: "pdf-processing-queue\n(all media types)\nStandard | Vis: 15min | Ret: 7d"]
    Media Processor [icon: aws-lambda, label: "pdf-processor\n(PDF/Image/Audio/Video)\n• Metadata sidecar\n• Category inference\n• Title inference"]
  }
}

// ==========================================================================
// GROUP E: Classification (Full Crawl Only)
// ==========================================================================
Classification [icon: tag, color: orange] {
  Batch Prepare [icon: aws-lambda, label: "batch-classifier-prepare\n• List unclassified .md\n• Read first 2000 bytes\n• Load URL metadata\n• Write input.jsonl\n• Submit batch job"]
  Bedrock Batch [icon: aws-bedrock, label: "Claude 3 Haiku\nBatch Inference\n(up to 24 hours)\n19 categories"]
  Batch Event [icon: aws-eventbridge, label: "EventBridge\nJob Completion"]
  Batch Process [icon: aws-lambda, label: "batch-classifier-process\n• Read output.jsonl.out\n• Write .md.metadata.json\n• Set page_category\n• Extract entities/facts"]
}

// ==========================================================================
// GROUP F: Dashboard & Manual Upload
// ==========================================================================
Dashboard [icon: layout, color: teal] {
  API Gateway [icon: aws-api-gateway, label: "API Gateway\n/v1/universities/{uid}/..."] {
    Stats Routes [icon: bar-chart, label: "GET /universities\nGET /stats"]
    Category Routes [icon: folder, label: "GET /categories\nGET|POST|DELETE .../pages\nPOST /media/upload-url\nPOST /media/process"]
    Pipeline Routes [icon: play, label: "POST /pipeline\nGET /pipeline\nGET /pipeline/{job_id}"]
    KB Routes [icon: book, label: "POST /kb/sync\nGET /kb/sync"]
    Other Routes [icon: more-horizontal, label: "GET /classification\nGET /dlq\nGET|POST /freshness"]
  }

  Dashboard Lambda [icon: aws-lambda, label: "dashboard-api\nroutes: stats, categories,\npipeline, kb, classification"]

  Manual Upload [icon: upload-cloud, label: "Manual Upload Flow\n1. GET presigned URL\n2. PUT file → S3\n3. POST /media/process\n   → SQS → Media Processor"]
}

// ==========================================================================
// GROUP G: SQS Queues & Dead Letter Queues
// ==========================================================================
Queues [icon: aws-sqs, color: red] {
  Main Queues [icon: inbox, color: blue] {
    CQ [icon: aws-sqs, label: "crawl-queue.fifo\nFIFO | Vis: 6min\nRet: 7d | Dedup: content"]
    PQ [icon: aws-sqs, label: "processing-queue\nStandard | Vis: 15min\nRet: 7d"]
    MQ [icon: aws-sqs, label: "pdf-processing-queue\nStandard | Vis: 15min\nRet: 7d"]
  }

  Dead Letter Queues [icon: alert-triangle, color: red] {
    Crawl DLQ [icon: aws-sqs, label: "crawl-dlq.fifo\nFIFO | Ret: 14d\nAfter 3 failures"]
    Processing DLQ [icon: aws-sqs, label: "processing-dlq\nStandard | Ret: 14d\nAfter 3 failures"]
    Media DLQ [icon: aws-sqs, label: "pdf-processing-dlq\nStandard | Ret: 14d\nAfter 3 failures"]
  }
}

// ==========================================================================
// GROUP H: Storage Layer
// ==========================================================================
Storage [icon: database, color: gray] {
  S3 [icon: aws-s3, label: "university-kb-content", color: gray] {
    Raw HTML [icon: file, label: "raw-html/\n{uid}/{domain}/{hash}.html"]
    Clean Content [icon: file-text, label: "clean-content/\n{uid}/{domain}/{hash}.md\n+ .md.metadata.json"]
    Raw Media [icon: file-plus, label: "raw-pdf/\n{uid}/{domain}/{hash}.pdf\n{uid}/manual/{file}\n+ .metadata.json"]
    Configs [icon: settings, label: "configs/\n{uid}.json"]
    Summaries [icon: bar-chart, label: "crawl-summaries/\n{uid}/{job_id}.json"]
    Batch IO [icon: layers, label: "batch-inference/\ninput.jsonl\nmanifest.json\noutput.jsonl.out"]
  }

  DynamoDB [icon: aws-dynamodb, color: gray] {
    URL Registry [icon: aws-dynamodb, label: "url-registry\nPK: url\nGSI: university-status-index\n(university_id, crawl_status)\nFields: content_hash, etag,\nlast_crawled_at, page_category,\nprocessing_status, s3_key"]
    Entity Store [icon: aws-dynamodb, label: "entity-store\nPK: university_id\nSK: entity_key\nKeys: freshness_config,\nkb_sync_status,\ncurrent_crawl_mode,\nextracted facts"]
    Pipeline Jobs [icon: aws-dynamodb, label: "pipeline-jobs\nPK: job_id\nGSI: university-created-index\nFields: refresh_mode,\noverall_status,\ncrawl/clean/classify_stage"]
    Robots Cache [icon: aws-dynamodb, label: "robots-cache\nPK: domain\nTTL-based expiry"]
    Rate Limits [icon: aws-dynamodb, label: "crawl-rate-limits\nPK: domain\nToken bucket"]
  }

  Knowledge Base [icon: aws-bedrock, label: "Bedrock Knowledge Base\n• Reads clean-content/ + raw-pdf/\n• Dedup by S3 ETag\n• Re-embeds changed objects\n• Vector store for RAG"]
}

// ==========================================================================
// CONNECTIONS — Group A: Scheduling
// ==========================================================================
Daily Rule > List Unis: refresh_mode: incremental
Weekly Rule > List Unis: refresh_mode: full
List Unis > Check Running
Check Running > Check Enabled: no running job
Check Enabled > Check Stale: enabled
Check Stale > Validate: stale → start_execution

// Manual trigger bypasses scheduler
Manual Trigger > Validate: direct

// Scheduler reads
List Unis > Configs
Check Running > Pipeline Jobs
Check Enabled > Entity Store
Check Stale > Entity Store: freshness_config

// ==========================================================================
// CONNECTIONS — Group B: State Machine
// ==========================================================================
Validate > Mode Choice
Mode Choice > Discover Seeds: full
Mode Choice > Queue Stale: incremental

Discover Seeds > Wait
Queue Stale > Wait
Wait > Check Progress
Check Progress > Is Complete
Is Complete > Wait: not complete (loop)
Is Complete > Classify Choice: complete

Classify Choice > Batch Prepare: full → classify
Classify Choice > Generate Summary: incremental → skip

// QueueStaleUrls reads freshness config
Queue Stale > Entity Store: read freshness_config
Queue Stale > URL Registry: query GSI for stale URLs

// ==========================================================================
// CONNECTIONS — Group C: Crawl
// ==========================================================================
Discover Seeds > Crawl Queue: push discovered URLs
Queue Stale > Crawl Queue: push stale URLs
Dashboard Lambda > Crawl Queue: manual URL add

Crawl Queue > Dedup Layer 1
Dedup Layer 1 > Dedup Layer 2: not recent
Dedup Layer 2 > Dedup Layer 3: HTTP 200
Dedup Layer 3 > Store Content: hash differs

// Dedup reads/writes
Dedup Layer 1 <> URL Registry: crawl_status + last_crawled_at
Dedup Layer 2 <> URL Registry: etag + last_modified
Dedup Layer 3 <> URL Registry: content_hash

// Crawler outputs
Store Content > Raw HTML: raw-html/{uid}/{domain}/{hash}.html
Store Content > Raw Media: raw-pdf/{uid}/{domain}/{hash}.pdf
Store Content > Processing Queue: HTML content
Store Content > Media Queue: PDF/media content
Store Content > URL Registry: crawl_status=crawled

Link Discovery > Crawl Queue: new links (full crawl)
Crawler Worker > Robots Cache: check rules
Crawler Worker > Rate Limits: check rate

// ==========================================================================
// CONNECTIONS — Group D: Processing
// ==========================================================================
Processing Queue > Content Cleaner
Content Cleaner > Clean Content: {uid}/{domain}/{hash}.md
Content Cleaner > URL Registry: processing_status=cleaned
Content Cleaner > Entity Store: pages_changed++ (incremental)

Media Queue > Media Processor
Media Processor > Raw Media: .metadata.json sidecar
Media Processor > URL Registry: processing_status=processed_media

// ==========================================================================
// CONNECTIONS — Group E: Classification
// ==========================================================================
Batch Prepare > Clean Content: list unclassified .md
Batch Prepare > Batch IO: write input.jsonl
Batch Prepare > Bedrock Batch: create_model_invocation_job
Bedrock Batch > Batch Event: completion
Batch Event > Batch Process
Batch Process > Batch IO: read output.jsonl.out
Batch Process > Clean Content: write .md.metadata.json
Batch Process > URL Registry: page_category
Batch Process > Entity Store: extracted facts

// Classification feeds back to summary
Batch Process > Generate Summary

// ==========================================================================
// CONNECTIONS — Group F: Dashboard
// ==========================================================================
API Gateway > Dashboard Lambda
Dashboard Lambda <> URL Registry
Dashboard Lambda <> Pipeline Jobs
Dashboard Lambda <> S3
Dashboard Lambda <> Entity Store
Dashboard Lambda > State Machine: start_execution

// Manual upload
Manual Upload > Raw Media: presigned PUT
Manual Upload > Media Queue: POST /media/process → SQS

// KB sync
Dashboard Lambda > Knowledge Base: start_ingestion_job
Knowledge Base > Clean Content: read .md + .metadata.json
Knowledge Base > Raw Media: read media + .metadata.json

// ==========================================================================
// CONNECTIONS — Group G: DLQs
// ==========================================================================
CQ --> Crawl DLQ: 3 failures
PQ --> Processing DLQ: 3 failures
MQ --> Media DLQ: 3 failures
```
