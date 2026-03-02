#!/usr/bin/env python3
"""
Delete S3 objects in raw-html/ that belong to off-domain URLs.

Loads the university config to get allowed_domain_patterns and exclude_domains,
lists all objects under raw-html/{university_id}/, and deletes those whose
domain folder doesn't match the allowed patterns.

Dry-run by default. Pass --delete to actually remove objects.

Usage:
    python scripts/cleanup_off_domain.py --university-id phc
    python scripts/cleanup_off_domain.py --university-id phc --delete
    python scripts/cleanup_off_domain.py --university-id gmu --prefix clean-content
"""

import argparse
import json
import os
import sys
from fnmatch import fnmatch

import boto3

BUCKET = os.environ.get('CONTENT_BUCKET', 'university-kb-content-251221984842-dev')
CONFIG_PREFIX = 'configs'


def load_config(s3, university_id):
    """Load university config from S3 or local configs/ directory."""
    # Try local file first
    local_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'configs', f'{university_id}.json'
    )
    if os.path.exists(local_path):
        with open(local_path) as f:
            return json.load(f)

    # Fall back to S3
    key = f"{CONFIG_PREFIX}/{university_id}.json"
    obj = s3.get_object(Bucket=BUCKET, Key=key)
    return json.loads(obj['Body'].read().decode('utf-8'))


def is_allowed_domain(hostname, allowed_patterns, exclude_domains):
    """Check if a hostname matches the allowed domain patterns."""
    hostname = hostname.lower()

    # Check exclude list first
    for excl in exclude_domains:
        if hostname == excl.lower():
            return False

    # Check allowed patterns (supports *.example.edu wildcards)
    for pattern in allowed_patterns:
        pattern = pattern.lower()
        if pattern.startswith('*.'):
            base = pattern[2:]
            if hostname == base or hostname.endswith('.' + base):
                return True
        else:
            if hostname == pattern:
                return True

    return False


def list_off_domain_keys(s3, university_id, allowed_patterns, exclude_domains, prefix):
    """List all S3 keys under prefix/{university_id}/ that are off-domain."""
    s3_prefix = f"{prefix}/{university_id}/"
    paginator = s3.get_paginator('list_objects_v2')

    on_domain_count = 0
    off_domain_keys = []
    domain_counts = {}  # track how many objects per off-domain

    for page in paginator.paginate(Bucket=BUCKET, Prefix=s3_prefix):
        for obj in page.get('Contents', []):
            key = obj['Key']
            # Key format: {prefix}/{university_id}/{domain}/{hash}.ext
            parts = key[len(s3_prefix):].split('/', 1)
            if len(parts) < 2:
                continue

            domain = parts[0]
            if is_allowed_domain(domain, allowed_patterns, exclude_domains):
                on_domain_count += 1
            else:
                off_domain_keys.append(key)
                domain_counts[domain] = domain_counts.get(domain, 0) + 1

    return off_domain_keys, on_domain_count, domain_counts


def delete_keys(s3, keys):
    """Delete S3 objects in batches of 1000."""
    deleted = 0
    for i in range(0, len(keys), 1000):
        batch = keys[i:i + 1000]
        resp = s3.delete_objects(
            Bucket=BUCKET,
            Delete={'Objects': [{'Key': k} for k in batch], 'Quiet': True}
        )
        errors = resp.get('Errors', [])
        deleted += len(batch) - len(errors)
        for err in errors:
            print(f"  ERROR deleting {err['Key']}: {err['Message']}", flush=True)
    return deleted


def main():
    parser = argparse.ArgumentParser(description='Delete off-domain S3 objects')
    parser.add_argument('--university-id', required=True)
    parser.add_argument('--region', default='us-east-1')
    parser.add_argument('--prefix', default='raw-html',
                        help='S3 prefix to scan (default: raw-html)')
    parser.add_argument('--delete', action='store_true',
                        help='Actually delete (default is dry-run)')
    args = parser.parse_args()

    s3 = boto3.client('s3', region_name=args.region)

    # Load config
    print(f"Loading config for '{args.university_id}'...", flush=True)
    config = load_config(s3, args.university_id)
    crawl_config = config.get('crawl_config', {})
    allowed_patterns = crawl_config.get('allowed_domain_patterns', [])
    exclude_domains = crawl_config.get('exclude_domains', [])

    if not allowed_patterns:
        print("ERROR: No allowed_domain_patterns found in config. Aborting.", flush=True)
        sys.exit(1)

    print(f"  Allowed patterns: {allowed_patterns}", flush=True)
    print(f"  Exclude domains:  {exclude_domains}", flush=True)

    # Scan S3
    print(f"\nScanning s3://{BUCKET}/{args.prefix}/{args.university_id}/...", flush=True)
    off_domain_keys, on_domain_count, domain_counts = list_off_domain_keys(
        s3, args.university_id, allowed_patterns, exclude_domains, args.prefix
    )

    print(f"\n  On-domain objects:  {on_domain_count}", flush=True)
    print(f"  Off-domain objects: {len(off_domain_keys)}", flush=True)

    if domain_counts:
        print(f"\n  Off-domain breakdown:", flush=True)
        for domain, count in sorted(domain_counts.items(), key=lambda x: -x[1]):
            print(f"    {domain}: {count} objects", flush=True)

    if not off_domain_keys:
        print("\nNo off-domain objects found. Nothing to do.", flush=True)
        return

    # Show sample keys
    print(f"\n  Sample off-domain keys:", flush=True)
    for key in off_domain_keys[:10]:
        print(f"    {key}", flush=True)
    if len(off_domain_keys) > 10:
        print(f"    ... and {len(off_domain_keys) - 10} more", flush=True)

    if not args.delete:
        print(f"\n  DRY RUN — pass --delete to remove {len(off_domain_keys)} objects.", flush=True)
        return

    # Delete
    print(f"\n  Deleting {len(off_domain_keys)} off-domain objects...", flush=True)
    deleted = delete_keys(s3, off_domain_keys)
    print(f"  Done. Deleted {deleted} objects.", flush=True)


if __name__ == '__main__':
    main()
