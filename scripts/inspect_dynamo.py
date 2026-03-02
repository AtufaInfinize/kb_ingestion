#!/usr/bin/env python3
"""
Inspect DynamoDB tables used by the university crawler.

  1. Count items per crawl_status for a given university (parallel queries).
  2. Describe the schema (keys, GSIs, TTL, billing) of all crawler tables.

Usage:
    python inspect_dynamo.py --university-id gmu
    python inspect_dynamo.py --university-id gmu --env prod
    python inspect_dynamo.py --university-id gmu --schema-only
    python inspect_dynamo.py --university-id gmu --counts-only
"""

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import boto3

# -------------------------------------------------------
# All tables created by template.yaml (suffix = -{env})
# -------------------------------------------------------
TABLE_BASES = [
    "url-registry",
    "entity-store",
    "crawl-rate-limits",
    "robots-cache",
]

# All known crawl_status values in the system
CRAWL_STATUSES = [
    "crawled",
    "pending",
    "redirected",
    "blocked_robots",
    "skipped_depth",
    "error",
    "failed",
    "dead",
]


# -------------------------------------------------------
# Status counts via GSI query (SELECT COUNT)
# -------------------------------------------------------
def count_status(table, index, university_id, status):
    """Return the item count for one (university_id, crawl_status) pair."""
    total = 0
    query_kwargs = {
        'IndexName': index,
        'Select': 'COUNT',
        'KeyConditionExpression': 'university_id = :uid AND crawl_status = :s',
        'ExpressionAttributeValues': {
            ':uid': university_id,
            ':s': status,
        },
    }
    while True:
        resp = table.query(**query_kwargs)
        total += resp['Count']
        if 'LastEvaluatedKey' not in resp:
            break
        query_kwargs['ExclusiveStartKey'] = resp['LastEvaluatedKey']
    return status, total


def print_status_counts(args):
    """Query each status in parallel and print a summary table."""
    dynamodb = boto3.resource('dynamodb', region_name=args.region)
    table_name = f"url-registry-{args.env}"
    table = dynamodb.Table(table_name)

    print(f"Counting items per status for university_id = '{args.university_id}'")
    print(f"Table: {table_name}  |  Index: {args.index}")
    print(f"{'─' * 50}")

    start = time.time()
    results = {}

    with ThreadPoolExecutor(max_workers=len(CRAWL_STATUSES)) as executor:
        futures = {
            executor.submit(count_status, table, args.index, args.university_id, s): s
            for s in CRAWL_STATUSES
        }
        for future in as_completed(futures):
            status = futures[future]
            try:
                _, count = future.result()
                results[status] = count
            except Exception as exc:
                print(f"  [{status}] ERROR: {exc}")
                results[status] = -1

    grand_total = 0
    for status in CRAWL_STATUSES:
        count = results.get(status, 0)
        if count >= 0:
            grand_total += count
        marker = " <--" if count > 10000 else ""
        print(f"  {status:<20s} {count:>10,}{marker}")

    print(f"{'─' * 50}")
    print(f"  {'TOTAL':<20s} {grand_total:>10,}")
    print(f"  (completed in {time.time() - start:.1f}s)\n")


# -------------------------------------------------------
# Table schema description
# -------------------------------------------------------
def describe_table_schema(client, table_name):
    """Describe a single table and return a formatted string."""
    try:
        resp = client.describe_table(TableName=table_name)
    except client.exceptions.ResourceNotFoundException:
        return f"\n{'═' * 60}\n  {table_name}  —  TABLE NOT FOUND\n{'═' * 60}\n"

    t = resp['Table']
    lines = []
    lines.append(f"\n{'═' * 60}")
    lines.append(f"  {table_name}")
    lines.append(f"{'═' * 60}")

    # Item count & size (approximate, updated every ~6 hrs)
    lines.append(f"  Item count (approx): {t.get('ItemCount', 'N/A'):,}")
    size_bytes = t.get('TableSizeBytes', 0)
    if size_bytes > 1_073_741_824:
        lines.append(f"  Table size (approx): {size_bytes / 1_073_741_824:.2f} GB")
    elif size_bytes > 1_048_576:
        lines.append(f"  Table size (approx): {size_bytes / 1_048_576:.2f} MB")
    else:
        lines.append(f"  Table size (approx): {size_bytes / 1024:.1f} KB")

    # Billing
    billing = t.get('BillingModeSummary', {}).get('BillingMode', 'PROVISIONED')
    lines.append(f"  Billing mode:        {billing}")

    # Key schema
    attr_types = {a['AttributeName']: a['AttributeType'] for a in t['AttributeDefinitions']}
    lines.append(f"\n  Key Schema:")
    for key in t['KeySchema']:
        aname = key['AttributeName']
        lines.append(f"    {key['KeyType']:<6s}  {aname} ({attr_types.get(aname, '?')})")

    # GSIs
    gsis = t.get('GlobalSecondaryIndexes', [])
    if gsis:
        lines.append(f"\n  Global Secondary Indexes ({len(gsis)}):")
        for gsi in gsis:
            lines.append(f"    [{gsi['IndexName']}]")
            for key in gsi['KeySchema']:
                aname = key['AttributeName']
                lines.append(f"      {key['KeyType']:<6s}  {aname} ({attr_types.get(aname, '?')})")
            proj = gsi['Projection']['ProjectionType']
            lines.append(f"      Projection: {proj}")
            idx_count = gsi.get('ItemCount', 'N/A')
            if isinstance(idx_count, int):
                idx_count = f"{idx_count:,}"
            lines.append(f"      Items:      {idx_count}")

    # TTL
    try:
        ttl_resp = client.describe_time_to_live(TableName=table_name)
        ttl = ttl_resp.get('TimeToLiveDescription', {})
        ttl_status = ttl.get('TimeToLiveStatus', 'DISABLED')
        ttl_attr = ttl.get('AttributeName', 'N/A')
        if ttl_status == 'ENABLED':
            lines.append(f"\n  TTL: ENABLED on '{ttl_attr}'")
        else:
            lines.append(f"\n  TTL: {ttl_status}")
    except Exception:
        lines.append(f"\n  TTL: (unable to query)")

    return '\n'.join(lines)


def print_table_schemas(args):
    """Describe all crawler tables in parallel."""
    client = boto3.client('dynamodb', region_name=args.region)
    table_names = [f"{base}-{args.env}" for base in TABLE_BASES]

    print(f"Describing schemas for {len(table_names)} tables (env={args.env})")

    with ThreadPoolExecutor(max_workers=len(table_names)) as executor:
        futures = {
            executor.submit(describe_table_schema, client, name): name
            for name in table_names
        }
        for future in as_completed(futures):
            print(future.result())

    print()


# -------------------------------------------------------
# Main
# -------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description='Inspect DynamoDB tables for the university crawler')
    parser.add_argument('--university-id', required=True, help='University ID (e.g., gmu)')
    parser.add_argument('--env', default='dev', help='Environment suffix (default: dev)')
    parser.add_argument('--index', default='university-status-index', help='GSI name for status counts')
    parser.add_argument('--region', default='us-east-1', help='AWS region')
    parser.add_argument('--counts-only', action='store_true', help='Only show status counts')
    parser.add_argument('--schema-only', action='store_true', help='Only show table schemas')
    args = parser.parse_args()

    show_counts = not args.schema_only
    show_schema = not args.counts_only

    if show_counts:
        print_status_counts(args)

    if show_schema:
        print_table_schemas(args)


if __name__ == '__main__':
    main()
