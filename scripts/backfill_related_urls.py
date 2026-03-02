#!/usr/bin/env python3
"""
Backfill links_to and linked_from attributes for already-crawled URLs.

Reads raw HTML from S3, extracts internal links using the same normalization
logic as the crawler, and writes:
  - links_to (outgoing links) on each source URL's DynamoDB item
  - linked_from (incoming backlinks) on each target URL's DynamoDB item

Usage:
    # Dry run — see stats, no DynamoDB writes
    python scripts/backfill_related_urls.py --university-id phc --dry-run

    # Run for real
    python scripts/backfill_related_urls.py --university-id phc

    # Limit to one domain
    python scripts/backfill_related_urls.py --university-id phc --domain www.phc.edu
"""

import argparse
import json
import os
import re
import statistics
import sys
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse, urljoin, urldefrag

import boto3
from botocore.exceptions import ClientError

# -------------------------------------------------------
# Constants
# -------------------------------------------------------
MAX_LINKS_TO = 500
MAX_LINKED_FROM = 100

CONTENT_BUCKET = os.environ.get("CONTENT_BUCKET", "university-kb-content-251221984842-dev")

SKIP_EXTENSIONS = {
    ".zip", ".gz", ".tar", ".rar",
    ".mp4", ".mp3", ".avi", ".mov", ".wmv", ".flv",
    ".jpg", ".jpeg", ".png", ".gif", ".svg", ".ico", ".webp", ".bmp",
    ".css", ".js", ".woff", ".woff2", ".ttf", ".eot",
    ".xls", ".xlsx", ".ppt", ".pptx", ".doc", ".docx",
}

# -------------------------------------------------------
# Config loading
# -------------------------------------------------------
CONFIGS_DIR = os.path.join(os.path.dirname(__file__), '..', 'configs')


def load_config(university_id):
    config_path = os.path.join(CONFIGS_DIR, f'{university_id}.json')
    with open(config_path) as f:
        return json.load(f)


def get_allowed_domains(config):
    return config.get('crawl_config', {}).get('allowed_domain_patterns', [])


def get_exclude_domains(config):
    return config.get('crawl_config', {}).get('exclude_domains', [])


# -------------------------------------------------------
# URL helpers (mirrored from crawler-worker/handler.py)
# -------------------------------------------------------
def normalize_url(parsed):
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


def get_root_domain(netloc):
    parts = netloc.lower().split(".")
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return netloc


def is_allowed_domain(url, allowed_domains, exclude_domains):
    try:
        hostname = urlparse(url).hostname
        if not hostname:
            return False
        hostname = hostname.lower()
    except Exception:
        return False

    for domain in exclude_domains:
        domain = domain.lower()
        if hostname == domain or hostname.endswith('.' + domain):
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


def extract_links_from_html(html, source_url, allowed_domains, exclude_domains):
    """
    Extract all internal links from HTML. Returns list of normalized URLs
    (the full relationship set, before crawl filtering).
    """
    href_pattern = re.compile(r'href=["\']([^"\']+)["\']', re.IGNORECASE)
    all_internal = set()

    parsed_base = urlparse(source_url)
    root_domain = get_root_domain(parsed_base.netloc)
    normalized_base = normalize_url(parsed_base)

    for match in href_pattern.finditer(html):
        href = match.group(1).strip()
        if not href or href.startswith(("javascript:", "mailto:", "tel:", "#", "data:")):
            continue

        full_url = urljoin(source_url, href)
        full_url, _ = urldefrag(full_url)
        parsed = urlparse(full_url)

        if parsed.scheme not in ("http", "https"):
            continue

        if allowed_domains:
            if not is_allowed_domain(full_url, allowed_domains, exclude_domains):
                continue
        else:
            if get_root_domain(parsed.netloc) != root_domain:
                continue

        normalized = normalize_url(parsed)
        if normalized == normalized_base:
            continue

        all_internal.add(normalized)

    return list(all_internal)


