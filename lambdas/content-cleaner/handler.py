"""
Content Cleaner Lambda
Reads raw HTML from S3, extracts clean text using trafilatura,
outputs markdown with heading structure preserved.
Creates the .md file in the clean-content prefix.

Triggered by: processing-queue (SQS)
Input message format:
{
    "url": "https://www.phc.edu/admissions",
    "university_id": "phc",
    "domain": "www.phc.edu",
    "s3_key": "raw-html/phc/www.phc.edu/a3f2b1c9.html",
    "content_type": "text/html",
    "crawled_at": "2026-02-16T...",
    "url_hash": "a3f2b1c9",
    "depth": 0,
    "action": "process" | "delete"
}
"""

import json
import os
import re
import hashlib
import logging
from datetime import datetime, timezone
from urllib.parse import urlparse, urljoin, urldefrag

import boto3
import trafilatura
from trafilatura.settings import use_config

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3 = boto3.client('s3')
sqs = boto3.client('sqs')
dynamodb = boto3.resource('dynamodb')

BUCKET = os.environ.get('CONTENT_BUCKET')
URL_REGISTRY_TABLE = os.environ.get('URL_REGISTRY_TABLE', 'url-registry-dev')
ENTITY_STORE_TABLE = os.environ.get('ENTITY_STORE_TABLE', 'entity-store-dev')

entity_table = dynamodb.Table(ENTITY_STORE_TABLE)

# Per-execution cache of crawl mode per university (avoids repeated DynamoDB reads)
_crawl_mode_cache: dict = {}

# Configure trafilatura for best extraction quality
traf_config = use_config()
traf_config.set("DEFAULT", "EXTRACTION_TIMEOUT", "60")
traf_config.set("DEFAULT", "MIN_OUTPUT_SIZE", "50")

# Config cache: {university_id: config_dict}
_config_cache = {}
MAX_LINKS_TO = 500

SKIP_EXTENSIONS = {
    ".zip", ".gz", ".tar", ".rar",
    ".mp4", ".mp3", ".avi", ".mov", ".wmv", ".flv",
    ".jpg", ".jpeg", ".png", ".gif", ".svg", ".ico", ".webp", ".bmp",
    ".css", ".js", ".woff", ".woff2", ".ttf", ".eot",
    ".xls", ".xlsx", ".ppt", ".pptx", ".doc", ".docx",
}


def load_university_config(university_id):
    """Load university config from S3, with in-memory cache."""
    if university_id in _config_cache:
        return _config_cache[university_id]
    try:
        key = f"configs/{university_id}.json"
        obj = s3.get_object(Bucket=BUCKET, Key=key)
        config = json.loads(obj['Body'].read().decode('utf-8'))
        _config_cache[university_id] = config
        return config
    except Exception as e:
        logger.warning(f"Could not load config for {university_id}: {e}")
        return {}


def _get_crawl_mode(university_id: str) -> str:
    """Return 'incremental' or 'full' for this university's current crawl.

    Reads entity-store once per Lambda execution context, then caches in memory.
    Falls back to 'full' (safe default: keep existing sidecar-deletion behaviour).
    """
    if university_id in _crawl_mode_cache:
        return _crawl_mode_cache[university_id]
    try:
        resp = entity_table.get_item(
            Key={'university_id': university_id, 'entity_key': 'current_crawl_mode'}
        )
        item = resp.get('Item', {})
        mode = item.get('refresh_mode', 'full')
    except Exception as e:
        logger.warning(f"Could not read crawl mode for {university_id}: {e}")
        mode = 'full'
    _crawl_mode_cache[university_id] = mode
    return mode


def _normalize_url(parsed):
    """Normalize a parsed URL for deduplication."""
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
    """Check if a hostname matches the allowed domain patterns."""
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
        else:
            if hostname == pattern:
                return True
    return False


