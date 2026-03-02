#!/usr/bin/env python3
"""Count S3 objects under clean-content/gmu/ and raw-html/gmu/ with progress."""

import boto3
import time
import sys

BUCKET = "university-kb-content-251221984842-dev"

def count_prefix(s3, prefix):
    paginator = s3.get_paginator("list_objects_v2")
    count = 0
    md_count = 0
    meta_count = 0
    start = time.time()

    for page in paginator.paginate(Bucket=BUCKET, Prefix=prefix):
        for o in page.get("Contents", []):
            key = o["Key"]
            count += 1
            if key.endswith(".md.metadata.json"):
                meta_count += 1
            elif key.endswith(".md"):
                md_count += 1

        elapsed = time.time() - start
        rate = count / elapsed if elapsed > 0 else 0
        sys.stdout.write(f"\r  {prefix}  {count:>8,} objects  ({rate:.0f}/s)")
        sys.stdout.flush()

    print()
    return count, md_count, meta_count


s3 = boto3.client("s3", region_name="us-east-1")

print("Counting clean-content/gmu/ ...")
total_clean, md, meta = count_prefix(s3, "clean-content/gmu/")
print(f"  .md files:           {md:,}")
print(f"  .metadata.json:      {meta:,}")
print(f"  other:               {total_clean - md - meta:,}")

print("\nCounting raw-html/gmu/ ...")
total_raw, _, _ = count_prefix(s3, "raw-html/gmu/")

print(f"\n{'='*40}")
print(f"  raw-html/gmu/:       {total_raw:,}")
print(f"  clean-content/gmu/:  {total_clean:,}")
print(f"  Gap:                 {total_raw - total_clean:,}")
print(f"{'='*40}")