# -------------------------------------------------------
# Main
# -------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description='Backfill links_to and linked_from attributes')
    parser.add_argument('--university-id', required=True, help='University ID (e.g., phc)')
    parser.add_argument('--table', default='url-registry-dev', help='URL registry table name')
    parser.add_argument('--bucket', default=None, help=f'S3 bucket (default: {CONTENT_BUCKET})')
    parser.add_argument('--region', default='us-east-1', help='AWS region')
    parser.add_argument('--domain', default=None, help='Limit to a single domain')
    parser.add_argument('--workers', type=int, default=20, help='Thread pool size for parallel S3/DynamoDB ops (default: 20)')
    parser.add_argument('--dry-run', action='store_true', help='Show stats without writing to DynamoDB')
    args = parser.parse_args()

    bucket = args.bucket or CONTENT_BUCKET
    university_id = args.university_id

    config = load_config(university_id)
    allowed_domains = get_allowed_domains(config)
    exclude_domains = get_exclude_domains(config)

    workers = args.workers

    # ── Step 1: Load crawled URLs from DynamoDB (parallel segment scan) ──
    num_segments = workers
    print(f"Step 1: Loading crawled URLs for '{university_id}' from {args.table} "
          f"({num_segments} parallel segments)...", flush=True)

    def scan_segment(segment):
        """Scan one segment of the table, filtering for this university's crawled URLs.
        Returns (backfill_map, all_urls):
          - backfill_map: {url: s3_key} for URLs missing links_to
          - all_urls: set of all crawled URLs (for linked_from validation)"""
        tl_table = boto3.resource('dynamodb', region_name=args.region).Table(args.table)
        backfill_map = {}
        all_urls = set()
        scan_kwargs = {
            'FilterExpression': 'university_id = :uid AND crawl_status = :s AND content_type = :html',
            'ExpressionAttributeValues': {':uid': university_id, ':s': 'crawled', ':html': 'html'},
            'ProjectionExpression': '#u, links_to, s3_key',
            'ExpressionAttributeNames': {'#u': 'url'},
            'TotalSegments': num_segments,
            'Segment': segment,
        }
        while True:
            resp = tl_table.scan(**scan_kwargs)
            for item in resp.get('Items', []):
                url = item['url']
                all_urls.add(url)
                if 'links_to' not in item and item.get('s3_key'):
                    backfill_map[url] = item['s3_key']
            if 'LastEvaluatedKey' not in resp:
                break
            scan_kwargs['ExclusiveStartKey'] = resp['LastEvaluatedKey']
        return backfill_map, all_urls

    known_urls = set()       # all crawled URLs (for linked_from target validation)
    backfill_map = {}        # {url: s3_key} for URLs needing backfill
    with ThreadPoolExecutor(max_workers=num_segments) as pool:
        futures = {pool.submit(scan_segment, i): i for i in range(num_segments)}
        for future in as_completed(futures):
            seg = futures[future]
            seg_map, seg_all = future.result()
            backfill_map.update(seg_map)
            known_urls.update(seg_all)
            print(f"  Segment {seg}: {len(seg_all)} URLs ({len(seg_map)} need backfill)", flush=True)

    already_done = len(known_urls) - len(backfill_map)
    print(f"  Done: {len(known_urls)} crawled URLs, "
          f"{already_done} already backfilled, {len(backfill_map)} remaining", flush=True)

    if not backfill_map and not args.dry_run:
        print("All URLs already backfilled. Nothing to do.", flush=True)
        return

    # ── Step 2: Download HTML and extract links (parallel S3 reads) ──
    total = len(backfill_map)
    print(f"Step 2: Processing {total} HTML files ({workers} threads)...", flush=True)
    outgoing = {}   # source_url -> list of related URLs
    incoming = defaultdict(set)  # target_url -> set of source URLs
    lock = threading.Lock()

    processed = 0
    errors = 0

    def process_one(source_url, s3_key):
        """Download HTML from S3, extract links. Returns (source_url, related_urls)."""
        tl_s3 = boto3.client('s3', region_name=args.region)
        obj = tl_s3.get_object(Bucket=bucket, Key=s3_key)
        html = obj['Body'].read().decode('utf-8', errors='replace')
        related_urls = extract_links_from_html(html, source_url, allowed_domains, exclude_domains)
        return (source_url, related_urls)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(process_one, url, key): url for url, key in backfill_map.items()}
        for future in as_completed(futures):
            source_url = futures[future]
            try:
                source_url, related_urls = future.result()
                with lock:
                    outgoing[source_url] = related_urls[:MAX_LINKS_TO]
                    for target_url in related_urls:
                        if target_url in known_urls:
                            incoming[target_url].add(source_url)
                    processed += 1
                    if processed % 100 == 0:
                        print(f"  Processed {processed}/{total}...", flush=True)

            except Exception as e:
                print(f"  ERROR processing {source_url}: {e}", flush=True)
                errors += 1

    print(f"  Done: {processed} processed, {errors} errors", flush=True)

    # ── Step 4: Statistics ──
    print(f"\n{'='*60}")
    print("STATISTICS")
    print(f"{'='*60}")

    outgoing_counts = [len(urls) for urls in outgoing.values()]
    if outgoing_counts:
        print(f"\nOutgoing links (links_to) per page:")
        print(f"  Pages with links: {len(outgoing_counts)}")
        print(f"  Min:    {min(outgoing_counts)}")
        print(f"  Max:    {max(outgoing_counts)}")
        print(f"  Avg:    {statistics.mean(outgoing_counts):.1f}")
        print(f"  Median: {statistics.median(outgoing_counts):.1f}")

    incoming_counts = [len(urls) for urls in incoming.values()]
    if incoming_counts:
        print(f"\nIncoming links (linked_from) per page:")
        print(f"  Pages with backlinks: {len(incoming_counts)}")
        print(f"  Min:    {min(incoming_counts)}")
        print(f"  Max:    {max(incoming_counts)}")
        print(f"  Avg:    {statistics.mean(incoming_counts):.1f}")
        print(f"  Median: {statistics.median(incoming_counts):.1f}")

        # Top 10 most linked-to pages
        print(f"\nTop 10 most linked-to pages:")
        sorted_incoming = sorted(incoming.items(), key=lambda x: len(x[1]), reverse=True)
        for url, sources in sorted_incoming[:10]:
            print(f"  {len(sources):4d} ← {url}")

    if args.dry_run:
        print(f"\n[DRY RUN] Would write links_to for {len(outgoing)} URLs")
        print(f"[DRY RUN] Would write linked_from for {len(incoming)} URLs")
        return

    # ── Step 5: Write links_to (parallel) ──
    print(f"\nStep 5: Writing links_to for {len(outgoing)} URLs ({workers} threads)...")
    written_out = 0
    errors_out = 0

    def write_links_to(source_url, links):
        tl_table = boto3.resource('dynamodb', region_name=args.region).Table(args.table)
        tl_table.update_item(
            Key={'url': source_url},
            UpdateExpression='SET links_to = :lt',
            ConditionExpression='attribute_exists(#u) AND attribute_not_exists(links_to)',
            ExpressionAttributeNames={'#u': 'url'},
            ExpressionAttributeValues={':lt': links},
        )

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(write_links_to, src, links): src for src, links in outgoing.items()}
        for future in as_completed(futures):
            src = futures[future]
            try:
                future.result()
                written_out += 1
                if written_out % 100 == 0:
                    print(f"  Written links_to: {written_out}/{len(outgoing)}")
            except ClientError as e:
                if e.response['Error']['Code'] != 'ConditionalCheckFailedException':
                    errors_out += 1
                    print(f"  ERROR writing links_to for {src}: {e}")
            except Exception as e:
                errors_out += 1
                print(f"  ERROR writing links_to for {src}: {e}")

    print(f"  Done: {written_out} written, {errors_out} errors")

    # ── Step 6: Write linked_from (parallel) ──
    print(f"\nStep 6: Writing linked_from for {len(incoming)} URLs ({workers} threads)...")
    written_in = 0
    errors_in = 0

    def write_linked_from(target_url, backlinks):
        tl_table = boto3.resource('dynamodb', region_name=args.region).Table(args.table)
        tl_table.update_item(
            Key={'url': target_url},
            UpdateExpression='SET linked_from = :lf',
            ConditionExpression='attribute_exists(#u) AND attribute_not_exists(linked_from)',
            ExpressionAttributeNames={'#u': 'url'},
            ExpressionAttributeValues={':lf': backlinks},
        )

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {}
        for target_url, sources in incoming.items():
            backlinks = sorted(sources)[:MAX_LINKED_FROM]
            futures[pool.submit(write_linked_from, target_url, backlinks)] = target_url
        for future in as_completed(futures):
            tgt = futures[future]
            try:
                future.result()
                written_in += 1
                if written_in % 100 == 0:
                    print(f"  Written linked_from: {written_in}/{len(incoming)}")
            except ClientError as e:
                if e.response['Error']['Code'] != 'ConditionalCheckFailedException':
                    errors_in += 1
                    print(f"  ERROR writing linked_from for {tgt}: {e}")
            except Exception as e:
                errors_in += 1
                print(f"  ERROR writing linked_from for {tgt}: {e}")

    print(f"  Done: {written_in} written, {errors_in} errors")

    print(f"\n{'='*60}")
    print("BACKFILL COMPLETE")
    print(f"  links_to:    {written_out} URLs updated")
    print(f"  linked_from: {written_in} URLs updated")
    print(f"{'='*60}")


if __name__ == '__main__':
    main()
