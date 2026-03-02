#!/usr/bin/env python3
"""
Test the page classifier on 3 sample pages from S3.
Downloads clean markdown, calls Bedrock Claude Haiku, saves metadata locally.

Usage:
    python scripts/test_classifier.py --university-id phc
    python scripts/test_classifier.py --university-id phc --pages 5
"""

import argparse
import json
import os
import re
import sys

import boto3

BUCKET = os.environ.get('CONTENT_BUCKET', 'university-kb-content-251221984842-dev')
URL_REGISTRY_TABLE = os.environ.get('URL_REGISTRY_TABLE', 'url-registry-dev')
MODEL_ID = os.environ.get('CLASSIFIER_MODEL_ID', 'anthropic.claude-3-haiku-20240307-v1:0')

CLASSIFICATION_PROMPT = """You are a university web page classifier. Analyze this page and return a JSON response.

Page URL: __URL__
Page Title: __TITLE__
Page Description: __DESCRIPTION__

Content (first 1000 characters):
__CONTENT_PREVIEW__

Rules:
- Pick the MOST specific category that fits the visible content.
- Use "low_value" if the page has no substantive content for prospective students, parents, or researchers.
  Examples: login/auth walls, empty search results, template placeholders (e.g. {{field.value}}), cookie notices, error pages, redirect stubs, or pages with fewer than ~50 words of real content.
- is_useful_page = "yes" only if the page contains meaningful, information-rich content.
- is_high_traffic_page = "yes" only if this is likely a major entry page (homepage, admissions overview, tuition page, financial aid, application pages, major program landing pages, campus visit pages).
- Extract ONLY concrete, explicitly stated facts.
- Never invent values or infer missing numbers or dates.
- Prefer exact values (dates, prices, GPA thresholds, phone numbers, emails, hours).
- Use snake_case keys for facts.
- If no structured facts exist, return an empty array.
- Output must be valid JSON only. No markdown, no explanations.

Classify the page and extract structured facts. Return ONLY valid JSON with this exact structure:
{
    "category": "<one of: admissions, financial_aid, academic_programs, course_catalog, student_services, housing_dining, campus_life, athletics, faculty_staff, library, it_services, policies, events, news, about, careers, alumni, low_value, other>",
    "subcategory": "<specific classification, e.g. 'transfer_admissions', 'merit_scholarships', 'computer_science_program'>",
    "summary": "<1–2 sentence factual summary describing ONLY visible page content. Be specific and literal. Do NOT infer intent or add marketing language. If content is blocked, say 'Page content not available.' If empty, broken, or boilerplate, state that explicitly.>",
    "is_useful_page": "<yes or no>",
    "is_high_traffic_page": "<yes or no>",
    "facts": [
        {
            "type": "<deadline|requirement|cost|contact|hours|program|policy|statistic>",
            "key": "<snake_case key>",
            "value": "<literal value from page>",
            "context": "<qualifying context if needed>"
        }
    ]
}
"""


EXPECTED_KEYS = {"category", "subcategory", "summary", "is_useful_page", "is_high_traffic_page", "facts"}


def parse_classification(raw_text: str) -> dict:
    """
    Robustly parse the LLM classification response into a dict.

    Handles: markdown fences, preamble/trailing text, escaped chars,
    multiple JSON blocks, and partial/malformed output.
    """
    text = raw_text.strip()

    # 1. Strip markdown code fences (```json ... ``` or ``` ... ```)
    fence_match = re.search(r'```(?:json)?\s*\n?(.*?)```', text, re.DOTALL)
    if fence_match:
        text = fence_match.group(1).strip()

    # 2. Try direct parse first (fast path)
    try:
        result = json.loads(text)
        if isinstance(result, dict):
            return _normalize(result)
    except json.JSONDecodeError:
        pass

    # 3. Extract the outermost { ... } block (handles preamble/trailing text)
    brace_start = text.find('{')
    if brace_start != -1:
        depth = 0
        brace_end = -1
        in_string = False
        escape_next = False
        for i in range(brace_start, len(text)):
            ch = text[i]
            if escape_next:
                escape_next = False
                continue
            if ch == '\\' and in_string:
                escape_next = True
                continue
            if ch == '"' and not escape_next:
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    brace_end = i
                    break

        if brace_end != -1:
            candidate = text[brace_start:brace_end + 1]
            try:
                result = json.loads(candidate)
                if isinstance(result, dict):
                    return _normalize(result)
            except json.JSONDecodeError:
                pass

            # 4. Try fixing common issues: trailing commas, single quotes
            fixed = _fix_json(candidate)
            try:
                result = json.loads(fixed)
                if isinstance(result, dict):
                    return _normalize(result)
            except json.JSONDecodeError:
                pass

    # 5. Last resort: regex extraction of individual fields
    return _regex_fallback(raw_text)


