#!/usr/bin/env python3
"""
Backfill links_to from raw HTML already stored in S3 into DynamoDB.

Fully parallel single-pass design: each worker thread independently downloads
HTML from S3, extracts links, and writes links_to to DynamoDB.
The main thread only does fast local I/O (checkpoint + JSONL append).

On restart, already-checkpointed keys are skipped — no re-downloading.
The local JSONL results file can be used later to compute links_from (reverse
index) without re-processing.

Usage:
    # Dry run (extract + count, no writes)
    python3 scripts/backfill_links_to_dynamo.py --university-id gmu --dry-run

    # Full run (writes to DynamoDB, saves results locally)
    python3 scripts/backfill_links_to_dynamo.py --university-id gmu --workers 20

    # Resume after crash (automatic — skips checkpointed keys)
    python3 scripts/backfill_links_to_dynamo.py --university-id gmu --workers 20

    # Start fresh (delete checkpoint + results, reprocess everything)
    python3 scripts/backfill_links_to_dynamo.py --university-id gmu --workers 20 --reset
"""

import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse, urljoin, urldefrag

import boto3
from botocore.config import Config as BotoConfig

BUCKET = os.environ.get('CONTENT_BUCKET', 'university-kb-content-251221984842-dev')
URL_REGISTRY_TABLE = os.environ.get('URL_REGISTRY_TABLE', 'url-registry-dev')
CONFIGS_DIR = os.path.join(os.path.dirname(__file__), '..', 'configs')
RESULTS_DIR = os.path.join(os.path.dirname(__file__), '..', 'data')
MAX_LINKS = 500

SKIP_EXTENSIONS = {
    ".zip", ".gz", ".tar", ".rar",
    ".mp4", ".mp3", ".avi", ".mov", ".wmv", ".flv",
    ".jpg", ".jpeg", ".png", ".gif", ".svg", ".ico", ".webp", ".bmp",
    ".css", ".js", ".woff", ".woff2", ".ttf", ".eot",
    ".xls", ".xlsx", ".ppt", ".pptx", ".doc", ".docx",
}


def load_config(university_id):
    config_path = os.path.join(CONFIGS_DIR, f'{university_id}.json')
    with open(config_path) as f:
        return json.load(f)


def _normalize_url(parsed):
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    path = parsed.path
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    query = ""
    if parsed.query:
        params = sorted(parsed.query.split("&"))
        tracking_prefixes = ("utm_", "ref=", "fbclid=", "gclid=", "mc_")
        params = [p for p in params if not any(p.startswith(t) for t in tracking_prefixes)]
        if params:
            query = "?" + "&".join(params)
    return f"{scheme}://{netloc}{path}{query}"


def _is_allowed_domain(hostname, allowed_patterns, exclude_domains):
    hostname = hostname.lower()
    for excl in exclude_domains:
        if hostname == excl.lower():
            return False
    for pattern in allowed_patterns:
        pattern = pattern.lower()
        if pattern.startswith('*.'):
            base = pattern[2:]
            if hostname == base or hostname.endswith('.' + base):
                return True
        elif hostname == pattern:
            return True
    return False


def extract_internal_links(html, source_url, allowed_patterns, exclude_domains):
    href_pattern = re.compile(r"href=[\"']([^\"']+)[\"']", re.IGNORECASE)
    all_internal = set()
    parsed_base = urlparse(source_url)
    normalized_base = _normalize_url(parsed_base)

    for match in href_pattern.finditer(html):
        href = match.group(1).strip()
        if not href or href.startswith(("javascript:", "mailto:", "tel:", "#", "data:")):
            continue
        full_url = urljoin(source_url, href)
        full_url, _ = urldefrag(full_url)
        parsed = urlparse(full_url)
        if parsed.scheme not in ("http", "https"):
            continue
        if any(parsed.path.lower().endswith(ext) for ext in SKIP_EXTENSIONS):
            continue
        hostname = (parsed.hostname or '').lower()
        if not hostname:
            continue
        if not _is_allowed_domain(hostname, allowed_patterns, exclude_domains):
            continue
        normalized = _normalize_url(parsed)
        if normalized != normalized_base:
            all_internal.add(normalized)

    return sorted(all_internal)[:MAX_LINKS]


def list_raw_html_keys(s3, university_id):
    """List all raw HTML S3 keys, cached locally after first fetch."""
    cache_file = os.path.join(RESULTS_DIR, f'html_keys_{university_id}.txt')

    if os.path.exists(cache_file):
        with open(cache_file, 'r') as f:
            keys = [line.strip() for line in f if line.strip()]
        print(f"  (loaded {len(keys)} keys from local cache)")
        return keys

    print("  Fetching from S3 (first time only)...")
    prefix = f"raw-html/{university_id}/"
    paginator = s3.get_paginator('list_objects_v2')
    keys = []
    for page in paginator.paginate(Bucket=BUCKET, Prefix=prefix):
        for obj in page.get('Contents', []):
            key = obj['Key']
            if key.endswith('.html'):
                keys.append(key)

    os.makedirs(RESULTS_DIR, exist_ok=True)
    with open(cache_file, 'w') as f:
        f.write('\n'.join(keys) + '\n')
    print(f"  Cached {len(keys)} keys to {cache_file}")
    return keys


