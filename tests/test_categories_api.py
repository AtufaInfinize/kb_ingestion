"""
Tests for category CRUD operations (add, remove, rename).

Each test uses moto-mocked DynamoDB + S3.  Route handler functions are called
directly — no HTTP layer — after the module-level boto3 clients are re-pointed
to the mocked resources via importlib.reload().
"""

import sys
import json
import importlib
import hashlib
from unittest.mock import AsyncMock, patch, MagicMock
from urllib.parse import urlparse

import boto3
import pytest

from tests.conftest import BUCKET, TABLE, REGION, UID, seed_url, seed_s3_objects, _url_hash


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _get_dynamo_item(url: str):
    """Fetch a single item from the mocked DynamoDB table."""
    ddb = boto3.resource("dynamodb", region_name=REGION)
    table = ddb.Table(TABLE)
    resp = table.get_item(Key={"url": url})
    return resp.get("Item")


def _s3_exists(key: str) -> bool:
    """Return True if the S3 object exists in the mocked bucket."""
    s3 = boto3.client("s3", region_name=REGION)
    try:
        s3.head_object(Bucket=BUCKET, Key=key)
        return True
    except Exception:
        return False


def _reload_categories():
    """
    Re-import the categories route module so boto3 clients are created
    AFTER moto mocks are active (module-level clients must be refreshed).
    """
    # Remove cached module so imports pick up moto-mocked boto3
    for mod in list(sys.modules.keys()):
        if "routes.categories" in mod or "utils.dynamo" in mod or "utils.response" in mod:
            del sys.modules[mod]

    # Ensure the dashboard-api package is importable
    import sys as _sys
    api_path = str(__import__("pathlib").Path(__file__).parent.parent /
                   "lambdas" / "dashboard-api")
    if api_path not in _sys.path:
        _sys.path.insert(0, api_path)

    import routes.categories as cats
    return cats


async def _make_request(body: dict):
    """Build a minimal FastAPI Request-like mock carrying a JSON body."""
    req = AsyncMock()
    req.json = AsyncMock(return_value=body)
    return req


# ─── Test 1: remove_pages — mark_excluded ──────────────────────────────────────

def test_remove_mark_excluded(dynamo_table, s3_bucket, set_env_vars):
    """DELETE with action='mark_excluded' sets page_category='excluded' in DynamoDB.
    It must NOT delete the S3 .md or .metadata.json files."""
    import asyncio

    url = "https://test.edu/admissions"
    seed_url(dynamo_table, url, category="admissions")
    prefix = seed_s3_objects(s3_bucket, url)

    cats = _reload_categories()

    async def run():
        req = await _make_request({"urls": [url], "action": "mark_excluded"})
        return await cats.remove_pages(UID, "admissions", req)

    result = asyncio.run(run())
    body = result.body if hasattr(result, "body") else json.loads(result.body)
    if isinstance(body, (bytes, bytearray)):
        body = json.loads(body)

    # DynamoDB: must be excluded
    item = _get_dynamo_item(url)
    assert item is not None
    assert item["page_category"] == "excluded"
    assert item.get("manually_curated") is True

    # S3: files must still exist
    assert _s3_exists(f"{prefix}.md"),               ".md should NOT be deleted for mark_excluded"
    assert _s3_exists(f"{prefix}.md.metadata.json"), ".metadata.json should NOT be deleted for mark_excluded"


# ─── Test 2: remove_pages — delete_content ─────────────────────────────────────

def test_remove_delete_content(dynamo_table, s3_bucket, set_env_vars):
    """DELETE with action='delete_content' marks excluded AND deletes S3 objects."""
    import asyncio

    url = "https://test.edu/financial-aid"
    seed_url(dynamo_table, url, category="financial_aid")
    prefix = seed_s3_objects(s3_bucket, url)

    assert _s3_exists(f"{prefix}.md"),               "pre-condition: .md must exist"
    assert _s3_exists(f"{prefix}.md.metadata.json"), "pre-condition: .metadata.json must exist"

    cats = _reload_categories()

    async def run():
        req = await _make_request({"urls": [url], "action": "delete_content"})
        return await cats.remove_pages(UID, "financial_aid", req)

    asyncio.run(run())

    # DynamoDB: must be excluded
    item = _get_dynamo_item(url)
    assert item is not None
    assert item["page_category"] == "excluded"

    # S3: files must be DELETED
    assert not _s3_exists(f"{prefix}.md"),               ".md should be deleted for delete_content"
    assert not _s3_exists(f"{prefix}.md.metadata.json"), ".metadata.json should be deleted for delete_content"


