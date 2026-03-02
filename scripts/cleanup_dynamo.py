#!/usr/bin/env python3
"""
Clean up off-domain and junk URLs from the DynamoDB url-registry table.

Uses Query on the university-status-index GSI (much faster than Scan).
Each crawl_status is processed in parallel up to --parallel workers.
When a worker finishes one status, it immediately picks up the next.

Usage:
    python cleanup_dynamo.py --university-id gmu
    python cleanup_dynamo.py --university-id gmu --delete-s3 --bucket <bucket>
"""

import argparse
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

import boto3

# -------------------------------------------------------
# Config loading
# -------------------------------------------------------
CONFIGS_DIR = os.path.join(os.path.dirname(__file__), '..', 'configs')


def load_config(university_id):
    config_path = os.path.join(CONFIGS_DIR, f'{university_id}.json')
    with open(config_path) as f:
        return json.load(f)


# -------------------------------------------------------
# Domain filtering
# -------------------------------------------------------
def is_allowed_domain(url, allowed_domains, exclude_domains):
    try:
        hostname = urlparse(url).hostname
        if not hostname:
            return False
        hostname = hostname.lower()
    except Exception:
        return False

    for excl in exclude_domains:
        if hostname == excl.lower():
            return False

    for domain in allowed_domains:
        domain = domain.lower()
        if domain.startswith('*.'):
            base = domain[2:]
            if hostname == base or hostname.endswith('.' + base):
                return True
        else:
            if hostname == domain:
                return True

    return False


# -------------------------------------------------------
# Junk URL patterns (same as crawler-worker)
# -------------------------------------------------------
JUNK_URL_PATTERNS = [
    r'[?&](q|query|search|s)=',
    r'/search[/?]',
    r'/results[/?]',
    r'/calendar[/?].*[?&](month|year|date)=',
    r'/events[/?].*[?&](month|year|date|page)=',
    r'[?&]tribe-bar-date=',
    r'[?&]eventDisplay=',
    r'[?&]page=\d+',
    r'/page/\d+',
    r'[?&](print|format)=',
    r'/print/',
    r'/login',
    r'/signin',
    r'/logout',
    r'/sso/',
    r'/cas/',
    r'/auth/',
    r'/authorize\?',
    r'/oauth2?/',
    r'/wp-admin',
    r'/wp-login',
    r'/wp-json',
    r'/feed/?$',
    r'/xmlrpc',
    r'/cgi-bin/',
    r'[?&]C=[NMSD]',
    r'[?&]O=[AD]',
    r'[?&](utm_|fbclid|gclid)',
    r'[?&]share=',
    r'/tag/[^/]+/?$',
    r'/category/[^/]+/page/',
    r'/author/[^/]+/?$',
    r'/index\.html?$',
    r'/default\.aspx?$',
    r'[?&]hsLang=',
    r'/studentportal/',
    r'/facultyportal/',
    r'/payerportal/',
    r'/_hcms/',
    r'/hs/hsstatic/',
    r'[?&]preview=',
    r'[?&]replytocom=',
    r'[?&]ref=',
]

JUNK_PATTERNS_COMPILED = [re.compile(p, re.IGNORECASE) for p in JUNK_URL_PATTERNS]


def is_junk_url(url):
    return any(p.search(url) for p in JUNK_PATTERNS_COMPILED)


# -------------------------------------------------------
# DynamoDB / S3 batch helpers
# -------------------------------------------------------
def flush_delete_batch(dynamodb_client, table_name, batch):
    """Send a batch_write_item delete request. Returns (deleted, errors)."""
    try:
        response = dynamodb_client.batch_write_item(
            RequestItems={table_name: batch}
        )
        unprocessed = response.get('UnprocessedItems', {}).get(table_name, [])
        if unprocessed:
            time.sleep(0.5)
            try:
                dynamodb_client.batch_write_item(
                    RequestItems={table_name: unprocessed}
                )
                return len(batch), 0
            except Exception:
                return len(batch) - len(unprocessed), len(unprocessed)
        return len(batch), 0
    except Exception as e:
        print(f"  Batch delete error: {e}")
        return 0, len(batch)


