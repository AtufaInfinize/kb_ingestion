"""
Requeue Missing Content Script

Finds pages that have raw HTML in S3 but are missing from clean-content,
and re-sends them to the processing queue for content cleaning.

Caches S3 listings and DynamoDB lookups locally in data/ to avoid
repeated AWS calls. Use --refresh to force re-fetch from AWS.

Usage:
    # Dry run - show what's missing
    python3 scripts/requeue_missing_content.py --university-id gmu --dry-run

    # Dry run for a specific domain
    python3 scripts/requeue_missing_content.py --university-id gmu --domain catalog.gmu.edu --dry-run

    # Force refresh from AWS (re-fetch S3 listings and DynamoDB)
    python3 scripts/requeue_missing_content.py --university-id gmu --dry-run --refresh

    # Actually send to queue
    python3 scripts/requeue_missing_content.py --university-id gmu \
        --queue-url https://sqs.us-east-1.amazonaws.com/251221984842/processing-queue-dev

    # Check queue depth only (no requeue)
    python3 scripts/requeue_missing_content.py --university-id gmu --check-queue-only \
        --queue-url https://sqs.us-east-1.amazonaws.com/251221984842/processing-queue-dev
"""

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from decimal import Decimal

import boto3

BUCKET = 'university-kb-content-251221984842-dev'
URL_REGISTRY_TABLE = 'url-registry-dev'
REGION = 'us-east-1'
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'data')

s3 = boto3.client('s3', region_name=REGION)
sqs = boto3.client('sqs', region_name=REGION)
dynamodb = boto3.resource('dynamodb', region_name=REGION)


class DecimalEncoder(json.JSONEncoder):
    """Handle Decimal types from DynamoDB."""
    def default(self, o):
        if isinstance(o, Decimal):
            return int(o) if o == int(o) else float(o)
        return super().default(o)


def cache_path(university_id, name):
    """Return path to a local cache file under data/."""
    uid_dir = os.path.join(DATA_DIR, university_id)
    os.makedirs(uid_dir, exist_ok=True)
    return os.path.join(uid_dir, f"{name}.json")


def load_cache(university_id, name):
    """Load cached data from local file. Returns (data, timestamp) or (None, None)."""
    path = cache_path(university_id, name)
    if not os.path.exists(path):
        return None, None
    try:
        with open(path, 'r') as f:
            doc = json.load(f)
        return doc.get('data'), doc.get('cached_at', '')
    except Exception:
        return None, None


def save_cache(university_id, name, data):
    """Save data to local cache file."""
    path = cache_path(university_id, name)
    doc = {
        'cached_at': datetime.now(timezone.utc).isoformat(),
        'count': len(data) if isinstance(data, (dict, list)) else 0,
        'data': data,
    }
    with open(path, 'w') as f:
        json.dump(doc, f, cls=DecimalEncoder, separators=(',', ':'))
    print(f"  Cached to {os.path.relpath(path)}")


def get_queue_depth(queue_url):
    """Get the current number of messages in an SQS queue."""
    attrs = sqs.get_queue_attributes(
        QueueUrl=queue_url,
        AttributeNames=['ApproximateNumberOfMessages', 'ApproximateNumberOfMessagesNotVisible']
    )['Attributes']
    visible = int(attrs.get('ApproximateNumberOfMessages', 0))
    in_flight = int(attrs.get('ApproximateNumberOfMessagesNotVisible', 0))
    return visible, in_flight


def list_s3_hashes(prefix):
    """List all file hashes under an S3 prefix. Returns {url_hash: s3_key}."""
    paginator = s3.get_paginator('list_objects_v2')
    hashes = {}
    count = 0

    for page in paginator.paginate(Bucket=BUCKET, Prefix=prefix):
        for obj in page.get('Contents', []):
            key = obj['Key']
            parts = key.split('/')
            if len(parts) >= 4:
                filename = parts[-1]
                url_hash = filename.split('.')[0]
                if url_hash:
                    hashes[url_hash] = key
            count += 1
            if count % 50000 == 0:
                sys.stdout.write(f"\r  Listed {count:,} objects under {prefix}...")
                sys.stdout.flush()

    if count >= 50000:
        print()
    return hashes


