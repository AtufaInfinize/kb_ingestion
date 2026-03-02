"""Classification status endpoints — Bedrock batch job progress."""

import logging

import boto3
from fastapi import APIRouter, Query

from utils.response import api_response

logger = logging.getLogger(__name__)
router = APIRouter()

bedrock = boto3.client('bedrock')


@router.get("/universities/{uid}/classification")
def get_classification_status(uid: str, limit: int = Query(default=10, le=50)):
    """GET /v1/universities/{uid}/classification — Batch classification progress."""
    try:
        resp = bedrock.list_model_invocation_jobs(
            maxResults=100,
            sortBy='CreationTime',
            sortOrder='Descending',
        )

        jobs = []
        for job in resp.get('invocationJobSummaries', []):
            job_name = job.get('jobName', '')
            if f'-{uid}-' not in job_name and not job_name.startswith(f'classify-{uid}-'):
                continue

            job_info = {
                'job_name': job_name,
                'job_arn': job.get('jobArn'),
                'status': job.get('status'),
                'model_id': job.get('modelId', ''),
                'submitted_at': _fmt_dt(job.get('submitTime')),
                'last_modified_at': _fmt_dt(job.get('lastModifiedTime')),
                'end_time': _fmt_dt(job.get('endTime')),
                'message': job.get('message', ''),
            }

            input_config = job.get('inputDataConfig', {}).get('s3InputDataConfig', {})
            output_config = job.get('outputDataConfig', {}).get('s3OutputDataConfig', {})
            job_info['input_s3_uri'] = input_config.get('s3Uri', '')
            job_info['output_s3_uri'] = output_config.get('s3Uri', '')

            jobs.append(job_info)
            if len(jobs) >= limit:
                break

        status_counts = {}
        for j in jobs:
            s = j['status']
            status_counts[s] = status_counts.get(s, 0) + 1

        return api_response(200, {
            'university_id': uid,
            'total_jobs': len(jobs),
            'status_summary': status_counts,
            'jobs': jobs,
        })

    except Exception as e:
        logger.error(f"get_classification_status error: {e}", exc_info=True)
        return api_response(500, {'error': str(e)})


def _fmt_dt(dt_obj):
    """Format datetime to ISO string, return None if not set."""
    if dt_obj is None:
        return None
    try:
        return dt_obj.isoformat()
    except Exception:
        return str(dt_obj)
