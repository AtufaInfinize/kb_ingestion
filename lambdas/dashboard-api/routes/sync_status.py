"""Lightweight KB sync status health check endpoint."""

import logging

from fastapi import APIRouter

from utils.response import api_response
from utils.dynamo import entity_table, find_running_pipeline

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/universities/{uid}/sync-status")
def get_sync_status(uid: str):
    """GET /v1/universities/{uid}/sync-status — Fast sync health check.

    Single DynamoDB get_item + running pipeline check.  Much cheaper than
    the full stats endpoint (8 parallel queries).  Frontend can poll this
    every few seconds to show "KB sync needed" banners.
    """
    try:
        resp = entity_table.get_item(
            Key={'university_id': uid, 'entity_key': 'kb_sync_status'}
        )
        item = resp.get('Item', {})

        status = item.get('status', '')
        pages_changed = int(item.get('pages_changed', 0))
        categories_modified = int(item.get('categories_modified', 0))
        data_reset = bool(item.get('data_reset', False))

        sync_needed = (
            status == 'pending_sync'
            or categories_modified > 0
            or data_reset
        )

        pipeline_running = find_running_pipeline(uid) is not None

        return api_response(200, {
            'university_id': uid,
            'sync_needed': sync_needed,
            'reasons': {
                'pages_changed': pages_changed,
                'categories_modified': categories_modified,
                'data_reset': data_reset,
            },
            'status': status or None,
            'crawl_completed_at': item.get('crawl_completed_at'),
            'synced_at': item.get('synced_at'),
            'pipeline_running': pipeline_running,
        })

    except Exception as e:
        logger.error(f"sync-status error for {uid}: {e}", exc_info=True)
        return api_response(500, {'error': str(e)})