def get_s3_hashes(university_id, prefix_type, domain, refresh):
    """Get S3 hashes with local caching. prefix_type is 'raw-html' or 'clean-content'."""
    cache_name = f"s3_{prefix_type.replace('-', '_')}"
    if domain:
        cache_name += f"_{domain}"

    if not refresh:
        cached, cached_at = load_cache(university_id, cache_name)
        if cached is not None:
            print(f"  Loaded from cache ({len(cached):,} entries, cached at {cached_at})")
            return cached

    prefix = f"{prefix_type}/{university_id}/"
    if domain:
        prefix = f"{prefix_type}/{university_id}/{domain}/"

    hashes = list_s3_hashes(prefix)
    save_cache(university_id, cache_name, hashes)
    return hashes


def load_url_metadata_for_hashes(university_id, url_hashes, refresh, domain=None):
    """Load URL metadata from DynamoDB for specific url_hashes, with caching.

    When domain is specified, uses domain-status-index for much faster queries
    (only scans that domain instead of all university pages).
    """
    cache_name = f"dynamo_url_metadata_{domain}" if domain else "dynamo_url_metadata"

    if not refresh:
        cached, cached_at = load_cache(university_id, cache_name)
        if cached is not None:
            # Filter to only the hashes we need
            target_set = set(url_hashes)
            filtered = {h: v for h, v in cached.items() if h in target_set}
            print(f"  Loaded from cache ({len(cached):,} total, {len(filtered):,} matched, cached at {cached_at})")
            return filtered

    table = dynamodb.Table(URL_REGISTRY_TABLE)
    lookup = {}

    if domain:
        # Use domain-status-index for targeted query (much faster)
        query_kwargs = {
            'IndexName': 'domain-status-index',
            'KeyConditionExpression': '#d = :domain AND crawl_status = :cs',
            'ExpressionAttributeValues': {':domain': domain, ':cs': 'crawled'},
            'ProjectionExpression': '#u, url_hash, #d, #dep, s3_key, crawled_at, last_crawled_at, content_type, processing_status',
            'ExpressionAttributeNames': {'#u': 'url', '#d': 'domain', '#dep': 'depth'},
        }
    else:
        query_kwargs = {
            'IndexName': 'university-status-index',
            'KeyConditionExpression': 'university_id = :uid AND crawl_status = :cs',
            'ExpressionAttributeValues': {':uid': university_id, ':cs': 'crawled'},
            'ProjectionExpression': '#u, url_hash, #d, #dep, s3_key, crawled_at, last_crawled_at, content_type, processing_status',
            'ExpressionAttributeNames': {'#u': 'url', '#d': 'domain', '#dep': 'depth'},
        }

    target_set = set(url_hashes)
    count = 0

    while True:
        resp = table.query(**query_kwargs)
        for item in resp.get('Items', []):
            h = item.get('url_hash', '')
            if h:
                lookup[h] = item
        count += len(resp.get('Items', []))
        sys.stdout.write(f"\r  Scanned {count:,} DynamoDB items, matched {len(lookup):,}...")
        sys.stdout.flush()
        if 'LastEvaluatedKey' not in resp:
            break
        query_kwargs['ExclusiveStartKey'] = resp['LastEvaluatedKey']

    print()
    save_cache(university_id, cache_name, lookup)

    # Return only the hashes we need
    return {h: v for h, v in lookup.items() if h in target_set}


