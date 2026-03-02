#!/usr/bin/env python3
"""
Re-queue crawled URLs for content processing, with filtering:
  1. Domain allowlist - only process pages from the university's own domains
  2. Junk URL patterns - skip search pages, login pages, calendar views, etc.
  3. Content deduplication - one URL per unique content_hash

Usage:
    # Dry run first to see what gets filtered
    python requeue_for_processing.py --university-id gmu --queue-url https://... --dry-run

    # Then run for real
    python requeue_for_processing.py --university-id gmu --queue-url https://...

    # Override allowed domains (default: inferred from university-id)
    python requeue_for_processing.py --university-id gmu --queue-url https://... \
        --allowed-domains gmu.edu www.gmu.edu
"""

import argparse
import json
import os
import re
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

import boto3


# -------------------------------------------------------
# Config loading — reads allowed_domain_patterns from
# university config files instead of hardcoding.
# -------------------------------------------------------
CONFIGS_DIR = os.path.join(os.path.dirname(__file__), '..', 'configs')


def load_allowed_domains_from_config(university_id):
    """Load allowed_domain_patterns from the university config file."""
    config_path = os.path.join(CONFIGS_DIR, f'{university_id}.json')
    try:
        with open(config_path) as f:
            config = json.load(f)
        return config.get('crawl_config', {}).get('allowed_domain_patterns', [])
    except FileNotFoundError:
        return []
    except Exception as e:
        print(f"WARNING: Failed to load config from {config_path}: {e}")
        return []

# URL patterns that indicate non-content pages
JUNK_URL_PATTERNS = [
    # Search and query pages
    r'[?&](q|query|search|s)=',
    r'/search[/?]',
    r'/results[/?]',

    # Calendar and date-filtered views
    r'/calendar[/?].*[?&](month|year|date)=',
    r'/events[/?].*[?&](month|year|date|page)=',
    r'[?&]tribe-bar-date=',
    r'[?&]eventDisplay=',

    # Pagination
    r'[?&]page=\d+',
    r'/page/\d+',

    # Print and alternate views
    r'[?&](print|format)=',
    r'/print/',

    # Login and authentication
    r'/login',
    r'/signin',
    r'/sso/',
    r'/cas/',
    r'/auth/',
    r'/authorize\?',
    r'/oauth2?/',

    # Admin and backend
    r'/wp-admin',
    r'/wp-json',
    r'/feed/?$',
    r'/xmlrpc',
    r'/cgi-bin/',

    # File listings and indexes
    r'[?&]C=[NMSD]',
    r'[?&]O=[AD]',

    # Social sharing and tracking
    r'[?&](utm_|fbclid|gclid)',
    r'[?&]share=',

    # Tag and category archive pages (often thin content)
    r'/tag/[^/]+/?$',
    r'/category/[^/]+/page/',
    r'/author/[^/]+/?$',

    # Duplicate indicators
    r'/index\.html?$',
    r'/default\.aspx?$',

    # HubSpot dynamic pages
    r'[?&]hsLang=',
    r'/form\.resume_url',
    r'/getApply',
    r'/getCategoryLink',
]

JUNK_PATTERNS_COMPILED = [re.compile(p, re.IGNORECASE) for p in JUNK_URL_PATTERNS]


def is_allowed_domain(url, allowed_domains):
    """Check if URL belongs to an allowed domain."""
    try:
        hostname = urlparse(url).hostname
        if not hostname:
            return False
        hostname = hostname.lower()
    except Exception:
        return False

    for domain in allowed_domains:
        domain = domain.lower()
        if domain.startswith('*.'):
            # Wildcard: *.gmu.edu matches anything.gmu.edu and gmu.edu itself
            base = domain[2:]
            if hostname == base or hostname.endswith('.' + base):
                return True
        else:
            if hostname == domain:
                return True

    return False


def is_junk_url(url):
    """Check if URL matches known junk patterns."""
    for pattern in JUNK_PATTERNS_COMPILED:
        if pattern.search(url):
            return True
    return False


def pick_best_url(urls):
    """From URLs with the same content_hash, pick the best one.
    Prefers: shallowest depth, then shortest URL, then alphabetically first.
    """
    return min(urls, key=lambda item: (
        int(item.get('depth', 99)),
        len(item.get('url', '')),
        item.get('url', '')
    ))


