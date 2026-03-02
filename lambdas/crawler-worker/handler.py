"""
Crawler Worker Lambda

The core scraping function. For each URL from the crawl queue:
1. Checks robots.txt compliance
2. Enforces per-domain rate limiting
3. Makes conditional HTTP requests (ETag/Last-Modified for efficiency)
4. Detects content type (HTML vs PDF)
5. Stores raw content in S3
6. Extracts new links from HTML pages
7. Pushes discovered URLs back to crawl queue
8. Pushes completed pages to processing queue

Triggered by: SQS FIFO crawl queue
Concurrency: controlled by Lambda reserved concurrency (default 10-20)
"""

import os
import json
import hashlib
import time
import re
import logging
from datetime import datetime, timezone
from urllib.parse import urlparse, urljoin, urldefrag
from decimal import Decimal

import boto3
from botocore.exceptions import ClientError
import urllib3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ─────────────────────────────────────────────
# AWS Clients
# ─────────────────────────────────────────────
sqs = boto3.client("sqs")
dynamodb = boto3.resource("dynamodb")
s3 = boto3.client("s3")

# ─────────────────────────────────────────────
# Environment Variables
# ─────────────────────────────────────────────
CRAWL_QUEUE_URL = os.environ["CRAWL_QUEUE_URL"]
PROCESSING_QUEUE_URL = os.environ["PROCESSING_QUEUE_URL"]
PDF_PROCESSING_QUEUE_URL = os.environ.get("PDF_PROCESSING_QUEUE_URL", PROCESSING_QUEUE_URL)
URL_REGISTRY_TABLE = os.environ["URL_REGISTRY_TABLE"]
ROBOTS_CACHE_TABLE = os.environ["ROBOTS_CACHE_TABLE"]
RATE_LIMITS_TABLE = os.environ["RATE_LIMITS_TABLE"]
CONTENT_BUCKET = os.environ["CONTENT_BUCKET"]
USER_AGENT = os.environ.get("USER_AGENT", "InfinizeBot/1.0")

url_table = dynamodb.Table(URL_REGISTRY_TABLE)
robots_table = dynamodb.Table(ROBOTS_CACHE_TABLE)
rate_table = dynamodb.Table(RATE_LIMITS_TABLE)

# HTTP client
http = urllib3.PoolManager(
    timeout=urllib3.Timeout(connect=10, read=30),
    retries=urllib3.Retry(total=1, backoff_factor=0.3),
    headers={"User-Agent": USER_AGENT},
)

# ─────────────────────────────────────────────
# File extension sets
# ─────────────────────────────────────────────
SKIP_EXTENSIONS = {
    ".zip", ".gz", ".tar", ".rar",
    ".mp4", ".mp3", ".avi", ".mov", ".wmv", ".flv",
    ".jpg", ".jpeg", ".png", ".gif", ".svg", ".ico", ".webp", ".bmp",
    ".css", ".js", ".woff", ".woff2", ".ttf", ".eot",
    ".xls", ".xlsx", ".ppt", ".pptx", ".doc", ".docx",
}

PDF_EXTENSIONS = {".pdf"}

# ─────────────────────────────────────────────
# University config cache (loaded from S3 on first use)
# Persists across invocations within the same Lambda container.
# ─────────────────────────────────────────────
_config_cache = {}

# ─────────────────────────────────────────────
# URL patterns to skip (junk / non-content pages)
# ─────────────────────────────────────────────
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
    r'/logout',
    r'/sso/',
    r'/cas/',
    r'/auth/',
    r'/authorize\?',
    r'/oauth2?/',

    # Admin and backend
    r'/wp-admin',
    r'/wp-login',
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

    # Portals (require auth, no public content)
    r'/studentportal/',
    r'/facultyportal/',
    r'/payerportal/',

    # HubSpot CMS internals
    r'/_hcms/',
    r'/hs/hsstatic/',

    # Preview and draft pages
    r'[?&]preview=',
    r'[?&]replytocom=',
    r'[?&]ref=',
]