def extract_internal_links(html, source_url, allowed_patterns, exclude_domains):
    """Extract internal links from HTML. Returns list of normalized URLs."""
    href_pattern = re.compile(r"href=[\"']([^\"']+)[\"']", re.IGNORECASE)
    all_internal = set()

    parsed_base = urlparse(source_url)
    normalized_base = _normalize_url(parsed_base)

    total_hrefs = 0
    skip_proto = 0
    skip_scheme = 0
    skip_ext = 0
    skip_domain = 0
    skip_self = 0

    for match in href_pattern.finditer(html):
        href = match.group(1).strip()
        total_hrefs += 1
        if not href or href.startswith(("javascript:", "mailto:", "tel:", "#", "data:")):
            skip_proto += 1
            continue

        full_url = urljoin(source_url, href)
        full_url, _ = urldefrag(full_url)
        parsed = urlparse(full_url)

        if parsed.scheme not in ("http", "https"):
            skip_scheme += 1
            continue

        # Skip non-page extensions
        path_lower = parsed.path.lower()
        if any(path_lower.endswith(ext) for ext in SKIP_EXTENSIONS):
            skip_ext += 1
            continue

        hostname = (parsed.hostname or '').lower()
        if not hostname:
            continue

        if not _is_allowed_domain(hostname, allowed_patterns, exclude_domains):
            skip_domain += 1
            continue

        normalized = _normalize_url(parsed)
        if normalized == normalized_base:
            skip_self += 1
            continue

        all_internal.add(normalized)

    logger.info(
        f"Link extraction for {source_url}: {total_hrefs} hrefs found, "
        f"{len(all_internal)} internal kept, "
        f"filtered: {skip_proto} proto, {skip_scheme} scheme, {skip_ext} ext, "
        f"{skip_domain} off-domain, {skip_self} self"
    )

    return sorted(all_internal)[:MAX_LINKS_TO]


def handler(event, context):
    """Process SQS messages containing crawled page info."""
    results = []

    for record in event.get('Records', []):
        try:
            message = json.loads(record['body'])
            result = process_message(message)
            results.append(result)
        except Exception as e:
            logger.error(f"Error processing record: {e}", exc_info=True)
            # Don't raise - let other messages in the batch succeed
            # Set processing_status so we know this page was attempted
            try:
                msg = json.loads(record.get('body', '{}'))
                if msg.get('url'):
                    update_url_registry(msg['url'], 'cleaning_error',
                                        error_detail=str(e)[:200])
            except Exception:
                pass
            results.append({
                'status': 'error',
                'error': str(e),
                'message': record.get('body', '')[:200]
            })

    succeeded = sum(1 for r in results if r.get('status') == 'success')
    skipped = sum(1 for r in results if r.get('status') == 'skipped')
    failed = sum(1 for r in results if r.get('status') == 'error')
    logger.info(f"Batch complete: {succeeded} succeeded, {skipped} skipped, {failed} failed")

    return {'results': results}