def flush_s3_batch(s3_client, bucket, keys, status, print_lock):
    """Delete a batch of S3 objects. Returns count of successfully deleted."""
    try:
        resp = s3_client.delete_objects(
            Bucket=bucket,
            Delete={'Objects': [{'Key': k} for k in keys], 'Quiet': True},
        )
        return len(keys) - len(resp.get('Errors', []))
    except Exception as e:
        with print_lock:
            print(f"  [{status}] S3 error: {e}")
        return 0


# All known crawl_status values used in the system
CRAWL_STATUSES = [
    # "crawled",
    "pending",
    "redirected",
    "blocked_robots",
    "skipped_depth",
    "error",
    "failed",
    "dead",
]


# -------------------------------------------------------
# Per-status worker
# -------------------------------------------------------
def process_status(status, *, args, table, dynamodb_client, s3_client,
                   allowed_domains, exclude_domains, print_lock, start_time):
    """Query one crawl_status and delete off-domain / junk URLs."""
    counts = {
        'scanned': 0, 'kept': 0,
        'deleted_off_domain': 0, 'deleted_junk': 0, 'deleted_excluded': 0,
        'errors': 0, 's3_deleted': 0,
    }
    delete_batch = []
    s3_keys_batch = []

    exclude_set = {d.lower() for d in exclude_domains}

    query_kwargs = {
        'IndexName': args.index,
        'KeyConditionExpression': 'university_id = :uid AND crawl_status = :status',
        'ExpressionAttributeValues': {
            ':uid': args.university_id,
            ':status': status,
        },
        'ProjectionExpression': '#u, s3_key',
        'ExpressionAttributeNames': {'#u': 'url'},
    }

    while True:
        response = table.query(**query_kwargs)

        for item in response.get('Items', []):
            counts['scanned'] += 1
            url = item.get('url', '')

            should_delete = False
            reason = None

            if not is_allowed_domain(url, allowed_domains, exclude_domains):
                should_delete = True
                try:
                    hostname = (urlparse(url).hostname or 'unknown').lower()
                except Exception:
                    hostname = 'unknown'
                reason = 'excluded' if hostname in exclude_set else 'off_domain'
            elif is_junk_url(url):
                should_delete = True
                reason = 'junk'

            if should_delete:
                delete_batch.append({'DeleteRequest': {'Key': {'url': url}}})
                counts[f'deleted_{reason}'] += 1

                s3_key = item.get('s3_key', '')
                if s3_key and s3_client:
                    s3_keys_batch.append(s3_key)

                # Flush DynamoDB batch at 25
                if len(delete_batch) >= 25:
                    _, errs = flush_delete_batch(dynamodb_client, args.table, delete_batch)
                    counts['errors'] += errs
                    delete_batch = []

                # Flush S3 batch at 1000
                if s3_client and len(s3_keys_batch) >= 1000:
                    counts['s3_deleted'] += flush_s3_batch(
                        s3_client, args.bucket, s3_keys_batch, status, print_lock,
                    )
                    s3_keys_batch = []
            else:
                counts['kept'] += 1

        # Progress line after each page
        total_del = counts['deleted_off_domain'] + counts['deleted_junk'] + counts['deleted_excluded']
        elapsed = time.time() - start_time
        rate = counts['scanned'] / elapsed if elapsed > 0 else 0
        with print_lock:
            print(
                f"  [{status}] scanned: {counts['scanned']:,} | "
                f"deleted: {total_del:,} (off-domain: {counts['deleted_off_domain']:,}, "
                f"junk: {counts['deleted_junk']:,}, excluded: {counts['deleted_excluded']:,}) | "
                f"kept: {counts['kept']:,} | {rate:.0f}/s"
            )

        if 'LastEvaluatedKey' not in response:
            break
        query_kwargs['ExclusiveStartKey'] = response['LastEvaluatedKey']

    # Flush remaining batches
    if delete_batch:
        _, errs = flush_delete_batch(dynamodb_client, args.table, delete_batch)
        counts['errors'] += errs

    if s3_client and s3_keys_batch:
        counts['s3_deleted'] += flush_s3_batch(
            s3_client, args.bucket, s3_keys_batch, status, print_lock,
        )

    with print_lock:
        print(f"  [{status}] DONE — {counts['scanned']:,} items")

    return counts


