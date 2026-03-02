"""
Dashboard API Lambda — FastAPI + Mangum ASGI adapter.

Routes all /v1/universities/* API requests for the admin dashboard.

Endpoints:
  GET  /v1/universities                              — List universities
  GET  /v1/universities/{uid}/stats                   — Dashboard overview
  GET  /v1/universities/{uid}/categories              — Category cards
  GET  /v1/universities/{uid}/categories/{cat}/pages  — Pages in category
  POST /v1/universities/{uid}/categories/{cat}/pages  — Add URLs
  DELETE /v1/universities/{uid}/categories/{cat}/pages — Remove URLs
  POST /v1/universities/{uid}/categories/{cat}/media/upload-url — Presigned upload
  POST /v1/universities/{uid}/pipeline                — Start pipeline
  GET  /v1/universities/{uid}/pipeline                — List jobs
  GET  /v1/universities/{uid}/pipeline/{job_id}       — Job status
  POST /v1/universities/{uid}/kb/sync                 — Trigger KB sync
  GET  /v1/universities/{uid}/kb/sync                 — KB sync status
  GET  /v1/universities/{uid}/classification           — Batch classification progress
  GET  /v1/universities/{uid}/config                   — Get university config
  POST /v1/universities/{uid}/config                   — Create/update config
  POST /v1/universities/{uid}/categories/{cat}/rename  — Rename category
  POST /v1/universities/{uid}/categories/{cat}/delete  — Delete category
  POST /v1/universities/{uid}/reset                    — Reset/delete university data
  GET  /v1/universities/{uid}/dlq                      — Inspect DLQ messages (read-only)
  GET  /v1/universities/{uid}/freshness                — Get per-category freshness windows
  POST /v1/universities/{uid}/freshness                — Save per-category freshness windows

Local dev:
  uvicorn handler:app --reload
"""

import logging
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from mangum import Mangum

from utils.response import DecimalJSONResponse
from routes import stats, categories, pipeline, kb, classification, config, reset, maintenance, freshness

logging.basicConfig(level=logging.INFO)

# API Gateway prepends the stage name to all paths (e.g. /dev/docs).
# Setting root_path tells FastAPI to prefix the OpenAPI URL in Swagger UI so
# the browser requests /dev/openapi.json instead of /openapi.json.
_stage = os.environ.get("STAGE", "")
_root_path = f"/{_stage}" if _stage else ""

app = FastAPI(
    title="University Crawler Dashboard API",
    version="1.0.0",
    default_response_class=DecimalJSONResponse,
    root_path=_root_path,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "X-Api-Key", "Authorization"],
)

app.include_router(stats.router, prefix="/v1")
app.include_router(categories.router, prefix="/v1")
app.include_router(pipeline.router, prefix="/v1")
app.include_router(kb.router, prefix="/v1")
app.include_router(classification.router, prefix="/v1")
app.include_router(config.router, prefix="/v1")
app.include_router(reset.router, prefix="/v1")
app.include_router(maintenance.router, prefix="/v1")
app.include_router(freshness.router, prefix="/v1")

# Mangum ASGI adapter — this becomes the Lambda handler entry point
lambda_handler = Mangum(app, lifespan="off")