def process_message(message):
    """Process a single crawled page message."""
    action = message.get('action', 'process')
    url = message.get('url', '')
    university_id = message.get('university_id', '')
    s3_key = message.get('s3_key', '')
    url_hash = message.get('url_hash', '')
    domain = message.get('domain', '')

    # Fallback: extract url_hash from s3_key if missing
    if not url_hash and s3_key:
        # s3_key format: raw-html/{uid}/{domain}/{hash}.html
        filename = s3_key.rsplit('/', 1)[-1]  # e.g. "a3f2b1c9.html"
        url_hash = filename.rsplit('.', 1)[0]  # e.g. "a3f2b1c9"
    content_type = message.get('content_type', 'text/html')
    depth = message.get('depth', 0)
    crawled_at = message.get('crawled_at', '')

    # Handle deletions - remove the clean content if the page was deleted
    if action == 'delete':
        return handle_deletion(university_id, domain, url_hash)

    # Skip PDFs - they go to the PDF processor (separate queue)
    if content_type in ('application/pdf', 'pdf') or s3_key.startswith('raw-pdf/'):
        logger.info(f"Skipping PDF: {url} - handled by PDF processor")
        return {'status': 'skipped', 'reason': 'pdf', 'url': url}

    # Skip if no S3 key
    if not s3_key:
        logger.warning(f"No s3_key for {url}")
        return {'status': 'skipped', 'reason': 'no_s3_key', 'url': url}

    logger.info(f"Processing: {url} (s3_key: {s3_key})")

    # 1. Fetch raw HTML from S3
    try:
        response = s3.get_object(Bucket=BUCKET, Key=s3_key)
        raw_html = response['Body'].read().decode('utf-8', errors='replace')
    except s3.exceptions.NoSuchKey:
        logger.warning(f"S3 object not found: {s3_key}")
        update_url_registry(url, 's3_not_found')
        return {'status': 'skipped', 'reason': 'not_found', 'url': url}

    # 2. Extract clean content using trafilatura
    extracted = extract_content(raw_html, url)

    if not extracted or not extracted.get('text'):
        logger.warning(f"No content extracted from {url}")
        # Update registry to mark as empty
        update_url_registry(url, 'empty_content')
        return {'status': 'skipped', 'reason': 'no_content', 'url': url}

    # 3. Extract internal links from raw HTML
    config = load_university_config(university_id)
    crawl_config = config.get('crawl_config', {})
    allowed_patterns = crawl_config.get('allowed_domain_patterns', [])
    exclude_domains = crawl_config.get('exclude_domains', [])
    if not allowed_patterns:
        logger.warning(f"No allowed_domain_patterns for {university_id}, skipping link extraction")
    internal_links = extract_internal_links(raw_html, url, allowed_patterns, exclude_domains)

    # 4. Build markdown with heading structure
    markdown_content = build_markdown(extracted)

    # 5. Store clean markdown in S3, invalidating classification if content changed
    clean_key = f"clean-content/{university_id}/{domain}/{url_hash}.md"
    new_bytes = markdown_content.encode('utf-8')

    # Check whether the content actually changed before overwriting
    content_changed = True
    try:
        old_obj = s3.get_object(Bucket=BUCKET, Key=clean_key)
        content_changed = old_obj['Body'].read() != new_bytes
    except s3.exceptions.NoSuchKey:
        pass  # New page — always classify

    s3.put_object(
        Bucket=BUCKET,
        Key=clean_key,
        Body=new_bytes,
        ContentType='text/markdown',
        Metadata={
            'source_url': url,
            'university_id': university_id,
            'domain': domain,
            'extracted_at': datetime.now(timezone.utc).isoformat()
        }
    )

    # If content changed, handle classification sidecar based on crawl mode.
    # Full crawl: delete sidecar so page gets re-classified.
    # Incremental crawl: keep existing sidecar (category stays the same) and
    # atomically increment the pages_changed counter so the admin is notified
    # to run KB Sync after the crawl completes.
    if content_changed:
        crawl_mode = _get_crawl_mode(university_id)
        if crawl_mode == 'incremental':
            try:
                entity_table.update_item(
                    Key={'university_id': university_id, 'entity_key': 'kb_sync_status'},
                    UpdateExpression='ADD pages_changed :n SET entity_type = :et',
                    ExpressionAttributeValues={':n': 1, ':et': 'crawl_state'},
                )
            except Exception as e:
                logger.warning(f"Could not increment pages_changed for {university_id}: {e}")
        else:
            metadata_key = f"clean-content/{university_id}/{domain}/{url_hash}.md.metadata.json"
            try:
                s3.delete_object(Bucket=BUCKET, Key=metadata_key)
                logger.info(f"Content changed — invalidated classification: {metadata_key}")
            except Exception:
                pass

    logger.info(f"Stored clean content: {clean_key} ({len(new_bytes)} chars, changed={content_changed})")

    # 6. Write links_to to DynamoDB
    write_links_to(url, internal_links)

    # 7. Mark as cleaned in DynamoDB so we can track the pipeline
    update_url_registry(url, 'cleaned')

    return {
        'status': 'success',
        'url': url,
        'clean_key': clean_key,
        'content_length': len(markdown_content),
        'title': extracted.get('title', '')
    }


def extract_content(html, url):
    """Extract clean content from HTML using trafilatura."""
    result = {}

    # Use trafilatura for main content extraction
    # output_format='txt' gives clean text, 'xml' preserves structure
    extracted_text = trafilatura.extract(
        html,
        url=url,
        output_format='txt',
        include_comments=False,
        include_tables=True,
        include_links=True,
        include_images=False,
        favor_precision=False,  # favor recall to get more content
        config=traf_config
    )

    # Also extract with XML format to get heading structure
    extracted_xml = trafilatura.extract(
        html,
        url=url,
        output_format='xml',
        include_comments=False,
        include_tables=True,
        include_links=True,
        include_images=False,
        favor_precision=False,
        config=traf_config
    )

    # Extract metadata
    metadata = trafilatura.extract_metadata(html, default_url=url)

    result['text'] = extracted_text or ''
    result['xml'] = extracted_xml or ''
    result['title'] = ''
    result['description'] = ''
    result['author'] = ''
    result['date'] = ''
    result['sitename'] = ''

    if metadata:
        result['title'] = metadata.title or ''
        result['description'] = metadata.description or ''
        result['author'] = metadata.author or ''
        result['date'] = metadata.date or ''
        result['sitename'] = metadata.sitename or ''

    # Fallback title extraction from HTML if trafilatura didn't get it
    if not result['title']:
        result['title'] = extract_title_fallback(html)

    return result


def extract_title_fallback(html):
    """Extract title from HTML using simple regex as fallback."""
    # Try <title> tag
    match = re.search(r'<title[^>]*>(.*?)</title>', html, re.IGNORECASE | re.DOTALL)
    if match:
        title = match.group(1).strip()
        # Clean up common site-name suffixes from titles
        title = re.sub(r'\s*[|\-–—]\s*(?:Patrick Henry College|PHC|George Mason University|Mason).*$', '', title)
        return title.strip()

    # Try <h1> tag
    match = re.search(r'<h1[^>]*>(.*?)</h1>', html, re.IGNORECASE | re.DOTALL)
    if match:
        return re.sub(r'<[^>]+>', '', match.group(1)).strip()

    return ''