def send_to_queue(queue_url, messages, send_threads=20, batch_size=10):
    """Send messages to SQS queue in parallel. Returns (sent, errors)."""
    batches = []
    batch = []
    for msg in messages:
        batch.append({
            'Id': str(len(batch)),
            'MessageBody': json.dumps(msg)
        })
        if len(batch) >= batch_size:
            batches.append(batch)
            batch = []
    if batch:
        batches.append(batch)

    sent = 0
    errors = 0
    failed_urls = []
    start_time = time.time()

    def _send(entries):
        try:
            response = sqs.send_message_batch(QueueUrl=queue_url, Entries=entries)
            ok = len(response.get('Successful', []))
            fail = response.get('Failed', [])
            return ok, fail
        except Exception as e:
            return 0, [{'Message': str(e)} for _ in entries]

    with ThreadPoolExecutor(max_workers=send_threads) as pool:
        futures = [pool.submit(_send, b) for b in batches]
        for f in as_completed(futures):
            ok, fail = f.result()
            sent += ok
            if fail:
                errors += len(fail)
                for item in fail[:3]:
                    failed_urls.append(str(item))
            if sent % 500 < batch_size:
                elapsed = time.time() - start_time
                rate = sent / elapsed if elapsed > 0 else 0
                sys.stdout.write(f"\r  Sent {sent:,}/{len(messages):,} ({rate:.0f}/s)...")
                sys.stdout.flush()

    print()
    if failed_urls:
        print(f"  Sample failures: {failed_urls[:5]}")

    return sent, errors