def process_and_write(s3_key, s3_client, dynamo_client, university_id,
                      allowed_patterns, exclude_domains, dry_run):
    """Worker: download HTML → extract links → write DynamoDB.

    All network I/O happens inside the worker thread for full parallelism.
    Returns a dict with results for the main thread to checkpoint locally.
    """
    result = {
        's3_key': s3_key,
        'source_url': None,
        'links': [],
        'dynamo_status': None,   # 'written' | 'skipped' | 'error' | None
        'error': False,
    }

    # 1. Download HTML + extract links
    try:
        resp = s3_client.get_object(Bucket=BUCKET, Key=s3_key)
        html = resp['Body'].read().decode('utf-8', errors='replace')
        meta = resp.get('Metadata', {})
        source_url = meta.get('source-url', '') or meta.get('source_url', '')

        if not source_url:
            result['error'] = True
            return result

        result['source_url'] = source_url
        links = extract_internal_links(html, source_url, allowed_patterns, exclude_domains)
        result['links'] = links
    except Exception:
        result['error'] = True
        return result

    if not links or dry_run:
        return result

    # 2. Write links_to to DynamoDB (conditional, thread-safe client API)
    try:
        dynamo_client.update_item(
            TableName=URL_REGISTRY_TABLE,
            Key={'url': {'S': source_url}},
            UpdateExpression='SET links_to = :lt',
            ConditionExpression='attribute_not_exists(links_to)',
            ExpressionAttributeValues={
                ':lt': {'L': [{'S': link} for link in links]}
            }
        )
        result['dynamo_status'] = 'written'
    except dynamo_client.exceptions.ConditionalCheckFailedException:
        result['dynamo_status'] = 'skipped'
    except Exception:
        result['dynamo_status'] = 'error'

    return result


# ── Checkpoint and results helpers ──

def checkpoint_path(university_id):
    return os.path.join(RESULTS_DIR, f'backfill_checkpoint_{university_id}.txt')


def results_path(university_id):
    return os.path.join(RESULTS_DIR, f'backfill_links_to_{university_id}.jsonl')


def load_checkpoint(university_id):
    """Load set of already-processed S3 keys from checkpoint file."""
    path = checkpoint_path(university_id)
    if not os.path.exists(path):
        return set()
    with open(path, 'r') as f:
        return {line.strip() for line in f if line.strip()}


def append_checkpoint(f_ckpt, s3_key):
    """Append a processed S3 key to the checkpoint file."""
    f_ckpt.write(s3_key + '\n')
    f_ckpt.flush()


def append_result(f_results, source_url, links_to):
    """Append a links_to record to the JSONL results file."""
    record = {"source_url": source_url, "links_to": links_to}
    f_results.write(json.dumps(record, separators=(',', ':')) + '\n')
    f_results.flush()