def main():
    parser = argparse.ArgumentParser(description='Re-queue crawled URLs for content processing (with filtering)')
    parser.add_argument('--university-id', required=True, help='University ID (e.g., gmu)')
    parser.add_argument('--queue-url', required=True, help='Processing queue URL')
    parser.add_argument('--table', default='url-registry-dev', help='URL registry table name')
    parser.add_argument('--region', default='us-east-1', help='AWS region')
    parser.add_argument('--batch-size', type=int, default=10, help='SQS send batch size')
    parser.add_argument('--allowed-domains', nargs='+', default=None,
                        help='Override allowed domains (default: from UNIVERSITY_DOMAINS config)')
    parser.add_argument('--send-threads', type=int, default=20, help='Parallel threads for SQS sending')
    parser.add_argument('--dry-run', action='store_true', help='Show filtering stats without sending messages')
    args = parser.parse_args()

    # Resolve allowed domains from config file or CLI override
    if args.allowed_domains:
        allowed_domains = args.allowed_domains
    else:
        allowed_domains = load_allowed_domains_from_config(args.university_id)
    if not allowed_domains:
        print(f"ERROR: No domain allowlist for '{args.university_id}'.")
        print(f"Either add allowed_domain_patterns to configs/{args.university_id}.json or pass --allowed-domains.")
        sys.exit(1)

    print(f"Allowed domains: {', '.join(allowed_domains)}")

    dynamodb = boto3.resource('dynamodb', region_name=args.region)
    sqs = boto3.client('sqs', region_name=args.region)
    table = dynamodb.Table(args.table)

    # -------------------------------------------------------
    # 1. Fetch all crawled URLs from DynamoDB
    # -------------------------------------------------------
    print(f"\nQuerying {args.table} for university_id={args.university_id}, crawl_status=crawled...")

    all_items = []
    query_kwargs = {
        'IndexName': 'university-status-index',
        'KeyConditionExpression': 'university_id = :uid AND crawl_status = :status',
        'ExpressionAttributeValues': {
            ':uid': args.university_id,
            ':status': 'crawled'
        },
        'ProjectionExpression': '#u, university_id, #dom, s3_key, content_type, url_hash, #d, content_hash, crawled_at, last_crawled_at',
        'ExpressionAttributeNames': {
            '#u': 'url',
            '#d': 'depth',
            '#dom': 'domain'
        }
    }

    while True:
        response = table.query(**query_kwargs)
        all_items.extend(response.get('Items', []))
        sys.stdout.write(f"\r  Fetched {len(all_items):,} items...")
        sys.stdout.flush()
        if 'LastEvaluatedKey' not in response:
            break
        query_kwargs['ExclusiveStartKey'] = response['LastEvaluatedKey']

    print(f"\rTotal crawled URLs: {len(all_items):,}      ")

    # -------------------------------------------------------
    # 2. Filter by domain allowlist
    # -------------------------------------------------------
    on_domain = []
    off_domain = defaultdict(list)

    for item in all_items:
        url = item.get('url', '')
        if is_allowed_domain(url, allowed_domains):
            on_domain.append(item)
        else:
            hostname = urlparse(url).hostname or 'unknown'
            off_domain[hostname].append(url)

    off_domain_count = sum(len(urls) for urls in off_domain.values())
    print(f"\nDomain filter:")
    print(f"  On-domain:  {len(on_domain)}")
    print(f"  Off-domain: {off_domain_count}")

    if off_domain and args.dry_run:
        print(f"\n  Off-domain breakdown:")
        for domain, urls in sorted(off_domain.items(), key=lambda x: -len(x[1])):
            print(f"    {domain}: {len(urls)} URLs")
            if len(urls) <= 3:
                for url in urls:
                    print(f"      {url}")

    # -------------------------------------------------------
    # 3. Separate HTML and PDF
    # -------------------------------------------------------
    html_items = []
    pdf_items = []
    for item in on_domain:
        s3_key = item.get('s3_key', '')
        if s3_key.startswith('raw-pdf/') or item.get('content_type') == 'application/pdf':
            pdf_items.append(item)
        else:
            html_items.append(item)

    print(f"\nContent types (on-domain only):")
    print(f"  HTML pages: {len(html_items)}")
    print(f"  PDF files:  {len(pdf_items)}")

    # -------------------------------------------------------
    # 4. Filter junk URLs (HTML only)
    # -------------------------------------------------------
    clean_html = []
    junk_urls = []
    for item in html_items:
        url = item.get('url', '')
        if is_junk_url(url):
            junk_urls.append(url)
        else:
            clean_html.append(item)

    print(f"\nJunk URL filter:")
    print(f"  Kept:    {len(clean_html)} HTML pages")
    print(f"  Removed: {len(junk_urls)} junk URLs")

    if junk_urls and args.dry_run:
        print(f"\n  Sample junk URLs:")
        for url in sorted(junk_urls)[:15]:
            print(f"    {url}")
        if len(junk_urls) > 15:
            print(f"    ... and {len(junk_urls) - 15} more")

    # -------------------------------------------------------
    # 5. Deduplicate by content_hash
    # -------------------------------------------------------
    hash_groups = defaultdict(list)
    no_hash = []

    for item in clean_html:
        content_hash = item.get('content_hash', '')
        if content_hash:
            hash_groups[content_hash].append(item)
        else:
            no_hash.append(item)

    deduped_html = []
    duplicate_urls = []

    for content_hash, group in hash_groups.items():
        best = pick_best_url(group)
        deduped_html.append(best)
        for item in group:
            if item.get('url') != best.get('url'):
                duplicate_urls.append(item.get('url', ''))

    deduped_html.extend(no_hash)

    print(f"\nContent deduplication:")
    print(f"  Unique content hashes: {len(hash_groups)}")
    print(f"  Duplicate URLs removed: {len(duplicate_urls)}")
    print(f"  Pages without hash:     {len(no_hash)}")
    print(f"  Final HTML count:       {len(deduped_html)}")

    if duplicate_urls and args.dry_run:
        print(f"\n  Sample duplicates removed:")
        for url in sorted(duplicate_urls)[:15]:
            print(f"    {url}")
        if len(duplicate_urls) > 15:
            print(f"    ... and {len(duplicate_urls) - 15} more")

    # -------------------------------------------------------
    # 6. Summary
    # -------------------------------------------------------
    total_to_queue = len(deduped_html) + len(pdf_items)
    total_filtered = len(all_items) - total_to_queue

    print(f"\n{'='*50}")
    print(f"SUMMARY")
    print(f"{'='*50}")
    print(f"  Total crawled:       {len(all_items)}")
    print(f"  Off-domain removed:  {off_domain_count}")
    print(f"  Junk URLs removed:   {len(junk_urls)}")
    print(f"  Duplicates removed:  {len(duplicate_urls)}")
    print(f"  Total filtered out:  {total_filtered}")
    print(f"  ─────────────────────────────")
    print(f"  HTML to process:     {len(deduped_html)}")
    print(f"  PDFs to process:     {len(pdf_items)}")
    print(f"  Total to queue:      {total_to_queue}")
    print(f"{'='*50}")

    if args.dry_run:
        print("\nDry run complete. Run without --dry-run to send messages.")
        return

    # -------------------------------------------------------
    # 7. Send to processing queue
    # -------------------------------------------------------
    all_to_queue = []

    for item in deduped_html:
        all_to_queue.append({
            'url': item.get('url', ''),
            'university_id': item.get('university_id', ''),
            'domain': item.get('domain', ''),
            's3_key': item.get('s3_key', ''),
            'content_type': 'text/html',
            'url_hash': item.get('url_hash', ''),
            'depth': int(item.get('depth', 0)),
            'crawled_at': item.get('crawled_at', item.get('last_crawled_at', '')),
            'action': 'process'
        })

    for item in pdf_items:
        all_to_queue.append({
            'url': item.get('url', ''),
            'university_id': item.get('university_id', ''),
            'domain': item.get('domain', ''),
            's3_key': item.get('s3_key', ''),
            'content_type': 'application/pdf',
            'url_hash': item.get('url_hash', ''),
            'depth': int(item.get('depth', 0)),
            'crawled_at': item.get('crawled_at', item.get('last_crawled_at', '')),
            'action': 'process'
        })

    # Build all SQS batches (10 messages each, SQS max)
    batches = []
    batch = []
    for msg in all_to_queue:
        batch.append({
            'Id': str(len(batch)),
            'MessageBody': json.dumps(msg)
        })
        if len(batch) >= args.batch_size:
            batches.append(batch)
            batch = []
    if batch:
        batches.append(batch)

    print(f"Sending {len(batches)} batches with {args.send_threads} threads...")

    sent = 0
    errors = 0
    start_time = time.time()

    def send_batch(entries):
        try:
            response = sqs.send_message_batch(
                QueueUrl=args.queue_url,
                Entries=entries
            )
            return len(response.get('Successful', [])), len(response.get('Failed', []))
        except Exception as e:
            return 0, len(entries)

    with ThreadPoolExecutor(max_workers=args.send_threads) as pool:
        futures = [pool.submit(send_batch, b) for b in batches]
        for f in as_completed(futures):
            ok, fail = f.result()
            sent += ok
            errors += fail
            if sent % 1000 < 10:
                elapsed = time.time() - start_time
                rate = sent / elapsed if elapsed > 0 else 0
                print(f"  Sent {sent}/{total_to_queue} ({rate:.0f}/s)...")

    print(f"\nDone: {sent} sent, {errors} errors")


if __name__ == '__main__':
    main()