JUNK_PATTERNS_COMPILED = [re.compile(p, re.IGNORECASE) for p in JUNK_URL_PATTERNS]


# ═════════════════════════════════════════════
# DOMAIN & URL FILTERING
# ═════════════════════════════════════════════
def load_university_config(university_id):
    """Load university config from S3, cached in memory per Lambda container."""
    if university_id in _config_cache:
        return _config_cache[university_id]

    try:
        response = s3.get_object(
            Bucket=CONTENT_BUCKET,
            Key=f"configs/{university_id}.json"
        )
        config = json.loads(response["Body"].read().decode("utf-8"))
        _config_cache[university_id] = config
        logger.info(f"Loaded config from S3 for {university_id}")
        return config
    except Exception as e:
        logger.warning(f"Failed to load config for {university_id}: {e}")
        _config_cache[university_id] = {}
        return {}


def get_allowed_domains(university_id):
    """Return the domain allowlist from config (allowed patterns minus excluded domains)."""
    config = load_university_config(university_id)
    crawl_config = config.get("crawl_config", {})
    return crawl_config.get("allowed_domain_patterns", [])


def get_exclude_domains(university_id):
    """Return excluded domains (e.g. email.gmu.edu, patriotweb.gmu.edu)."""
    config = load_university_config(university_id)
    crawl_config = config.get("crawl_config", {})
    return crawl_config.get("exclude_domains", [])


def is_allowed_domain(url, allowed_domains, exclude_domains=None):
    """Check if URL belongs to an allowed domain and not in exclude list."""
    try:
        hostname = urlparse(url).hostname
        if not hostname:
            return False
        hostname = hostname.lower()
    except Exception:
        return False

    # Check exclude list first
    if exclude_domains:
        for excl in exclude_domains:
            if hostname == excl.lower():
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


def is_junk_url(url):
    """Check if URL matches known junk/non-content patterns."""
    for pattern in JUNK_PATTERNS_COMPILED:
        if pattern.search(url):
            return True
    return False


# ═════════════════════════════════════════════
# MAIN HANDLER
# ═════════════════════════════════════════════
def lambda_handler(event, context):
    batch_item_failures = []

    for record in event.get("Records", []):
        message_id = record["messageId"]
        try:
            message = json.loads(record["body"])
            url = message["url"]
            university_id = message["university_id"]
            domain = message["domain"]
            depth = message.get("depth", 0)
            max_crawl_depth = message.get("max_crawl_depth", 3)

            logger.info(f"Processing (depth={depth}/{max_crawl_depth}): {url}")
            process_url(url, university_id, domain, depth, max_crawl_depth)

        except Exception as e:
            logger.error(f"Error processing {message_id}: {e}", exc_info=True)
            batch_item_failures.append({"itemIdentifier": message_id})

    return {"batchItemFailures": batch_item_failures}

