# Architecture Diagrams — University KB Crawler

## 1. Complete System Architecture

```
title University Knowledge Base Crawl Pipeline

// ▸ Detail: v2 Group A — Scheduling & Triggers
// Triggers
Triggers [icon: zap] {
  Daily Schedule [icon: aws-eventbridge, label: "EventBridge Daily 2AM UTC\n(incremental)"]
  Weekly Schedule [icon: aws-eventbridge, label: "EventBridge Sunday 3AM UTC\n(full)"]
  Refresh API [icon: aws-api-gateway, label: "POST /crawl/refresh"]
  Dashboard API [icon: aws-api-gateway, label: "Dashboard API"]
}

// Crawl Scheduler (pre-filter per university)
Crawl Scheduler [icon: aws-lambda, label: "crawl-scheduler\nPer-university checks:\n• Pipeline running?\n• Schedule enabled?\n• Content stale?", color: yellow]

// ▸ Detail: v2 Group B — State Machine Orchestration
// Step Functions Orchestrator
Orchestrator [icon: aws-step-functions, color: purple] {
  Validate Request [icon: check-circle]
  Check Mode [icon: git-branch, label: "CheckRefreshMode"]
  Discover Seeds [icon: search, label: "DiscoverSeeds\n(full crawl)"]
  Queue Stale URLs [icon: clock, label: "QueueStaleUrls\n(incremental)"]
  Wait for Crawl [icon: loader]
  Check Progress [icon: activity]
  Should Classify [icon: git-branch, label: "ShouldClassify\n(skip for incremental)"]
  Trigger Classification [icon: tag, label: "TriggerClassification\n(full only)"]
  Generate Summary [icon: file-text]
}

// ▸ Detail: v2 Group C — Crawl & 3-Layer Dedup
// Stage 2: Crawling
Crawling [icon: globe, color: blue] {
  Crawl Queue [icon: aws-sqs, label: "crawl-queue.fifo\nVis: 6min | Ret: 7d"]
  Crawler Worker [icon: aws-lambda, label: "crawler-worker\n3-layer dedup:\n1. Freshness (24h)\n2. HTTP 304\n3. SHA-256 hash"]
}

// ▸ Detail: v2 Group D — Content Processing
// Stage 3: Content Processing
Processing [icon: cpu, color: green] {
  Processing Queue [icon: aws-sqs, label: "processing-queue\nVis: 15min | Ret: 7d"]
  Media Queue [icon: aws-sqs, label: "pdf-processing-queue\n(all media types)\nVis: 15min | Ret: 7d"]
  Content Cleaner [icon: aws-lambda, label: "content-cleaner"]
  Media Processor [icon: aws-lambda, label: "pdf-processor\n(PDF/Image/Audio/Video)"]
}

// ▸ Detail: v2 Group G — SQS & DLQs
// Dead Letter Queues
DLQ [icon: alert-triangle, color: red] {
  Crawl DLQ [icon: aws-sqs, label: "crawl-dlq.fifo\n14d retention | After 3 failures"]
  Processing DLQ [icon: aws-sqs, label: "processing-dlq\n14d retention | After 3 failures"]
  Media DLQ [icon: aws-sqs, label: "pdf-processing-dlq\n14d retention | After 3 failures"]
}

// ▸ Detail: v2 Group E — Classification (full only)
// Stage 4: Batch Classification (full crawl only)
Classification [icon: tag, color: orange] {
  Batch Prepare [icon: aws-lambda, label: "batch-classifier-prepare"]
  Bedrock Batch [icon: aws-bedrock, label: "Claude 3 Haiku Batch"]
  Batch Complete Event [icon: aws-eventbridge, label: "Job Completion"]
  Batch Process [icon: aws-lambda, label: "batch-classifier-process"]
}

// ▸ Detail: v2 Group H — Storage Layer
// Storage
Storage [icon: database, color: gray] {
  S3 Bucket [icon: aws-s3, label: "university-kb-content"]
  URL Registry [icon: aws-dynamodb, label: "url-registry"]
  Entity Store [icon: aws-dynamodb, label: "entity-store\n(freshness_config,\nkb_sync_status)"]
  Robots Cache [icon: aws-dynamodb, label: "robots-cache"]
  Rate Limits [icon: aws-dynamodb, label: "crawl-rate-limits"]
  Pipeline Jobs [icon: aws-dynamodb, label: "pipeline-jobs"]
}

// Stage 5: Knowledge Base
Knowledge Base [icon: aws-bedrock, label: "Bedrock Knowledge Base"]

// ▸ Detail: v2 Group F — Dashboard & Manual Upload
// Dashboard
Dashboard Lambda [icon: aws-lambda, label: "dashboard-api"]

// Trigger connections (through scheduler)
Daily Schedule > Crawl Scheduler: incremental
Weekly Schedule > Crawl Scheduler: full
Crawl Scheduler > Orchestrator: start_execution (per eligible university)
Refresh API > Orchestrator: manual trigger (bypasses scheduler)
Dashboard API > Dashboard Lambda

// Orchestrator to Crawling
Discover Seeds > Crawl Queue: push URLs
Queue Stale URLs > Crawl Queue: push stale URLs

// Crawl stage flow
Crawl Queue > Crawler Worker
Crawler Worker > Crawl Queue: new links (full crawl)
Crawler Worker > Processing Queue: HTML
Crawler Worker > Media Queue: PDF/media
Crawler Worker > S3 Bucket: raw content
Crawler Worker <> URL Registry: status updates + dedup checks
Crawler Worker > Rate Limits: check rate
Crawler Worker > Robots Cache: check rules

// Processing stage flow
Processing Queue > Content Cleaner
Media Queue > Media Processor
Content Cleaner > S3 Bucket: clean .md
Content Cleaner > URL Registry: processing_status
Content Cleaner > Entity Store: pages_changed++ (incremental)
Media Processor > S3 Bucket: .metadata.json
Media Processor > URL Registry: processing_status

// Manual upload flow (Dashboard → S3 → Media Processor)
Dashboard Lambda > S3 Bucket: presigned upload (PDF/image/audio/video)
Dashboard Lambda > Media Queue: POST /media/process triggers SQS

// Classification stage flow (full crawl only)
Trigger Classification > Batch Prepare: invoke classification
Batch Prepare > S3 Bucket: input.jsonl
Batch Prepare > Bedrock Batch: submit job
Bedrock Batch > Batch Complete Event: completion
Batch Complete Event > Batch Process
Batch Process > S3 Bucket: read/write
Batch Process > URL Registry: page_category
Batch Process > Entity Store: facts

// Knowledge Base sync
Dashboard Lambda > Knowledge Base: start_ingestion_job
Knowledge Base > S3 Bucket: reads content

// Dashboard API connections
Dashboard Lambda <> URL Registry
Dashboard Lambda <> Pipeline Jobs
Dashboard Lambda <> S3 Bucket
Dashboard Lambda <> Entity Store
Dashboard Lambda > Orchestrator: start_execution

// DLQ connections
Crawl Queue --> Crawl DLQ: 3 failures
Processing Queue --> Processing DLQ: 3 failures
Media Queue --> Media DLQ: 3 failures

// Scheduler reads config
Crawl Scheduler > S3 Bucket: list configs/
Crawl Scheduler > Pipeline Jobs: check running jobs
Crawl Scheduler > Entity Store: freshness_config + schedule flags
```