# -------------------------------------------------------
# Main
# -------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description='Clean up off-domain and junk URLs from DynamoDB')
    parser.add_argument('--university-id', required=True, help='University ID (e.g., gmu)')
    parser.add_argument('--table', default='url-registry-dev', help='URL registry table name')
    parser.add_argument('--index', default='university-status-index', help='GSI name')
    parser.add_argument('--region', default='us-east-1', help='AWS region')
    parser.add_argument('--parallel', type=int, default=4, help='Max parallel workers (default: 4)')
    parser.add_argument('--delete-s3', action='store_true', help='Also delete raw content from S3')
    parser.add_argument('--bucket', default=None, help='S3 bucket name (required if --delete-s3)')
    args = parser.parse_args()

    if args.delete_s3 and not args.bucket:
        print("ERROR: --bucket is required when using --delete-s3")
        sys.exit(1)

    config = load_config(args.university_id)
    crawl_config = config.get('crawl_config', {})
    allowed_domains = crawl_config.get('allowed_domain_patterns', [])
    exclude_domains = crawl_config.get('exclude_domains', [])

    if not allowed_domains:
        print(f"ERROR: No allowed_domain_patterns in config for '{args.university_id}'")
        sys.exit(1)

    print(f"University:       {args.university_id}")
    print(f"Allowed domains:  {', '.join(allowed_domains)}")
    print(f"Exclude domains:  {', '.join(exclude_domains) if exclude_domains else '(none)'}")
    print(f"Table:            {args.table}")
    print(f"Index:            {args.index}")
    print(f"Parallel workers: {args.parallel}")
    print(f"Statuses:         {', '.join(CRAWL_STATUSES)}")
    print()

    dynamodb = boto3.resource('dynamodb', region_name=args.region)
    dynamodb_client = dynamodb.meta.client
    table = dynamodb.Table(args.table)
    s3_client = boto3.client('s3', region_name=args.region) if args.delete_s3 else None

    print_lock = threading.Lock()
    start_time = time.time()

    print("Querying and deleting...")
    print(f"{'─' * 70}")

    # Submit all statuses to the pool — workers pick up the next status
    # as soon as they finish the current one.
    totals = {
        'scanned': 0, 'kept': 0,
        'deleted_off_domain': 0, 'deleted_junk': 0, 'deleted_excluded': 0,
        'errors': 0, 's3_deleted': 0,
    }

    with ThreadPoolExecutor(max_workers=args.parallel) as executor:
        futures = {
            executor.submit(
                process_status, status,
                args=args,
                table=table,
                dynamodb_client=dynamodb_client,
                s3_client=s3_client,
                allowed_domains=allowed_domains,
                exclude_domains=exclude_domains,
                print_lock=print_lock,
                start_time=start_time,
            ): status
            for status in CRAWL_STATUSES
        }

        for future in as_completed(futures):
            status = futures[future]
            try:
                result = future.result()
                for key in totals:
                    totals[key] += result[key]
            except Exception as exc:
                with print_lock:
                    print(f"  [{status}] FAILED with exception: {exc}")

    # Final summary
    total_deleted = totals['deleted_off_domain'] + totals['deleted_junk'] + totals['deleted_excluded']
    elapsed = time.time() - start_time

    print(f"{'─' * 70}")
    print(f"\nDONE in {elapsed:.1f}s")
    print(f"  Scanned:             {totals['scanned']:,}")
    print(f"  Deleted off-domain:  {totals['deleted_off_domain']:,}")
    print(f"  Deleted excluded:    {totals['deleted_excluded']:,}")
    print(f"  Deleted junk:        {totals['deleted_junk']:,}")
    print(f"  Total deleted:       {total_deleted:,}")
    print(f"  Kept:                {totals['kept']:,}")
    print(f"  Errors:              {totals['errors']:,}")
    if args.delete_s3:
        print(f"  S3 objects deleted:  {totals['s3_deleted']:,}")


if __name__ == '__main__':
    main()
