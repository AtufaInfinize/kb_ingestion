"""Freshness window CRUD endpoints — stored per-university in entity-store-dev."""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Request

from utils.response import api_response
from utils.dynamo import (
    entity_table,
    load_freshness_windows,
    CRAWLABLE_CATEGORIES,
    DEFAULT_FRESHNESS_DAYS,
    _FRESHNESS_ENTITY_KEY,
)

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/universities/{uid}/freshness")
def get_freshness(uid: str):
    """GET /v1/universities/{uid}/freshness — Per-category freshness windows (days) + schedule toggles.

    Returns the configured windows from DynamoDB, falling back to 1 day per
    category for any university that hasn't saved custom settings yet.
    Also returns schedule enable/disable flags (default: both enabled).
    """
    try:
        windows = load_freshness_windows(uid)
        fw_resp = entity_table.get_item(
            Key={'university_id': uid, 'entity_key': _FRESHNESS_ENTITY_KEY}
        )
        item = fw_resp.get('Item', {})
        schedule = {
            'incremental_enabled': bool(item.get('incremental_enabled', True)),
            'full_enabled': bool(item.get('full_enabled', True)),
        }
        return api_response(200, {'university_id': uid, 'windows': windows, 'schedule': schedule})
    except Exception as e:
        logger.error(f"get_freshness error: {e}", exc_info=True)
        return api_response(500, {'error': str(e)})


@router.post("/universities/{uid}/freshness")
async def save_freshness(uid: str, request: Request):
    """POST /v1/universities/{uid}/freshness — Save per-category freshness windows.

    Body: {"windows": {"admissions": 3, "events": 1, ...}}
    """
    try:
        body = await request.json()
    except Exception:
        return api_response(400, {'error': 'Invalid JSON body'})

    windows = body.get('windows')
    if not isinstance(windows, dict) or not windows:
        return api_response(400, {'error': '"windows" dict is required'})

    cleaned = {}
    for cat, days in windows.items():
        try:
            d = int(days)
        except (TypeError, ValueError):
            return api_response(400, {'error': f'Invalid days value for {cat}: {days}'})
        if d < 1 or d > 3650:
            return api_response(400, {'error': f'days for {cat} must be 1–3650'})
        cleaned[cat] = d

    schedule = body.get('schedule', {})
    incremental_enabled = bool(schedule.get('incremental_enabled', True))
    full_enabled = bool(schedule.get('full_enabled', True))

    try:
        entity_table.put_item(Item={
            'university_id': uid,
            'entity_key': _FRESHNESS_ENTITY_KEY,
            'entity_type': 'freshness_config',
            'windows': cleaned,
            'incremental_enabled': incremental_enabled,
            'full_enabled': full_enabled,
            'updated_at': datetime.now(timezone.utc).isoformat(),
        })
        return api_response(200, {
            'status': 'saved',
            'university_id': uid,
            'windows': cleaned,
            'schedule': {'incremental_enabled': incremental_enabled, 'full_enabled': full_enabled},
        })
    except Exception as e:
        logger.error(f"save_freshness error: {e}", exc_info=True)
        return api_response(500, {'error': str(e)})
