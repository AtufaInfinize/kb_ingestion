#!/usr/bin/env python3
"""
Trim .metadata.json sidecar files to fit Bedrock's 1024-byte limit.

Keeps:
  - Scalar fields: source_url, title, category, subcategory, domain,
    university_id, depth, is_useful_page, is_high_traffic_page
  - summary, last_updated, content_length
  - facts: converted from object list to string list, trimmed to 2-3

Drops:
  - related_urls (stored in DynamoDB instead)

If still over 1024B after trimming, progressively reduces facts count.

Usage:
    # Dry run — count how many are over/under limit
    python3 scripts/trim_metadata.py --university-id gmu --dry-run

    # Trim all sidecars
    python3 scripts/trim_metadata.py --university-id gmu --workers 20

    # Resume after interruption
    python3 scripts/trim_metadata.py --university-id gmu --workers 20
"""

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import boto3
from botocore.config import Config as BotoConfig

BUCKET = os.environ.get('CONTENT_BUCKET', 'university-kb-content-251221984842-dev')
RESULTS_DIR = os.path.join(os.path.dirname(__file__), '..', 'data')
BEDROCK_LIMIT = 1024
MAX_FACTS = 3

# Fields to always keep as-is (all scalars)
SCALAR_FIELDS = {
    'source_url',
    'title',
    'category',
    'subcategory',
    'domain',
    'university_id',
    'depth',
    'is_useful_page',
    'is_high_traffic_page',
    'summary',
    'last_updated',
    'content_length',
}


def fact_to_string(fact):
    """Convert a fact object {type, key, value} to a single string."""
    if isinstance(fact, str):
        return fact
    if isinstance(fact, dict):
        return fact.get('value', str(fact))
    return str(fact)