# ─── Test 3: add_pages — new URL ───────────────────────────────────────────────

def test_add_new_url(dynamo_table, s3_bucket, set_env_vars):
    """POST to add a brand-new URL registers it in DynamoDB with correct fields."""
    import asyncio

    url = "https://new.edu/page"
    cats = _reload_categories()

    async def run():
        req = await _make_request({"urls": [url], "trigger_crawl": False})
        return await cats.add_pages(UID, "admissions", req)

    asyncio.run(run())

    item = _get_dynamo_item(url)
    assert item is not None,                            "URL must be registered in DynamoDB"
    assert item["university_id"] == UID
    assert item["page_category"] == "admissions"
    assert item["crawl_status"]  == "pending"
    assert item.get("manually_curated") is True
    assert "url_hash" in item,                          "url_hash must be set"
    assert "discovered_at" in item


# ─── Test 4: add_pages — existing URL ──────────────────────────────────────────

def test_add_existing_url(dynamo_table, s3_bucket, set_env_vars):
    """POST to add an already-registered URL updates its category."""
    import asyncio

    url = "https://test.edu/about"
    seed_url(dynamo_table, url, category="about")

    item_before = _get_dynamo_item(url)
    assert item_before["page_category"] == "about"

    cats = _reload_categories()

    async def run():
        req = await _make_request({"urls": [url], "trigger_crawl": False})
        return await cats.add_pages(UID, "admissions", req)

    asyncio.run(run())

    item_after = _get_dynamo_item(url)
    assert item_after["page_category"] == "admissions", "category must be updated"
    assert item_after.get("manually_curated") is True


# ─── Test 5: rename_category — happy path ──────────────────────────────────────

def test_rename_category(dynamo_table, s3_bucket, set_env_vars):
    """POST rename moves all pages from source to target category."""
    import asyncio

    urls = [f"https://test.edu/admissions/{i}" for i in range(5)]
    for url in urls:
        seed_url(dynamo_table, url, category="admissions")

    cats = _reload_categories()

    async def run():
        req = await _make_request({"new_category": "about"})
        return await cats.rename_category(UID, "admissions", req)

    result = asyncio.run(run())
    raw = result.body if hasattr(result, "body") else b""
    if isinstance(raw, (bytes, bytearray)):
        body = json.loads(raw)
    else:
        body = raw

    # Response fields
    assert body.get("pages_moved") == 5,       f"Expected 5 moved, got {body}"
    assert body.get("old_category") == "admissions"
    assert body.get("new_category")  == "about"
    assert body.get("errors")        == []

    # DynamoDB: all URLs must now have page_category="about"
    for url in urls:
        item = _get_dynamo_item(url)
        assert item["page_category"] == "about", f"{url} still has wrong category: {item}"


# ─── Test 6: rename_category — invalid target ──────────────────────────────────

def test_rename_invalid_target(dynamo_table, s3_bucket, set_env_vars):
    """POST rename with invalid new_category returns 400."""
    import asyncio

    url = "https://test.edu/admissions/0"
    seed_url(dynamo_table, url, category="admissions")

    cats = _reload_categories()

    async def run():
        req = await _make_request({"new_category": "not_a_real_category"})
        return await cats.rename_category(UID, "admissions", req)

    result = asyncio.run(run())
    raw = result.body if hasattr(result, "body") else b""
    if isinstance(raw, (bytes, bytearray)):
        body = json.loads(raw)
    else:
        body = raw

    # Should be a 400 error response
    assert "error" in body, f"Expected error field in response: {body}"
    # page_category must NOT have changed
    item = _get_dynamo_item(url)
    assert item["page_category"] == "admissions", "Category must be unchanged on invalid rename"