def build_markdown(extracted):
    """Build clean markdown from extracted content.

    If XML output is available, parse it for heading structure.
    Otherwise, use the plain text output.
    """
    xml_content = extracted.get('xml', '')
    text_content = extracted.get('text', '')
    title = extracted.get('title', '')

    # Start with title
    lines = []
    if title:
        lines.append(f"# {title}")
        lines.append('')

    if xml_content:
        # Parse the trafilatura XML to reconstruct markdown with headings
        markdown_from_xml = xml_to_markdown(xml_content)
        if markdown_from_xml.strip():
            lines.append(markdown_from_xml)
        else:
            lines.append(text_content)
    else:
        lines.append(text_content)

    # Add metadata footer
    description = extracted.get('description', '')
    if description:
        lines.append('')
        lines.append('---')
        lines.append(f'*{description}*')

    return '\n'.join(lines)


def xml_to_markdown(xml_content):
    """Convert trafilatura XML output to markdown with heading structure."""
    try:
        import xml.etree.ElementTree as ET
        root = ET.fromstring(xml_content)
    except Exception:
        return ''

    lines = []

    for element in root.iter():
        tag = element.tag
        text = (element.text or '').strip()

        if not text:
            continue

        if tag == 'head' and element.get('rend'):
            rend = element.get('rend', '')
            if 'h1' in rend:
                lines.append(f"\n# {text}\n")
            elif 'h2' in rend:
                lines.append(f"\n## {text}\n")
            elif 'h3' in rend:
                lines.append(f"\n### {text}\n")
            elif 'h4' in rend:
                lines.append(f"\n#### {text}\n")
            else:
                lines.append(f"\n## {text}\n")
        elif tag == 'head':
            lines.append(f"\n## {text}\n")
        elif tag == 'p':
            lines.append(f"{text}\n")
        elif tag == 'item':
            lines.append(f"- {text}")
        elif tag == 'cell':
            # Table cells - basic handling
            lines.append(f"| {text} ")
        elif tag == 'row':
            lines.append('|')
        elif tag == 'hi' and text:
            # Highlighted/bold text - append to current context
            lines.append(f"**{text}**")
        elif tag == 'ref' and text:
            target = element.get('target', '')
            if target:
                lines.append(f"[{text}]({target})")
            else:
                lines.append(text)

    return '\n'.join(lines)


def handle_deletion(university_id, domain, url_hash):
    """Handle page deletion - remove clean content and metadata."""
    keys_to_delete = [
        f"clean-content/{university_id}/{domain}/{url_hash}.md",
        f"clean-content/{university_id}/{domain}/{url_hash}.md.metadata.json"
    ]

    for key in keys_to_delete:
        try:
            s3.delete_object(Bucket=BUCKET, Key=key)
            logger.info(f"Deleted: {key}")
        except Exception as e:
            logger.warning(f"Could not delete {key}: {e}")

    return {
        'status': 'success',
        'action': 'deleted',
        'university_id': university_id,
        'url_hash': url_hash
    }


def write_links_to(url, links):
    """Write links_to to the URL registry, only if not already present."""
    if not links:
        return
    try:
        table = dynamodb.Table(URL_REGISTRY_TABLE)
        table.update_item(
            Key={'url': url},
            UpdateExpression='SET links_to = :lt',
            ConditionExpression='attribute_not_exists(links_to)',
            ExpressionAttributeValues={':lt': links}
        )
    except dynamodb.meta.client.exceptions.ConditionalCheckFailedException:
        pass  # links_to already exists, skip
    except Exception as e:
        logger.warning(f"Could not write links_to for {url}: {e}")


def update_url_registry(url, status_note, error_detail=None):
    """Update URL registry with processing status."""
    try:
        table = dynamodb.Table(URL_REGISTRY_TABLE)
        update_expr = 'SET processing_status = :status, processing_updated_at = :ts'
        expr_values = {
            ':status': status_note,
            ':ts': datetime.now(timezone.utc).isoformat()
        }
        if error_detail:
            update_expr += ', last_error = :err'
            expr_values[':err'] = error_detail
        table.update_item(
            Key={'url': url},
            UpdateExpression=update_expr,
            ExpressionAttributeValues=expr_values,
        )
    except Exception as e:
        logger.warning(f"Could not update URL registry for {url}: {e}")