def _fix_json(text: str) -> str:
    """Fix common JSON issues from LLM output."""
    # Remove trailing commas before } or ]
    text = re.sub(r',\s*([}\]])', r'\1', text)
    # Replace single-quoted strings with double-quoted (naive but effective)
    # Only do this if there are no double quotes (avoids breaking valid JSON)
    if '"' not in text and "'" in text:
        text = text.replace("'", '"')
    return text


def _normalize(d: dict) -> dict:
    """Ensure all expected keys exist with sensible defaults."""
    return {
        "category": d.get("category", "other"),
        "subcategory": d.get("subcategory", ""),
        "summary": d.get("summary", ""),
        "is_useful_page": str(d.get("is_useful_page", "no")).lower(),
        "is_high_traffic_page": str(d.get("is_high_traffic_page", "no")).lower(),
        "facts": d.get("facts") if isinstance(d.get("facts"), list) else [],
    }


def _regex_fallback(text: str) -> dict:
    """Extract fields individually via regex when JSON parsing fails entirely."""
    def _extract(key):
        m = re.search(rf'"{key}"\s*:\s*"([^"]*)"', text)
        return m.group(1) if m else ''

    # Try to extract facts array
    facts = []
    facts_match = re.search(r'"facts"\s*:\s*(\[.*?\])', text, re.DOTALL)
    if facts_match:
        try:
            facts = json.loads(facts_match.group(1))
        except json.JSONDecodeError:
            facts = []

    return {
        "category": _extract("category") or "other",
        "subcategory": _extract("subcategory"),
        "summary": _extract("summary"),
        "is_useful_page": _extract("is_useful_page") or "no",
        "is_high_traffic_page": _extract("is_high_traffic_page") or "no",
        "facts": facts if isinstance(facts, list) else [],
    }