def process_url(url, university_id, domain, depth, max_crawl_depth):
    """Main processing logic for a single URL."""

    # Enforce max depth from config
    if depth > max_crawl_depth:
        logger.info(f"Skipping (depth {depth} exceeds max {max_crawl_depth}): {url}")
        mark_url_status(url, "skipped_depth")
        return

    # Load filtering config once — reused by extract_links downstream
    allowed_domains = get_allowed_domains(university_id)
    exclude_domains = get_exclude_domains(university_id)

    # ── Step 0a: Domain allowlist check ──
    if allowed_domains and not is_allowed_domain(url, allowed_domains, exclude_domains):
        logger.info(f"Skipping (off-domain): {url}")
        mark_url_status(url, "skipped_off_domain")
        return

    # ── Step 0b: Junk URL pattern check ──
    if is_junk_url(url):
        logger.info(f"Skipping (junk URL): {url}")
        mark_url_status(url, "skipped_junk")
        return

    # ── Step 1: Skip if recently crawled ──
    if is_recently_crawled(url):
        logger.info(f"Skipping (recently crawled): {url}")
        return

    # ── Step 2: Check robots.txt ──
    if not is_allowed_by_robots(url, domain):
        logger.info(f"Blocked by robots.txt: {url}")
        mark_url_status(url, "blocked_robots")
        return

    # ── Step 3: Rate limiting ──
    enforce_rate_limit(domain)

    # ── Step 4: Detect if this is a PDF ──
    parsed_url = urlparse(url)
    is_pdf = parsed_url.path.lower().endswith(".pdf")

    # ── Step 5: Fetch the content ──
    if is_pdf:
        result = fetch_pdf(url)
    else:
        result = fetch_page(url)

    # ── Step 6: Handle fetch result ──
    if result["status"] == "not_modified":
        logger.info(f"Not modified: {url}")
        mark_url_status(url, "crawled", touch_timestamp=True)
        return

    if result["status"] == "error":
        logger.warning(f"Fetch failed: {url} — {result.get('error')}")
        handle_fetch_error(url, result)
        return

    if result["status"] == "redirect":
        new_url = result["redirect_url"]
        logger.info(f"Redirect: {url} → {new_url}")
        # Only follow redirects that stay on allowed domains
        if allowed_domains and not is_allowed_domain(new_url, allowed_domains, exclude_domains):
            logger.info(f"Skipping redirect (off-domain target): {new_url}")
            mark_url_status(url, "skipped_off_domain")
            return
        register_and_queue_url(new_url, university_id, child_depth=depth, max_crawl_depth=max_crawl_depth)
        mark_url_status(url, "redirected")
        return

    # ── Step 7: Check if content actually changed ──
    content = result["content"]
    if isinstance(content, str):
        content_hash = hashlib.sha256(content.encode()).hexdigest()
    else:
        content_hash = hashlib.sha256(content).hexdigest()

    if is_content_unchanged(url, content_hash):
        logger.info(f"Content unchanged: {url}")
        mark_url_status(url, "crawled", touch_timestamp=True)
        return

    # ── Step 8: Store in S3 ──
    related_urls = []  # Default for PDFs (no extractable hyperlinks)

    if is_pdf:
        s3_key = store_pdf(url, university_id, domain, content, result)
        content_type = "pdf"
        content_length = len(content)
        links_found = 0
    else:
        s3_key = store_html(url, university_id, domain, content, result)
        content_type = "html"
        content_length = len(content)

        # ── Step 9: Extract and queue new links ──
        crawlable_links, related_urls = extract_links(content, url, university_id, allowed_domains, exclude_domains)
        links_found = 0
        for link_url in crawlable_links:
            if register_and_queue_url(link_url, university_id, child_depth=depth + 1, max_crawl_depth=max_crawl_depth):
                links_found += 1

        # ── Step 9b: Store backlinks (source → each target) ──
        update_incoming_links(url, related_urls)

    # ── Step 10: Update URL registry ──
    update_url_registry(
        url=url,
        status="crawled",
        content_hash=content_hash,
        s3_key=s3_key,
        etag=result.get("etag", ""),
        last_modified=result.get("last_modified", ""),
        content_length=content_length,
        links_found=links_found,
        content_type=content_type,
        links_to=related_urls,
    )

    # ── Step 11: Push to processing queue ──
    push_to_processing_queue(url, university_id, s3_key, domain, content_type)

    logger.info(
        f"Crawled: {url} | type={content_type} | "
        f"{content_length} bytes | {links_found} new links | "
        f"{len(related_urls)} related | depth={depth}"
    )

# ═════════════════════════════════════════════
# URL STATUS CHECKS
# ═════════════════════════════════════════════
def is_recently_crawled(url, freshness_hours=24):
    """Check if URL was crawled within the freshness window."""
    try:
        response = url_table.get_item(
            Key={"url": url},
            ProjectionExpression="crawl_status, last_crawled_at"
        )
        item = response.get("Item")
        if not item or item["crawl_status"] != "crawled":
            return False

        last_crawled = item.get("last_crawled_at", "never")
        if last_crawled == "never":
            return False

        last_crawled_dt = datetime.fromisoformat(last_crawled)
        age_hours = (datetime.now(timezone.utc) - last_crawled_dt).total_seconds() / 3600
        return age_hours < freshness_hours

    except Exception:
        return False


