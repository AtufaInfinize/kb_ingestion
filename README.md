# University KB Crawler — Frontend Integration Package

API reference and types for integrating the University Knowledge Base Crawler backend into your frontend application.

## Quick Start

**Base URL:**
```
https://{api-id}.execute-api.us-east-1.amazonaws.com/dev/v1
```

**Authentication:** API Key via `X-Api-Key` header

**CORS:** All origins allowed; methods GET, POST, DELETE, OPTIONS

```typescript
import type { DashboardStats, CategoriesResponse } from "./types/api-types";

const API_BASE = "https://{api-id}.execute-api.us-east-1.amazonaws.com/dev/v1";

const res = await fetch(`${API_BASE}/universities/gmu/stats`, {
  headers: { "X-Api-Key": process.env.NEXT_PUBLIC_API_KEY! },
});
const stats: DashboardStats = await res.json();
```

## What's in This Branch

| Directory | Contents |
|-----------|----------|
| [`api-reference/`](api-reference/) | Full API guide — all 22 endpoints with request/response schemas, examples, pagination patterns, caching strategy |
| [`types/`](types/) | Standalone TypeScript interfaces and enums — import directly into your project |
| [`reference/`](reference/) | Streamlit dashboard (Python) — reference implementation showing how every API endpoint is called |
| [`docs/`](docs/) | Architecture diagrams and system documentation — for understanding the backend |

## For AI Assistants

If you're an AI coding assistant helping with frontend integration:

1. **Read [`api-reference/frontend-integration-guide.md`](api-reference/frontend-integration-guide.md)** first — it has every endpoint, every field, every status code
2. **Import from [`types/api-types.ts`](types/api-types.ts)** — all TypeScript interfaces are ready to use
3. **Reference [`reference/dashboard.py`](reference/dashboard.py)** for how the Streamlit dashboard calls each API (search for `api_get`, `api_post`, `api_delete`)

## API Overview (22 Endpoints)

| # | Method | Path | Description |
|---|--------|------|-------------|
| 1 | GET | `/universities` | List configured universities |
| 2 | GET | `/universities/{uid}/stats` | Dashboard overview stats |
| 3 | GET | `/universities/{uid}/config` | Get university config |
| 4 | POST | `/universities/{uid}/config` | Save university config |
| 5 | GET | `/universities/{uid}/categories` | Category cards with counts |
| 6 | GET | `/universities/{uid}/categories/{cat}/pages` | Paginated pages list |
| 7 | POST | `/universities/{uid}/categories/{cat}/pages` | Add URLs to category |
| 8 | DELETE | `/universities/{uid}/categories/{cat}/pages` | Remove URLs |
| 9 | POST | `/universities/{uid}/categories/{cat}/media/upload-url` | Get presigned upload URL |
| 10 | POST | `/universities/{uid}/categories/{cat}/media/process` | Trigger media processing |
| 11 | POST | `/universities/{uid}/categories/{cat}/rename` | Rename category |
| 12 | POST | `/universities/{uid}/categories/{cat}/delete` | Delete category |
| 13 | POST | `/universities/{uid}/pipeline` | Start crawl pipeline |
| 14 | GET | `/universities/{uid}/pipeline` | List pipeline jobs |
| 15 | GET | `/universities/{uid}/pipeline/{job_id}` | Live job progress |
| 16 | POST | `/universities/{uid}/kb/sync` | Trigger KB sync |
| 17 | GET | `/universities/{uid}/kb/sync` | KB sync status |
| 18 | GET | `/universities/{uid}/freshness` | Get freshness windows |
| 19 | POST | `/universities/{uid}/freshness` | Save freshness windows |
| 20 | GET | `/universities/{uid}/classification` | Classification job status |
| 21 | GET | `/universities/{uid}/dlq` | DLQ inspection |
| 22 | POST | `/universities/{uid}/reset` | Reset university data |

## Backend Code

The full backend codebase (Lambda functions, SAM template, scripts, tests) is on the [`main`](../../tree/main) branch.