def main():
    parser = argparse.ArgumentParser(description='Test classifier on sample pages')
    parser.add_argument('--university-id', required=True)
    parser.add_argument('--region', default='us-east-1')
    parser.add_argument('--pages', type=int, default=3, help='Number of pages to test')
    parser.add_argument('--output-dir', default='test_classifier_output')
    args = parser.parse_args()

    s3 = boto3.client('s3', region_name=args.region)
    bedrock = boto3.client('bedrock-runtime', region_name=args.region)
    dynamodb = boto3.resource('dynamodb', region_name=args.region)
    table = dynamodb.Table(URL_REGISTRY_TABLE)

    os.makedirs(args.output_dir, exist_ok=True)

    # Find clean .md files (skip metadata sidecars)
    prefix = f"clean-content/{args.university_id}/"
    print(f"Listing clean content in s3://{BUCKET}/{prefix}...", flush=True)

    md_keys = []
    paginator = s3.get_paginator('list_objects_v2')
    for page in paginator.paginate(Bucket=BUCKET, Prefix=prefix):
        for obj in page.get('Contents', []):
            if obj['Key'].endswith('.md') and not obj['Key'].endswith('.metadata.json'):
                md_keys.append(obj['Key'])
            if len(md_keys) >= args.pages:
                break
        if len(md_keys) >= args.pages:
            break

    print(f"Found {len(md_keys)} files, testing {min(args.pages, len(md_keys))}", flush=True)

    for i, md_key in enumerate(md_keys[:args.pages]):
        print(f"\n{'='*60}", flush=True)
        print(f"Page {i+1}: {md_key}", flush=True)
        print(f"{'='*60}", flush=True)

        # Read the markdown
        obj = s3.get_object(Bucket=BUCKET, Key=md_key)
        markdown = obj['Body'].read().decode('utf-8', errors='replace')
        s3_meta = obj.get('Metadata', {})
        source_url = s3_meta.get('source_url', '')

        # Extract title from first line if it's a heading
        title = ''
        first_line = markdown.split('\n')[0] if markdown else ''
        if first_line.startswith('# '):
            title = first_line[2:].strip()

        content_preview = markdown[:1000]

        print(f"  URL:   {source_url}", flush=True)
        print(f"  Title: {title}", flush=True)
        print(f"  Content length: {len(markdown)} chars", flush=True)
        print(f"  Preview: {content_preview[:200]}...", flush=True)

        # Call Bedrock
        print(f"\n  Calling Bedrock ({MODEL_ID})...", flush=True)
        prompt = (CLASSIFICATION_PROMPT
                  .replace('__URL__', source_url)
                  .replace('__TITLE__', title)
                  .replace('__DESCRIPTION__', '')
                  .replace('__CONTENT_PREVIEW__', content_preview))

        try:
            response = bedrock.invoke_model(
                modelId=MODEL_ID,
                contentType='application/json',
                accept='application/json',
                body=json.dumps({
                    "anthropic_version": "bedrock-2023-05-31",
                    "max_tokens": 1024,
                    "temperature": 0,
                    "messages": [
                        {"role": "user", "content": prompt}
                    ]
                })
            )

            response_body = json.loads(response['body'].read())
            raw_text = response_body.get('content', [{}])[0].get('text', '')
            print(f"  Raw LLM response:\n    {raw_text[:500]}", flush=True)

            classification = parse_classification(raw_text)

        except Exception as e:
            print(f"  ERROR from Bedrock: {e}", flush=True)
            classification = {
                'category': 'error',
                'subcategory': '',
                'summary': f'Bedrock call failed: {e}',
                'facts': []
            }

        # Fetch links_to from DynamoDB
        related_urls = []
        if source_url:
            try:
                resp = table.get_item(
                    Key={'url': source_url},
                    ProjectionExpression='links_to'
                )
                related_urls = resp.get('Item', {}).get('links_to', [])
            except Exception:
                pass

        # Build metadata doc (same structure as the classifier Lambda)
        metadata_doc = {
            "metadataAttributes": {
                "source_url": source_url,
                "category": classification.get('category', 'other'),
                "subcategory": classification.get('subcategory', ''),
                "summary": classification.get('summary', ''),
                "title": title or classification.get('summary', ''),
                "domain": s3_meta.get('domain', ''),
                "university_id": args.university_id,
                "content_length": len(markdown),
                "is_useful_page": classification.get('is_useful_page', 'yes'),
                "is_high_traffic_page": classification.get('is_high_traffic_page', 'no'),
                "related_urls": related_urls[:10],  # truncate for readability
                "facts": classification.get('facts', []),
            }
        }

        # Save locally
        safe_name = re.sub(r'[^\w]', '_', md_key)[:80]
        output_path = os.path.join(args.output_dir, f"{safe_name}.metadata.json")
        with open(output_path, 'w') as f:
            json.dump(metadata_doc, f, indent=2)

        print(f"\n  Result:", flush=True)
        print(f"    Category:    {classification.get('category')}", flush=True)
        print(f"    Subcategory: {classification.get('subcategory')}", flush=True)
        print(f"    Summary:     {classification.get('summary')}", flush=True)
        print(f"    Useful:      {classification.get('is_useful_page')}", flush=True)
        print(f"    High traffic:{classification.get('is_high_traffic_page')}", flush=True)
        print(f"    Facts:       {len(classification.get('facts', []))} extracted", flush=True)
        print(f"    Related URLs: {len(related_urls)} found in DynamoDB", flush=True)
        print(f"    Saved to:    {output_path}", flush=True)

    print(f"\n{'='*60}", flush=True)
    print(f"Done. {min(args.pages, len(md_keys))} metadata files saved to {args.output_dir}/", flush=True)


if __name__ == '__main__':
    main()