---

## 2. Data Flow: Single URL Lifecycle

```mermaid
sequenceDiagram
    participant SD as Seed Discovery
    participant DB as url-registry
    participant CQ as Crawl Queue FIFO
    participant CW as Crawler Worker
    participant S3 as S3 Bucket
    participant PQ as Processing Queue
    participant CC as Content Cleaner
    participant BP as Batch Prepare
    participant BR as Bedrock Batch
    participant BC as Batch Process
    participant ES as Entity Store
    participant KB as Bedrock KB

    SD->>DB: Register URL crawl_status pending
    SD->>CQ: Push URL message

    CQ->>CW: Deliver message
    CW->>CW: Check filters domain junk freshness robots
    CW->>S3: Store raw-html/uid/domain/hash.html
    CW->>DB: Update crawl_status crawled s3_key content_hash
    CW->>PQ: Push to processing queue
    CW->>CQ: Push discovered links

    PQ->>CC: Deliver message
    CC->>S3: Read raw HTML
    CC->>CC: Trafilatura extract text
    CC->>S3: Write clean-content/uid/domain/hash.md
    CC->>DB: Update processing_status cleaned links_to

    Note over BP: Auto-triggered by orchestrator after crawl+clean
    BP->>S3: List clean-content find unclassified
    BP->>S3: Read first 2000 bytes of each md
    BP->>DB: Load URL metadata via GSI
    BP->>S3: Write input.jsonl and manifest.json
    BP->>BR: create_model_invocation_job

    Note over BR: Up to 24 hours
    BR-->>BC: EventBridge job completed

    BC->>S3: Read manifest and output jsonl.out
    BC->>S3: Write md.metadata.json sidecar
    BC->>DB: Update page_category processing_status classified
    BC->>ES: Store facts batch writer

    Note over KB: Admin triggers sync
    KB->>S3: Read clean-content and metadata sidecars
    KB->>S3: Read raw-pdf and metadata sidecars
    KB->>KB: Index for RAG retrieval
```