def build_trimmed(attrs):
    """Build trimmed metadata attributes that fit under BEDROCK_LIMIT.

    Strategy:
    1. Keep all scalar fields
    2. Convert facts to string list, keep up to MAX_FACTS
    3. Drop related_urls
    4. If still over limit, progressively reduce facts, then truncate summary
    """
    trimmed = {}
    for key in SCALAR_FIELDS:
        if key in attrs:
            trimmed[key] = attrs[key]

    # Convert facts from object list to string list
    raw_facts = attrs.get('facts', [])
    if raw_facts and isinstance(raw_facts, list):
        fact_strings = [fact_to_string(f) for f in raw_facts[:MAX_FACTS]]
        trimmed['facts'] = fact_strings

    # Check size and progressively trim if over limit
    result = {"metadataAttributes": trimmed}
    encoded = json.dumps(result, separators=(',', ':'))

    if len(encoded) <= BEDROCK_LIMIT:
        return trimmed, len(encoded)

    # Reduce facts one at a time
    while 'facts' in trimmed and trimmed['facts']:
        trimmed['facts'] = trimmed['facts'][:-1]
        if not trimmed['facts']:
            del trimmed['facts']
        encoded = json.dumps({"metadataAttributes": trimmed}, separators=(',', ':'))
        if len(encoded) <= BEDROCK_LIMIT:
            return trimmed, len(encoded)

    # Truncate summary as last resort
    if 'summary' in trimmed and len(trimmed['summary']) > 50:
        while len(trimmed['summary']) > 50:
            trimmed['summary'] = trimmed['summary'][:len(trimmed['summary']) // 2]
            encoded = json.dumps({"metadataAttributes": trimmed}, separators=(',', ':'))
            if len(encoded) <= BEDROCK_LIMIT:
                return trimmed, len(encoded)

    # Final fallback — drop summary entirely
    trimmed.pop('summary', None)
    encoded = json.dumps({"metadataAttributes": trimmed}, separators=(',', ':'))
    return trimmed, len(encoded)


def list_sidecar_keys(s3, university_id):
    """List all .metadata.json keys, cached locally after first fetch."""
    cache_file = os.path.join(RESULTS_DIR, f'sidecar_keys_{university_id}.txt')

    if os.path.exists(cache_file):
        with open(cache_file, 'r') as f:
            keys = [line.strip() for line in f if line.strip()]
        print(f"  (loaded {len(keys)} keys from local cache)")
        return keys

    print("  Fetching from S3 (first time only)...")
    prefix = f"clean-content/{university_id}/"
    paginator = s3.get_paginator('list_objects_v2')
    keys = []
    for page in paginator.paginate(Bucket=BUCKET, Prefix=prefix):
        for obj in page.get('Contents', []):
            key = obj['Key']
            if key.endswith('.metadata.json'):
                keys.append(key)

    with open(cache_file, 'w') as f:
        f.write('\n'.join(keys) + '\n')
    print(f"  Cached {len(keys)} keys to {cache_file}")
    return keys


def trim_one_sidecar(s3_key, s3_client, dry_run):
    """Download a sidecar, trim fields, re-upload if changed."""
    result = {
        's3_key': s3_key,
        'original_size': 0,
        'trimmed_size': 0,
        'was_over': False,
        'updated': False,
        'error': False,
    }

    try:
        resp = s3_client.get_object(Bucket=BUCKET, Key=s3_key)
        raw = resp['Body'].read()
        result['original_size'] = len(raw)
        result['was_over'] = len(raw) > BEDROCK_LIMIT

        metadata = json.loads(raw.decode('utf-8'))
        attrs = metadata.get('metadataAttributes', {})

        trimmed, trimmed_size = build_trimmed(attrs)
        result['trimmed_size'] = trimmed_size

        if dry_run:
            return result

        if trimmed != attrs:
            trimmed_json = json.dumps({"metadataAttributes": trimmed}, separators=(',', ':'))
            s3_client.put_object(
                Bucket=BUCKET,
                Key=s3_key,
                Body=trimmed_json.encode('utf-8'),
                ContentType='application/json'
            )
            result['updated'] = True

        return result
    except Exception:
        result['error'] = True
        return result


def checkpoint_path(university_id):
    return os.path.join(RESULTS_DIR, f'trim_metadata_checkpoint_{university_id}.txt')


def load_checkpoint(university_id):
    path = checkpoint_path(university_id)
    if not os.path.exists(path):
        return set()
    with open(path, 'r') as f:
        return {line.strip() for line in f if line.strip()}


def main():
    parser = argparse.ArgumentParser(description='Trim metadata sidecars for Bedrock 1024B limit')
    parser.add_argument('--university-id', required=True)
    parser.add_argument('--region', default='us-east-1')
    parser.add_argument('--workers', type=int, default=20)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--reset', action='store_true')
    args = parser.parse_args()

    os.makedirs(RESULTS_DIR, exist_ok=True)

    ckpt_file = checkpoint_path(args.university_id)
    if args.reset and os.path.exists(ckpt_file):
        os.remove(ckpt_file)
        print(f"  Deleted {ckpt_file}\n")

    pool_size = args.workers + 5
    boto_config = BotoConfig(max_pool_connections=pool_size)
    s3 = boto3.client('s3', region_name=args.region, config=boto_config)

    print(f"Listing sidecar files for {args.university_id}...")
    all_keys = list_sidecar_keys(s3, args.university_id)
    print(f"  Found {len(all_keys)} sidecar files")

    done_keys = load_checkpoint(args.university_id)
    if done_keys:
        print(f"  Checkpoint: {len(done_keys)} already processed")
    remaining = [k for k in all_keys if k not in done_keys]
    print(f"  Remaining:  {len(remaining)} to process")

    if not remaining:
        print("\nAll done. Use --reset to reprocess.")
        return

    mode = "Dry run" if args.dry_run else "Trimming"
    print(f"\n{mode} ({args.workers} threads)...")

    processed = 0
    was_over = 0
    now_under = 0
    still_over = 0
    updated = 0
    errors = 0
    start = time.time()

    f_ckpt = open(ckpt_file, 'a') if not args.dry_run else None

    try:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {
                pool.submit(trim_one_sidecar, key, s3, args.dry_run): key
                for key in remaining
            }

            for future in as_completed(futures):
                r = future.result()
                processed += 1

                if r['error']:
                    errors += 1
                elif r['was_over']:
                    was_over += 1
                    if r['trimmed_size'] <= BEDROCK_LIMIT:
                        now_under += 1
                    else:
                        still_over += 1

                if r['updated']:
                    updated += 1

                if not args.dry_run and f_ckpt:
                    f_ckpt.write(r['s3_key'] + '\n')
                    f_ckpt.flush()

                if processed % 500 == 0:
                    elapsed = time.time() - start
                    rate = processed / elapsed
                    print(f"  {processed}/{len(remaining)} ({rate:.0f}/s) — "
                          f"over: {was_over}, fixed: {now_under}, "
                          f"still_over: {still_over}, updated: {updated}",
                          flush=True)

    except KeyboardInterrupt:
        print(f"\n\nInterrupted at {processed}/{len(remaining)}. Re-run to resume.")
    finally:
        if f_ckpt:
            f_ckpt.close()

    elapsed = time.time() - start
    print(f"\n{'='*55}")
    print(f"SUMMARY")
    print(f"{'='*55}")
    print(f"  Total sidecars:       {len(all_keys)}")
    print(f"  Processed this run:   {processed}")
    print(f"  Were over 1024B:      {was_over}")
    print(f"  Now under 1024B:      {now_under}")
    print(f"  Still over 1024B:     {still_over}")
    print(f"  Updated in S3:        {updated}")
    print(f"  Errors:               {errors}")
    print(f"  Time:                 {elapsed:.1f}s")
    print(f"{'='*55}")


if __name__ == '__main__':
    main()