def is_content_unchanged(url, new_hash):
    """Check if content hash matches what we already have."""
    try:
        response = url_table.get_item(
            Key={"url": url},
            ProjectionExpression="content_hash"
        )
        item = response.get("Item")
        if not item:
            return False
        return item.get("content_hash", "") == new_hash
    except Exception:
        return False


# ═════════════════════════════════════════════
# ROBOTS.TXT
# ═════════════════════════════════════════════
def is_allowed_by_robots(url, domain):
    """Check URL against cached robots.txt rules."""
    path = urlparse(url).path
    try:
        cached = robots_table.get_item(Key={"domain": domain}).get("Item")
        if cached:
            for rule in cached.get("disallow_rules", []):
                if path.startswith(rule):
                    return False
    except Exception:
        pass
    return True


# ═════════════════════════════════════════════
# RATE LIMITING
# ═════════════════════════════════════════════
def enforce_rate_limit(domain):
    """
    Token bucket rate limiter using DynamoDB.

    The idea: each domain gets N tokens per second.
    Each request consumes one token. If no tokens left, wait.
    Tokens refill based on elapsed time.
    """
    max_rps = 3  # Default requests per second
    now = time.time()

    try:
        response = rate_table.update_item(
            Key={"domain": domain},
            UpdateExpression="""
                SET last_request_at = :now,
                    tokens = if_not_exists(tokens, :max_val) - :one,
                    last_refill_at = if_not_exists(last_refill_at, :now),
                    max_rps = :max_val
            """,
            ExpressionAttributeValues={
                ":now": Decimal(str(now)),
                ":one": Decimal("1"),
                ":max_val": Decimal(str(max_rps)),
            },
            ReturnValues="ALL_NEW",
        )

        item = response["Attributes"]
        tokens = float(item.get("tokens", 0))
        last_refill = float(item.get("last_refill_at", now))

        # Refill tokens if more than 1 second has passed
        elapsed = now - last_refill
        if elapsed > 1.0:
            rate_table.update_item(
                Key={"domain": domain},
                UpdateExpression="SET tokens = :max_val, last_refill_at = :now",
                ExpressionAttributeValues={
                    ":max_val": Decimal(str(max_rps)),
                    ":now": Decimal(str(now)),
                },
            )
        elif tokens <= 0:
            # No tokens available — wait
            wait_time = 1.0 / max_rps
            logger.info(f"Rate limit: waiting {wait_time:.2f}s for {domain}")
            time.sleep(wait_time)

    except Exception as e:
        # Fallback: simple sleep
        logger.warning(f"Rate limit check failed: {e}")
        time.sleep(0.3)