---

## 3. Step Functions State Machine

```mermaid
stateDiagram-v2
    [*] --> ValidateRequest
    ValidateRequest --> IsRequestValid
    ValidateRequest --> CrawlFailed: error

    IsRequestValid --> CheckRefreshMode: valid
    IsRequestValid --> CrawlFailed: invalid

    CheckRefreshMode --> DiscoverSeeds: full
    CheckRefreshMode --> QueueStaleUrls: incremental
    CheckRefreshMode --> DiscoverSeeds: default

    DiscoverSeeds --> WaitForCrawl
    DiscoverSeeds --> CrawlFailed: error
    QueueStaleUrls --> WaitForCrawl
    QueueStaleUrls --> CrawlFailed: error

    WaitForCrawl --> CheckCrawlProgress: 120s
    note right of CheckCrawlProgress: Checks crawl queue +\nprocessing queue +\npending URL count
    CheckCrawlProgress --> IsCrawlComplete
    CheckCrawlProgress --> CrawlFailed: error

    IsCrawlComplete --> WaitForCrawl: not complete
    IsCrawlComplete --> ShouldClassify: complete

    state ShouldClassify <<choice>>
    ShouldClassify --> GenerateSummary: incremental (skip classification)
    ShouldClassify --> TriggerClassification: full

    TriggerClassification --> GenerateSummary
    TriggerClassification --> GenerateSummary: error (non-fatal)
    GenerateSummary --> PipelineComplete
    note right of GenerateSummary: Writes kb_sync_status:\npending_sync (if pages changed)\nno_changes (if unchanged)
    PipelineComplete --> [*]
    CrawlFailed --> [*]
```

---

## 4. DynamoDB Table Relationships

```mermaid
erDiagram
    URL_REGISTRY {
        string url PK
        string url_hash
        string university_id
        string domain
        string crawl_status
        string page_category
        string subcategory
        string processing_status
        string content_type
        string content_hash
        int content_length
        string s3_key
        string last_crawled_at
        string classified_at
        string discovered_at
        int depth
        int retry_count
        string etag
        int ttl
    }

    ENTITY_STORE {
        string university_id PK
        string entity_key SK
        string entity_type
        string category
        string key
        string value
        string source_url
    }

    PIPELINE_JOBS {
        string job_id PK
        string university_id
        string created_at
        string refresh_mode
        string overall_status
        string execution_arn
        string crawl_stage
        string clean_stage
        string classify_stage
        int ttl
    }

    ROBOTS_CACHE {
        string domain PK
        string rules
        int ttl
    }

    RATE_LIMITS {
        string domain PK
        int tokens
        string last_refill
    }

    URL_REGISTRY ||--o{ ENTITY_STORE : "facts extracted from"
    URL_REGISTRY }o--|| PIPELINE_JOBS : "tracked by"
    URL_REGISTRY }o--|| ROBOTS_CACHE : "respects"
    URL_REGISTRY }o--|| RATE_LIMITS : "throttled by"
```

---

## 5. Dashboard API Routes