def main():
    parser = argparse.ArgumentParser(description='Requeue pages missing from clean-content')
    parser.add_argument('--university-id', required=True, help='University ID (e.g., gmu)')
    parser.add_argument('--domain', help='Filter to a specific domain (e.g., catalog.gmu.edu)')
    parser.add_argument('--queue-url', help='SQS processing queue URL')
    parser.add_argument('--dry-run', action='store_true', help='Show what would be queued without sending')
    parser.add_argument('--check-queue-only', action='store_true', help='Only show queue depth, no requeue')
    parser.add_argument('--refresh', action='store_true', help='Force re-fetch from AWS (ignore local cache)')
    parser.add_argument('--send-threads', type=int, default=20, help='Parallel SQS send threads')
    parser.add_argument('--show-urls', type=int, default=20, help='Number of missing URLs to show in dry run')
    args = parser.parse_args()

    university_id = args.university_id

    # --- Check queue depth ---
    if args.queue_url:
        visible, in_flight = get_queue_depth(args.queue_url)
        print(f"\nQueue depth BEFORE:")
        print(f"  Visible (waiting):   {visible:,}")
        print(f"  In-flight (processing): {in_flight:,}")
        print(f"  Total:               {visible + in_flight:,}")

        if args.check_queue_only:
            return

    # --- List raw-html files ---
    print(f"\nRaw HTML files for {university_id}" + (f" ({args.domain})" if args.domain else "") + ":")
    raw_hashes = get_s3_hashes(university_id, 'raw-html', args.domain, args.refresh)
    print(f"  Total: {len(raw_hashes):,}")

    # --- List clean-content files ---
    print(f"\nClean content files for {university_id}" + (f" ({args.domain})" if args.domain else "") + ":")
    clean_hashes = get_s3_hashes(university_id, 'clean-content', args.domain, args.refresh)
    print(f"  Total: {len(clean_hashes):,}")

    # --- Find missing ---
    missing_hashes = set(raw_hashes.keys()) - set(clean_hashes.keys())
    print(f"\n{'='*50}")
    print(f"MISSING FROM CLEAN-CONTENT: {len(missing_hashes):,}")
    print(f"{'='*50}")

    if not missing_hashes:
        print("All raw HTML files have clean content. Nothing to do.")
        return

    # --- Breakdown by domain ---
    domain_counts = defaultdict(int)
    for h in missing_hashes:
        raw_key = raw_hashes[h]
        parts = raw_key.split('/')
        domain = parts[2] if len(parts) >= 4 else 'unknown'
        domain_counts[domain] += 1

    print(f"\nMissing pages by domain:")
    for domain, count in sorted(domain_counts.items(), key=lambda x: -x[1]):
        print(f"  {domain}: {count:,}")

    # --- Load DynamoDB metadata for missing pages ---
    print(f"\nLoading URL metadata for {len(missing_hashes):,} missing pages...")
    url_lookup = load_url_metadata_for_hashes(university_id, missing_hashes, args.refresh, domain=args.domain)
    print(f"  Matched {len(url_lookup):,} of {len(missing_hashes):,} in DynamoDB")

    unmatched = missing_hashes - set(url_lookup.keys())
    if unmatched:
        print(f"  WARNING: {len(unmatched):,} hashes not found in DynamoDB (orphaned S3 files)")

    # --- Build queue messages ---
    # Only requeue pages that were NEVER processed.
    # Pages with processing_status set (e.g. 'empty_content') were already
    # processed by the content cleaner but Trafilatura returned nothing —
    # re-queuing them would just fail again.
    messages = []
    no_s3_key = 0
    no_url = 0
    already_processed = 0
    processed_statuses = defaultdict(int)

    for url_hash in missing_hashes:
        meta = url_lookup.get(url_hash, {})
        url = meta.get('url', '')
        s3_key = meta.get('s3_key', '')

        if not url:
            no_url += 1
            continue
        if not s3_key:
            # Construct from raw_hashes
            s3_key = raw_hashes.get(url_hash, '')
            if not s3_key:
                no_s3_key += 1
                continue

        # Skip pages already processed by content cleaner
        proc_status = meta.get('processing_status', '')
        if proc_status:
            already_processed += 1
            processed_statuses[proc_status] += 1
            continue

        messages.append({
            'url': url,
            'university_id': university_id,
            'domain': meta.get('domain', ''),
            's3_key': s3_key,
            'content_type': 'text/html',
            'url_hash': url_hash,
            'depth': int(meta.get('depth', 0)),
            'crawled_at': meta.get('crawled_at', meta.get('last_crawled_at', '')),
            'action': 'process',
        })

    print(f"\nFiltering results:")
    if already_processed:
        print(f"  Already processed (skipped): {already_processed:,}")
        for status, cnt in sorted(processed_statuses.items(), key=lambda x: -x[1]):
            print(f"    {status}: {cnt:,}")
    if no_url:
        print(f"  No URL in DynamoDB:          {no_url:,}")
    if no_s3_key:
        print(f"  No S3 key:                   {no_s3_key:,}")

    print(f"\nPages to requeue: {len(messages):,}")

    # --- Show sample missing URLs ---
    if args.dry_run or args.show_urls > 0:
        sample = sorted(messages, key=lambda m: (m['domain'], m['url']))[:args.show_urls]
        print(f"\nSample missing URLs (showing {len(sample)}):")
        for msg in sample:
            print(f"  [{msg['domain']}] {msg['url']}")
        if len(messages) > args.show_urls:
            print(f"  ... and {len(messages) - args.show_urls:,} more")

    # --- Save missing list to data/ ---
    missing_list_path = cache_path(university_id, "missing_urls")
    missing_doc = {
        'cached_at': datetime.now(timezone.utc).isoformat(),
        'total_missing': len(missing_hashes),
        'already_processed': already_processed,
        'orphaned': len(unmatched),
        'to_requeue': len(messages),
        'domain_breakdown': dict(domain_counts),
        'urls': sorted([{'url': m['url'], 'domain': m['domain'], 'url_hash': m['url_hash'],
                         's3_key': m['s3_key'], 'depth': m['depth']} for m in messages],
                       key=lambda x: (x['domain'], x['url'])),
    }
    with open(missing_list_path, 'w') as f:
        json.dump(missing_doc, f, indent=2)
    print(f"\nSaved missing URLs list to {os.path.relpath(missing_list_path)}")

    if args.dry_run:
        print(f"Dry run complete. Run without --dry-run and with --queue-url to send.")
        return

    if not args.queue_url:
        print(f"\nNo --queue-url provided. Use --queue-url to send messages.")
        return

    # --- Send to queue ---
    print(f"\nSending {len(messages):,} messages to queue...")
    sent, errors = send_to_queue(args.queue_url, messages, args.send_threads)

    print(f"\nSend results:")
    print(f"  Sent:   {sent:,}")
    print(f"  Errors: {errors:,}")

    # --- Check queue depth AFTER ---
    time.sleep(2)
    visible_after, in_flight_after = get_queue_depth(args.queue_url)
    print(f"\nQueue depth AFTER:")
    print(f"  Visible (waiting):      {visible_after:,}")
    print(f"  In-flight (processing): {in_flight_after:,}")
    print(f"  Total:                  {visible_after + in_flight_after:,}")
    print(f"  New messages added:     ~{sent:,}")


if __name__ == '__main__':
    main()