def main():
    parser = argparse.ArgumentParser(description='Backfill links_to from raw HTML in S3')
    parser.add_argument('--university-id', required=True)
    parser.add_argument('--region', default='us-east-1')
    parser.add_argument('--workers', type=int, default=20, help='Parallel worker threads')
    parser.add_argument('--dry-run', action='store_true', help='Extract only, no writes')
    parser.add_argument('--reset', action='store_true',
                        help='Delete checkpoint and results files, start fresh')
    args = parser.parse_args()

    config = load_config(args.university_id)
    crawl_config = config.get('crawl_config', {})
    allowed_patterns = crawl_config.get('allowed_domain_patterns', [])
    exclude_domains = crawl_config.get('exclude_domains', [])

    if not allowed_patterns:
        print("ERROR: No allowed_domain_patterns in config.")
        sys.exit(1)

    # Ensure data directory exists
    os.makedirs(RESULTS_DIR, exist_ok=True)

    # Handle --reset
    ckpt_file = checkpoint_path(args.university_id)
    res_file = results_path(args.university_id)

    if args.reset:
        for path in (ckpt_file, res_file):
            if os.path.exists(path):
                os.remove(path)
                print(f"  Deleted {path}")
        print("Reset complete.\n")

    print(f"University:       {args.university_id}")
    print(f"Allowed patterns: {allowed_patterns}")
    print(f"Exclude domains:  {exclude_domains}")

    pool_size = args.workers + 5
    boto_config = BotoConfig(max_pool_connections=pool_size)

    # Shared thread-safe clients (boto3 clients are thread-safe, resources are NOT)
    s3 = boto3.client('s3', region_name=args.region, config=boto_config)
    dynamo_client = boto3.client('dynamodb', region_name=args.region,
                                 config=BotoConfig(max_pool_connections=pool_size))

    # ── Step 1: List all raw HTML keys ──
    print(f"\nListing raw HTML files for {args.university_id}...")
    html_keys = list_raw_html_keys(s3, args.university_id)
    print(f"  Found {len(html_keys)} HTML files")

    # ── Step 2: Load checkpoint, filter out already-processed keys ──
    done_keys = load_checkpoint(args.university_id)
    if done_keys:
        print(f"  Checkpoint: {len(done_keys)} already processed")
    remaining_keys = [k for k in html_keys if k not in done_keys]
    print(f"  Remaining:  {len(remaining_keys)} to process")

    if not remaining_keys:
        print("\nAll keys already processed. Use --reset to start fresh.")
        return

    # ── Step 3: Process pages — all network I/O in worker threads ──
    if args.dry_run:
        print(f"\nDry run: extracting links ({args.workers} threads), no writes...")
    else:
        print(f"\nProcessing ({args.workers} threads): extract + DynamoDB in parallel...")

    # Counters (updated only from main thread — no lock needed)
    processed = 0
    pages_with_links = 0
    total_links = 0
    errors = 0
    dynamo_written = 0
    dynamo_skipped = 0
    dynamo_errors = 0
    start = time.time()

    # Local file handles — only main thread writes to these
    f_ckpt = open(ckpt_file, 'a') if not args.dry_run else None
    f_results = open(res_file, 'a') if not args.dry_run else None

    # For dry-run summary
    dry_run_map = {} if args.dry_run else None

    try:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {
                pool.submit(
                    process_and_write, key, s3, dynamo_client,
                    args.university_id, allowed_patterns, exclude_domains,
                    args.dry_run
                ): key
                for key in remaining_keys
            }

            for future in as_completed(futures):
                r = future.result()
                processed += 1

                if r['error']:
                    errors += 1
                    if f_ckpt:
                        append_checkpoint(f_ckpt, r['s3_key'])
                    continue

                links = r['links']
                if links:
                    pages_with_links += 1
                    total_links += len(links)

                if args.dry_run:
                    if links:
                        dry_run_map[r['source_url']] = links
                else:
                    # Tally remote write results (already done in worker)
                    if r['dynamo_status'] == 'written':
                        dynamo_written += 1
                    elif r['dynamo_status'] == 'skipped':
                        dynamo_skipped += 1
                    elif r['dynamo_status'] == 'error':
                        dynamo_errors += 1

                    # Local I/O only — sub-millisecond
                    if links:
                        append_result(f_results, r['source_url'], links)
                    append_checkpoint(f_ckpt, r['s3_key'])

                # Progress reporting
                if processed % 500 == 0:
                    elapsed = time.time() - start
                    rate = processed / elapsed
                    if args.dry_run:
                        print(f"  {processed}/{len(remaining_keys)} ({rate:.0f}/s), "
                              f"{pages_with_links} with links", flush=True)
                    else:
                        print(f"  {processed}/{len(remaining_keys)} ({rate:.0f}/s) — "
                              f"dynamo: {dynamo_written}w/{dynamo_skipped}s/{dynamo_errors}e, "
                              f"links: {total_links}",
                              flush=True)

    except KeyboardInterrupt:
        print(f"\n\nInterrupted! Progress saved to checkpoint.")
        print(f"  Processed so far: {processed}/{len(remaining_keys)}")
        print(f"  Total checkpointed: {len(done_keys) + processed}")
        print(f"  Re-run the same command to resume.")
    finally:
        if f_ckpt:
            f_ckpt.close()
        if f_results:
            f_results.close()

    # ── Summary ──
    elapsed = time.time() - start
    total_checkpointed = len(done_keys) + processed

    print(f"\n{'='*55}")
    print(f"SUMMARY")
    print(f"{'='*55}")
    print(f"  Total HTML files:          {len(html_keys)}")
    print(f"  Previously checkpointed:   {len(done_keys)}")
    print(f"  Processed this run:        {processed}")
    print(f"  Total checkpointed now:    {total_checkpointed}")
    print(f"  Pages with links_to:       {pages_with_links}")
    print(f"  Total outgoing links:      {total_links}")
    print(f"  Avg links per page:        {total_links / max(pages_with_links, 1):.1f}")
    print(f"  Errors (no source_url):    {errors}")
    print(f"  Time:                      {elapsed:.1f}s")

    if not args.dry_run:
        print(f"  ─────────────────────────────")
        print(f"  DynamoDB written:          {dynamo_written}")
        print(f"  DynamoDB skipped (exists): {dynamo_skipped}")
        print(f"  DynamoDB errors:           {dynamo_errors}")
        print(f"  ─────────────────────────────")
        print(f"  Checkpoint: {ckpt_file}")
        print(f"  Results:    {res_file}")

    if args.dry_run and dry_run_map:
        top = sorted(dry_run_map.items(), key=lambda x: -len(x[1]))[:15]
        print(f"\n  Top pages by outgoing links:")
        for url, links in top:
            print(f"    {len(links):>4} → {url[:80]}")
        print(f"\n  Dry run complete. Run without --dry-run to write.")

    print(f"{'='*55}")


if __name__ == '__main__':
    main()