```mermaid
flowchart LR
    subgraph API["API Gateway"]
        direction TB
        R1["GET /universities"]
        R2["GET /stats"]
        R3["GET /categories"]
        R4["GET /categories/cat/pages"]
        R5["POST /categories/cat/pages"]
        R6["DELETE /categories/cat/pages"]
        R7["POST /media/upload-url\n(all media types)"]
        R7b["POST /media/process"]
        R8["POST /pipeline"]
        R9["GET /pipeline"]
        R10["GET /pipeline/job_id"]
        R11["POST /kb/sync"]
        R12["GET /kb/sync"]
        R13["GET /classification"]
    end

    subgraph Lambda["dashboard-api Lambda"]
        direction TB
        STATS["routes/stats.py"]
        CATS["routes/categories.py"]
        PIPE["routes/pipeline.py"]
        KB["routes/kb.py"]
        CLASS["routes/classification.py"]
    end

    subgraph Backend["Backend Services"]
        S3[("S3")]
        DDB[("DynamoDB")]
        SFN["Step Functions"]
        SQS[["SQS Queues"]]
        BK_AGENT["Bedrock Agent"]
        BK_BATCH["Bedrock Batch"]
    end

    R1 --> STATS
    R2 --> STATS
    R3 --> CATS
    R4 --> CATS
    R5 --> CATS
    R6 --> CATS
    R7 --> CATS
    R7b --> CATS
    R8 --> PIPE
    R9 --> PIPE
    R10 --> PIPE
    R11 --> KB
    R12 --> KB
    R13 --> CLASS

    STATS --> S3
    STATS --> DDB
    CATS --> DDB
    CATS --> S3
    CATS --> SQS
    PIPE --> DDB
    PIPE --> SFN
    PIPE --> SQS
    KB --> S3
    KB --> BK_AGENT
    CLASS --> BK_BATCH
```

---

## 6. SQS Message Flow

```mermaid
flowchart LR
    subgraph producers["Producers"]
        SD["Seed Discovery"]
        CW["Crawler Worker"]
        DASH["Dashboard API"]
        ORCH["Orchestrator\n(queue_stale_urls)"]
    end

    subgraph queues["SQS Queues"]
        CQ[["crawl-queue.fifo\nVis: 6min Ret: 7d"]]
        PQ[["processing-queue\nVis: 15min Ret: 7d"]]
        PDQ[["pdf-processing-queue\n(all media types)\nVis: 15min Ret: 7d"]]
    end

    subgraph dlqs["Dead Letter Queues"]
        CRAWL_DLQ[["crawl-dlq.fifo\nRet: 14d\nAfter 3 failures"]]
        PROC_DLQ[["processing-dlq\nRet: 14d\nAfter 3 failures"]]
        MEDIA_DLQ[["pdf-processing-dlq\nRet: 14d\nAfter 3 failures"]]
    end

    subgraph consumers["Consumers"]
        CW2["Crawler Worker\nBatch: 1 Max: 10"]
        CCL["Content Cleaner\nBatch: 5 Max: 10"]
        PDF["Media Processor\n(PDF/Image/Audio/Video)\nBatch: 5 Max: 5"]
    end

    SD -->|discovered URLs| CQ
    CW -->|new links| CQ
    DASH -->|manual URLs| CQ
    ORCH -->|stale URLs| CQ

    CW -->|HTML pages| PQ
    CW -->|PDF/media| PDQ
    DASH -->|manual media upload| PDQ

    CQ --> CW2
    PQ --> CCL
    PDQ --> PDF

    CQ -->|3 failures| CRAWL_DLQ
    PQ -->|3 failures| PROC_DLQ
    PDQ -->|3 failures| MEDIA_DLQ

    style CRAWL_DLQ fill:#f66,stroke:#333,color:#000
    style PROC_DLQ fill:#f66,stroke:#333,color:#000
    style MEDIA_DLQ fill:#f66,stroke:#333,color:#000
```

---

## 7. Recrawl & Incremental Crawl Flow

