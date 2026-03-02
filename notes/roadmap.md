# Roadmap

## 1. Migrate Dashboard API to FastAPI

### Goal
Replace the current hand-rolled Lambda HTTP router (`dashboard-api/handler.py`) with **FastAPI** so we get:
- Auto-generated `/docs` (Swagger UI) and `/redoc`
- Request/response validation via Pydantic models
- Cleaner route definitions with proper type hints
- Easier local development and testing

### Current State
- `lambdas/dashboard-api/handler.py` — manual regex router dispatching to route modules
- `lambdas/dashboard-api/routes/` — pipeline, reset, universities, categories, etc.
- Deployed as AWS Lambda + API Gateway (SAM)

### Target State
- Replace the manual router with FastAPI app
- Use **Mangum** as the ASGI adapter to run FastAPI inside Lambda
- Keep the same route modules, refactored to FastAPI `APIRouter`
- Same API Gateway + Lambda deployment via SAM
- Local dev: `uvicorn handler:app --reload`

### Migration Steps
1. Add `fastapi` and `mangum` to Lambda dependencies (`requirements.txt`)
2. Replace `handler.py` router with FastAPI app + Mangum adapter
3. Refactor each route module (`pipeline.py`, `reset.py`, etc.) to use `APIRouter` with Pydantic request/response models
4. Update SAM template if needed (API Gateway config)
5. Verify `/docs` accessible via API Gateway URL (or locally)
6. Run existing integration tests to confirm parity

### Key Files to Change
- `lambdas/dashboard-api/handler.py` — replace with FastAPI app
- `lambdas/dashboard-api/routes/*.py` — refactor to `APIRouter`
- `lambdas/dashboard-api/requirements.txt` — add fastapi, mangum, pydantic
- `lambdas/dashboard-api/utils/response.py` — can be removed (FastAPI handles responses)

### Notes
- Streamlit dashboard (`dashboard.py`) remains unchanged — it calls the API, not the Lambda directly
- `/docs` will be available at the API Gateway URL + `/docs`
- Mangum handles the Lambda event ↔ ASGI translation transparently

---

## 2. Testing Plan

### Unit Tests — `tests/unit/`
**Tools:** `pytest` + `moto` (AWS mocking)
**Trigger:** On every git push / PR in CI

| File | What it covers |
|---|---|
| `test_orchestrator.py` | `validate_request`, `check_progress` (queue states), `trigger_classification` (batch_jobs stored), `generate_summary` |
| `test_batch_prepare.py` | S3 listing, JSONL building, `nothing_to_classify` early return, correct S3 key format |
| `test_batch_process.py` | Output parsing, sidecar writing, DynamoDB updates, bad JSON handling |
| `test_dashboard_pipeline.py` | `classify_done` condition, `_check_batch_jobs` (empty/in-progress/completed), `_augment_live_progress` stage transitions |
| `test_reset.py` | `_stop_running_pipelines` finds all running SFN executions, S3 prefix deletion, DynamoDB cleanup |

### Integration Tests — `tests/integration/`
**Tools:** `pytest` + real boto3 against dev stack
**Trigger:** After `sam deploy` to dev (post-deploy CI step), or manually

| File | What it covers |
|---|---|
| `test_e2e_full.py` | Full pipeline for a dedicated `test-uni` (5 synthetic URLs): crawl → clean → classify → sidecars → pipeline-jobs = completed |
| `test_e2e_reset.py` | Start pipeline, reset mid-crawl, verify SFN stopped and all data cleared |
| `test_e2e_incremental.py` | Full run then incremental → no duplicate batch jobs, fresh pages skipped (`nothing_to_classify`) |
| `test_classification_status.py` | `_check_batch_jobs` reflects Bedrock job status: Submitted → InProgress → Completed |

### Design Decisions
- **Synthetic test university:** Use a dedicated `test-uni` config with 3–5 hardcoded seed URLs. Never use `minu`/`gmu` to avoid polluting real data.
- **Teardown:** Integration tests clean up after themselves (reset via API after each test)
- **Bedrock in CI:** Classification integration tests are skipped in CI (Bedrock batch jobs take 30–60 min). Run manually after deploy.
- **Moto limitation:** Moto doesn't support Bedrock — unit tests mock Bedrock responses directly via `unittest.mock`

### Makefile Targets
```makefile
test-unit:
    pytest tests/unit/ -v

test-integration:
    pytest tests/integration/ -v --env dev

test-all: test-unit test-integration
```