# ═════════════════════════════════════════════
# PAGE FETCHING
# ═════════════════════════════════════════════
def fetch_page(url):
    """
    Fetch an HTML page with conditional headers.

    Uses If-None-Match (ETag) and If-Modified-Since to avoid
    re-downloading content that hasn't changed. This saves
    bandwidth and reduces load on the university server.
    """
    headers = {"User-Agent": USER_AGENT}

    # Load stored ETag/Last-Modified for conditional request
    try:
        stored = url_table.get_item(
            Key={"url": url},
            ProjectionExpression="etag, last_modified"
        ).get("Item", {})

        if stored.get("etag"):
            headers["If-None-Match"] = stored["etag"]
        if stored.get("last_modified"):
            headers["If-Modified-Since"] = stored["last_modified"]
    except Exception:
        pass

    try:
        response = http.request(
            "GET", url,
            headers=headers,
            redirect=False,  # Handle redirects manually
            timeout=30,
        )

        # 304 — Content hasn't changed since last crawl
        if response.status == 304:
            return {"status": "not_modified"}

        # 3xx — Redirect
        if 300 <= response.status < 400:
            location = response.headers.get("Location", "")
            if location:
                return {
                    "status": "redirect",
                    "redirect_url": urljoin(url, location),
                }
            return {"status": "error", "error": "Redirect without Location header"}

        # 4xx/5xx — Error
        if response.status >= 400:
            return {
                "status": "error",
                "error": f"HTTP {response.status}",
                "status_code": response.status,
            }

        # Check content type — only process HTML
        content_type = response.headers.get("Content-Type", "")
        if "text/html" not in content_type.lower() and "application/xhtml" not in content_type.lower():
            # Might be a PDF served without .pdf extension
            if "application/pdf" in content_type.lower():
                return {
                    "status": "success",
                    "content": response.data,  # Binary
                    "content_type": "pdf",
                    "etag": response.headers.get("ETag", ""),
                    "last_modified": response.headers.get("Last-Modified", ""),
                }
            return {"status": "error", "error": f"Non-HTML content: {content_type}"}

        return {
            "status": "success",
            "content": response.data.decode("utf-8", errors="replace"),
            "content_type": "html",
            "etag": response.headers.get("ETag", ""),
            "last_modified": response.headers.get("Last-Modified", ""),
        }

    except urllib3.exceptions.MaxRetryError:
        return {"status": "error", "error": "Max retries exceeded"}
    except urllib3.exceptions.TimeoutError:
        return {"status": "error", "error": "Request timed out"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


def fetch_pdf(url):
    """
    Fetch a PDF file. Downloads the binary content.
    PDFs don't support conditional headers in practice,
    so we always download and check the content hash.
    """
    try:
        response = http.request(
            "GET", url,
            headers={"User-Agent": USER_AGENT},
            timeout=60,  # PDFs can be large, longer timeout
        )

        if response.status >= 400:
            return {
                "status": "error",
                "error": f"HTTP {response.status}",
                "status_code": response.status,
            }

        if 300 <= response.status < 400:
            location = response.headers.get("Location", "")
            if location:
                return {
                    "status": "redirect",
                    "redirect_url": urljoin(url, location),
                }

        return {
            "status": "success",
            "content": response.data,  # Raw bytes
            "content_type": "pdf",
            "etag": response.headers.get("ETag", ""),
            "last_modified": response.headers.get("Last-Modified", ""),
        }

    except Exception as e:
        return {"status": "error", "error": str(e)}


# ═════════════════════════════════════════════
# ERROR HANDLING
# ═════════════════════════════════════════════
def handle_fetch_error(url, result):
    """
    Handle fetch errors appropriately:
    - 404/410: Page is gone — mark as dead, set TTL for cleanup
    - Other errors: Increment retry count, mark as failed after 3 tries
    """
    status_code = result.get("status_code", 0)

    if status_code in (404, 410):
        # Page no longer exists
        mark_url_status(url, "dead")

        # Set TTL for auto-cleanup after 30 days
        try:
            url_table.update_item(
                Key={"url": url},
                UpdateExpression="SET #ttl = :ttl",
                ExpressionAttributeNames={"#ttl": "ttl"},
                ExpressionAttributeValues={
                    ":ttl": int(time.time()) + (30 * 86400)
                },
            )
        except Exception:
            pass

        # Push deletion event to processing queue
        # Week 3-4 pipeline will remove this from the knowledge base
        push_deletion_event(url)
        return

    # Other errors — increment retry count
    try:
        response = url_table.update_item(
            Key={"url": url},
            UpdateExpression="""
                SET retry_count = if_not_exists(retry_count, :zero) + :one,
                    crawl_status = :status,
                    last_error = :err,
                    last_error_at = :ts
            """,
            ExpressionAttributeValues={
                ":one": 1,
                ":zero": 0,
                ":status": "error",
                ":err": result.get("error", "unknown")[:200],
                ":ts": datetime.now(timezone.utc).isoformat(),
            },
            ReturnValues="ALL_NEW",
        )
        retry_count = response["Attributes"].get("retry_count", 0)
        if retry_count >= 3:
            mark_url_status(url, "failed")
            logger.warning(f"URL failed permanently after 3 retries: {url}")

    except Exception as e:
        logger.error(f"Error handling fetch error for {url}: {e}")


def push_deletion_event(url):
    """
    Notify the processing pipeline that a page has been removed.
    Week 3-4 pipeline will remove chunks/embeddings from the knowledge base.
    """
    try:
        sqs.send_message(
            QueueUrl=PROCESSING_QUEUE_URL,
            MessageBody=json.dumps({
                "url": url,
                "event_type": "page_deleted",
                "detected_at": datetime.now(timezone.utc).isoformat(),
            }),
        )
    except Exception as e:
        logger.error(f"Failed to push deletion event for {url}: {e}")


# ═════════════════════════════════════════════
# LINK EXTRACTION
# ═════════════════════════════════════════════
def extract_links(html, base_url, university_id, allowed_domains=None, exclude_domains=None):
    """
    Extract all internal links from HTML content.

    Returns (crawlable_links, related_urls):
      - crawlable_links: URLs that pass all filters (domain, extension, junk)
        and should be queued for crawling.
      - related_urls: ALL internal URLs on the page (after normalization and
        self-link removal, but before crawl filtering). This is a superset of
        crawlable_links and is stored as links_to for relationship tracking.

    allowed_domains/exclude_domains are passed from caller to avoid
    redundant config lookups.
    """
    href_pattern = re.compile(r'href=["\']([^"\']+)["\']', re.IGNORECASE)
    crawlable = set()
    all_internal = set()

    parsed_base = urlparse(base_url)
    root_domain = get_root_domain(parsed_base.netloc)
    normalized_base = normalize_url(parsed_base)

    if allowed_domains is None:
        allowed_domains = get_allowed_domains(university_id)
    if exclude_domains is None:
        exclude_domains = get_exclude_domains(university_id)

    for match in href_pattern.finditer(html):
        href = match.group(1).strip()

        # Skip non-URL hrefs
        if not href or href.startswith(("javascript:", "mailto:", "tel:", "#", "data:")):
            continue

        # Resolve relative URLs to absolute
        full_url = urljoin(base_url, href)

        # Remove URL fragments (#section)
        full_url, _ = urldefrag(full_url)

        parsed = urlparse(full_url)

        # Must be HTTP or HTTPS
        if parsed.scheme not in ("http", "https"):
            continue

        # Domain check: use allowlist if available, else fall back to root domain
        if allowed_domains:
            if not is_allowed_domain(full_url, allowed_domains, exclude_domains):
                continue
        else:
            if get_root_domain(parsed.netloc) != root_domain:
                continue

        # Normalize the URL
        normalized = normalize_url(parsed)

        # Skip self-links
        if normalized == normalized_base:
            continue

        # Track all internal links (relationship set, before crawl filtering)
        all_internal.add(normalized)

        # Check file extension — skip binary files for crawling
        path_lower = parsed.path.lower()
        if any(path_lower.endswith(ext) for ext in SKIP_EXTENSIONS):
            continue

        # Allow PDFs — they'll be handled by the PDF path
        # Allow HTML pages (no extension or .html/.htm/.php etc.)

        # Skip junk URLs (search, pagination, login, calendar, etc.)
        if is_junk_url(full_url):
            continue

        crawlable.add(normalized)

    return list(crawlable), list(all_internal)


def normalize_url(parsed):
    """
    Normalize a URL to reduce duplicates.
    - Lowercase scheme and netloc
    - Remove trailing slash (except for root)
    - Sort query parameters
    - Remove common tracking params
    """
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    path = parsed.path

    # Remove trailing slash (but keep "/" for root path)
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")

    # Sort and clean query parameters
    query = ""
    if parsed.query:
        params = sorted(parsed.query.split("&"))
        # Remove tracking parameters
        tracking_prefixes = ("utm_", "ref=", "fbclid=", "gclid=", "mc_")
        params = [p for p in params if not any(p.startswith(t) for t in tracking_prefixes)]
        if params:
            query = "?" + "&".join(params)

    return f"{scheme}://{netloc}{path}{query}"


def get_root_domain(netloc):
    """Extract root domain: 'cs.gmu.edu' → 'gmu.edu'."""
    parts = netloc.lower().split(".")
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return netloc


# ═════════════════════════════════════════════
# S3 STORAGE
# ═════════════════════════════════════════════
def store_html(url, university_id, domain, html_content, fetch_result):
    """Store raw HTML in S3. Returns the S3 key."""
    url_hash = hashlib.sha256(url.encode()).hexdigest()[:16]

    s3_key = f"raw-html/{university_id}/{domain}/{url_hash}.html"

    metadata = {
        "source-url": url[:512],  # S3 metadata has 2KB limit
        "domain": domain,
        "university-id": university_id,
        "crawled-at": datetime.now(timezone.utc).isoformat(),
        "content-hash": hashlib.sha256(html_content.encode()).hexdigest()[:32],
    }

    s3.put_object(
        Bucket=CONTENT_BUCKET,
        Key=s3_key,
        Body=html_content.encode("utf-8"),
        ContentType="text/html; charset=utf-8",
        Metadata=metadata,
    )
    return s3_key


def store_pdf(url, university_id, domain, pdf_bytes, fetch_result):
    """Store raw PDF binary in S3. Returns the S3 key."""
    url_hash = hashlib.sha256(url.encode()).hexdigest()[:16]

    s3_key = f"raw-pdf/{university_id}/{domain}/{url_hash}.pdf"

    metadata = {
        "source-url": url[:512],
        "domain": domain,
        "university-id": university_id,
        "crawled-at": datetime.now(timezone.utc).isoformat(),
        "content-hash": hashlib.sha256(pdf_bytes).hexdigest()[:32],
    }

    s3.put_object(
        Bucket=CONTENT_BUCKET,
        Key=s3_key,
        Body=pdf_bytes,
        ContentType="application/pdf",
        Metadata=metadata,
    )
    return s3_key


# ═════════════════════════════════════════════
# URL REGISTRATION AND QUEUING
# ═════════════════════════════════════════════
def get_message_group(url, domain):
    """
    Generate a message group ID from the URL path prefix.
    Creates parallel processing groups within the same domain.
    """
    parsed = urlparse(url)
    path_parts = parsed.path.strip("/").split("/")

    if path_parts and path_parts[0]:
        prefix = path_parts[0]
    else:
        prefix = "root"

    group_id = f"{domain}#{prefix}"
    return group_id[:128]


def register_and_queue_url(url, university_id, child_depth=0, max_crawl_depth=10):
    # No filtering here — callers (extract_links, process_url redirect handler)
    # already filter by domain allowlist and junk patterns before calling this.
    parsed = urlparse(url)
    domain = parsed.netloc
    url_hash = hashlib.sha256(url.encode()).hexdigest()[:16]
    now = datetime.now(timezone.utc).isoformat()
    content_type = "pdf" if parsed.path.lower().endswith(".pdf") else "html"

    try:
        url_table.put_item(
            Item={
                "url": url,
                "url_hash": url_hash,
                "university_id": university_id,
                "domain": domain,
                "path": parsed.path,
                "crawl_status": "pending",
                "discovered_at": now,
                "last_crawled_at": "never",
                "content_hash": "",
                "etag": "",
                "last_modified": "",
                "page_category": "unknown",
                "content_type": content_type,
                "s3_key": "",
                "retry_count": 0,
                "depth": child_depth,
            },
            ConditionExpression="attribute_not_exists(#u)",
            ExpressionAttributeNames={"#u": "url"},
        )

        message_group = get_message_group(url, domain)

        sqs.send_message(
            QueueUrl=CRAWL_QUEUE_URL,
            MessageBody=json.dumps({
                "url": url,
                "university_id": university_id,
                "domain": domain,
                "queued_at": now,
                "depth": child_depth,
                "max_crawl_depth": max_crawl_depth,
            }),
            MessageGroupId=message_group,
        )
        return True

    except dynamodb.meta.client.exceptions.ConditionalCheckFailedException:
        return False
    except Exception as e:
        logger.error(f"Failed to register {url}: {e}")
        return False

# ═════════════════════════════════════════════
# URL REGISTRY UPDATES
# ═════════════════════════════════════════════
def mark_url_status(url, status, touch_timestamp=False):
    """Update the crawl status of a URL."""
    update_expr = "SET crawl_status = :status"
    expr_values = {":status": status}

    if touch_timestamp:
        update_expr += ", last_crawled_at = :ts"
        expr_values[":ts"] = datetime.now(timezone.utc).isoformat()

    try:
        url_table.update_item(
            Key={"url": url},
            UpdateExpression=update_expr,
            ExpressionAttributeValues=expr_values,
        )
    except Exception as e:
        logger.error(f"Failed to update status for {url}: {e}")


def update_url_registry(url, status, content_hash, s3_key, etag,
                         last_modified, content_length, links_found,
                         content_type, links_to=None):
    """Full update after successful crawl."""
    try:
        update_expr = """
                SET crawl_status = :status,
                    content_hash = :hash,
                    s3_key = :s3key,
                    etag = :etag,
                    last_modified = :lm,
                    last_crawled_at = :ts,
                    content_length = :cl,
                    links_found = :lf,
                    content_type = :ct,
                    retry_count = :zero
        """
        expr_values = {
                ":status": status,
                ":hash": content_hash,
                ":s3key": s3_key,
                ":etag": etag,
                ":lm": last_modified,
                ":ts": datetime.now(timezone.utc).isoformat(),
                ":cl": content_length,
                ":lf": links_found,
                ":ct": content_type,
                ":zero": 0,
        }

        if links_to is not None:
            update_expr += ", links_to = :lt"
            expr_values[":lt"] = links_to[:500]  # Cap at 500 to stay under DynamoDB 400KB item limit

        url_table.update_item(
            Key={"url": url},
            UpdateExpression=update_expr,
            ExpressionAttributeValues=expr_values,
        )
    except Exception as e:
        logger.error(f"Failed to update registry for {url}: {e}")


def update_incoming_links(source_url, target_urls, max_backlinks=100):
    """
    For each target URL, append source_url to that target's linked_from list.

    Uses conditional DynamoDB updates:
    - Only updates URLs already in the registry (attribute_exists)
    - Stops appending once the target has max_backlinks entries
    - Silently skips failures (backlink tracking is supplementary)
    """
    for target_url in target_urls:
        try:
            url_table.update_item(
                Key={"url": target_url},
                UpdateExpression="SET linked_from = list_append(if_not_exists(linked_from, :empty), :new_link)",
                ConditionExpression="attribute_exists(#u) AND (attribute_not_exists(linked_from) OR size(linked_from) < :max_size)",
                ExpressionAttributeNames={"#u": "url"},
                ExpressionAttributeValues={
                    ":empty": [],
                    ":new_link": [source_url],
                    ":max_size": max_backlinks,
                },
            )
        except ClientError as e:
            # ConditionalCheckFailedException: target doesn't exist, list is full, or race condition — all expected
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                logger.debug(f"Failed to update backlink for {target_url}: {e}")
        except Exception as e:
            logger.debug(f"Failed to update backlink for {target_url}: {e}")


# ═════════════════════════════════════════════
# PROCESSING QUEUE
# ═════════════════════════════════════════════
def push_to_processing_queue(url, university_id, s3_key, domain, content_type):
    """
    Push successfully crawled content to the appropriate processing queue.
    HTML → ProcessingQueue (content cleaner)
    PDF  → PdfProcessingQueue (PDF processor)
    """
    queue_url = PDF_PROCESSING_QUEUE_URL if content_type == "pdf" else PROCESSING_QUEUE_URL
    try:
        # Extract url_hash from s3_key: raw-html/{uid}/{domain}/{hash}.html
        filename = s3_key.rsplit('/', 1)[-1]
        url_hash = filename.rsplit('.', 1)[0]

        sqs.send_message(
            QueueUrl=queue_url,
            MessageBody=json.dumps({
                "url": url,
                "university_id": university_id,
                "domain": domain,
                "s3_key": s3_key,
                "url_hash": url_hash,
                "content_type": content_type,
                "crawled_at": datetime.now(timezone.utc).isoformat(),
            }),
        )
    except Exception as e:
        logger.error(f"Failed to push to processing queue: {url}: {e}")