```mermaid
flowchart TB
    subgraph triggers["EventBridge Triggers"]
        DAILY["Daily 2AM UTC\nrefresh_mode: incremental"]
        WEEKLY["Sunday 3AM UTC\nrefresh_mode: full"]
    end

    subgraph scheduler["Crawl Scheduler Lambda"]
        direction TB
        LIST_UNI["List universities\nfrom S3 configs/"]
        CHECK_RUNNING{"Pipeline\nalready running?"}
        CHECK_ENABLED{"Schedule\nenabled?"}
        CHECK_STALE{"Content\nstale?"}
        SKIP["Skip university"]
        START_SFN["Start Step Functions\nexecution"]
    end

    subgraph sfn["Step Functions State Machine"]
        direction TB
        VALIDATE["ValidateRequest"]
        CHECK_MODE{"CheckRefreshMode"}

        subgraph full_path["Full Crawl Path"]
            DISCOVER["DiscoverSeeds\n• Parse sitemaps\n• Homepage links\n• Config seeds"]
        end

        subgraph incr_path["Incremental Crawl Path"]
            QUEUE_STALE["QueueStaleUrls\n• Query url-registry GSI\n• Check last_crawled_at\n  vs freshness_windows\n• Re-queue stale only"]
        end

        WAIT["WaitForCrawl\n(120s loop)"]
        CHECK_PROG["CheckCrawlProgress\n• SQS queue depth\n• Pending URL count"]
        IS_DONE{"IsCrawlComplete?"}
        SHOULD_CLASS{"ShouldClassify?"}
        CLASSIFY["TriggerClassification\n(batch classifier)"]
        SUMMARY["GenerateSummary\n• kb_sync_status\n• pages_changed count"]
    end

    subgraph dedup["Crawler Worker — 3-Layer Deduplication"]
        direction TB
        L1{"Layer 1:\nCrawled < 24h ago?\n(1 DynamoDB read)"}
        L2{"Layer 2:\nHTTP 304\nNot Modified?\n(conditional headers)"}
        L3{"Layer 3:\nSHA-256 hash\nmatches stored?\n(full download)"}
        CHANGED["Content changed\n→ Store in S3\n→ Push to processing queue"]
        SKIP_URL["Skip URL\n(no processing)"]
    end

    subgraph freshness["Freshness Configuration"]
        direction LR
        ENTITY["entity-store\nfreshness_config"]
        EXAMPLE["Example windows:\n• events: 1 day\n• admissions: 2 days\n• about: 14 days\n• default: 1 day"]
    end

    %% Trigger flow
    DAILY --> LIST_UNI
    WEEKLY --> LIST_UNI
    LIST_UNI --> CHECK_RUNNING
    CHECK_RUNNING -->|yes| SKIP
    CHECK_RUNNING -->|no| CHECK_ENABLED
    CHECK_ENABLED -->|disabled| SKIP
    CHECK_ENABLED -->|enabled| CHECK_STALE
    CHECK_STALE -->|fresh / full mode| START_SFN
    CHECK_STALE -->|stale| START_SFN
    CHECK_STALE -->|fresh + incremental| SKIP

    %% State machine flow
    START_SFN --> VALIDATE
    VALIDATE --> CHECK_MODE
    CHECK_MODE -->|full| DISCOVER
    CHECK_MODE -->|incremental| QUEUE_STALE
    DISCOVER --> WAIT
    QUEUE_STALE --> WAIT
    WAIT --> CHECK_PROG
    CHECK_PROG --> IS_DONE
    IS_DONE -->|no| WAIT
    IS_DONE -->|yes| SHOULD_CLASS
    SHOULD_CLASS -->|incremental| SUMMARY
    SHOULD_CLASS -->|full| CLASSIFY
    CLASSIFY --> SUMMARY

    %% Dedup flow
    L1 -->|yes, recent| SKIP_URL
    L1 -->|no, proceed| L2
    L2 -->|304, unchanged| SKIP_URL
    L2 -->|200, downloaded| L3
    L3 -->|hash match| SKIP_URL
    L3 -->|hash differs| CHANGED

    %% Freshness feeds stale detection
    ENTITY --- EXAMPLE
    ENTITY -.->|read by| QUEUE_STALE

    style SKIP fill:#fbb,stroke:#333
    style SKIP_URL fill:#fbb,stroke:#333
    style CHANGED fill:#bfb,stroke:#333
    style CLASSIFY fill:#fed,stroke:#333
```


### Recrawl Flow Summary

| Step | Incremental (daily) | Full (weekly) |
|------|---------------------|---------------|
| **Trigger** | EventBridge 2AM UTC | EventBridge Sunday 3AM UTC |
| **Scheduler checks** | Running? Enabled? Stale? | Running? Enabled? (always triggers) |
| **URL selection** | Only stale URLs (per category freshness window) | ALL crawled URLs + new seeds |
| **Crawler dedup** | 3-layer (freshness → HTTP 304 → hash) | 3-layer (same) |
| **Classification** | Skipped (existing categories preserved) | Full batch classification |
| **Metadata sidecar** | Preserved (not deleted) | Re-created with new category |
| **KB sync** | Manual (admin notified if pages changed) | Manual (admin notified) |
| **Pipeline status** | `classify_stage = "skipped"` | `classify_stage = "completed"` |
