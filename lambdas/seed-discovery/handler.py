"""
Seed Discovery Lambda

Discovers all URLs for a university by:
1. Fetching and parsing sitemap.xml (and nested sitemaps)
2. Discovering subdomains via Certificate Transparency logs
3. Parsing robots.txt for each domain
4. Pushing all discovered URLs to the crawl queue

Triggered by: Step Functions orchestrator
"""

import os
import json
import hashlib
import re
import time
import logging
from datetime import datetime, timezone
from urllib.parse import urlparse, urljoin
from xml.etree import ElementTree

import boto3
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
# Environment Variables (injected by SAM template)
# ─────────────────────────────────────────────
CRAWL_QUEUE_URL = os.environ["CRAWL_QUEUE_URL"]
URL_REGISTRY_TABLE = os.environ["URL_REGISTRY_TABLE"]
ROBOTS_CACHE_TABLE = os.environ["ROBOTS_CACHE_TABLE"]
CONTENT_BUCKET = os.environ["CONTENT_BUCKET"]
USER_AGENT = os.environ.get("USER_AGENT", "InfinizeBot/1.0")

url_table = dynamodb.Table(URL_REGISTRY_TABLE)
robots_table = dynamodb.Table(ROBOTS_CACHE_TABLE)

# HTTP client with retries and timeout
http = urllib3.PoolManager(
    timeout=urllib3.Timeout(connect=10, read=30),
    retries=urllib3.Retry(total=2, backoff_factor=0.5),
    headers={"User-Agent": USER_AGENT},
)

# Sitemap XML namespace
SITEMAP_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}

# ─────────────────────────────────────────────
# URL patterns to skip (junk / non-content pages)
# Same patterns used by crawler-worker for consistency.
# ─────────────────────────────────────────────
JUNK_URL_PATTERNS = [
    r'[?&](q|query|search|s)=',
    r'/search[/?]',
    r'/results[/?]',
    r'/calendar[/?].*[?&](month|year|date)=',
    r'/events[/?].*[?&](month|year|date|page)=',
    r'[?&]tribe-bar-date=',
    r'[?&]eventDisplay=',
    r'[?&]page=\d+',
    r'/page/\d+',
    r'[?&](print|format)=',
    r'/print/',
    r'/login',
    r'/signin',
    r'/logout',
    r'/sso/',
    r'/cas/',
    r'/auth/',
    r'/authorize\?',
    r'/oauth2?/',
    r'/wp-admin',
    r'/wp-login',
    r'/wp-json',
    r'/feed/?$',
    r'/xmlrpc',
    r'/cgi-bin/',
    r'[?&]C=[NMSD]',
    r'[?&]O=[AD]',
    r'[?&](utm_|fbclid|gclid)',
    r'[?&]share=',
    r'/tag/[^/]+/?$',
    r'/category/[^/]+/page/',
    r'/author/[^/]+/?$',
    r'/index\.html?$',
    r'/default\.aspx?$',
    r'[?&]hsLang=',
    r'/studentportal/',
    r'/facultyportal/',
    r'/payerportal/',
    r'/_hcms/',
    r'/hs/hsstatic/',
    r'[?&]preview=',
    r'[?&]replytocom=',
    r'[?&]ref=',
]

JUNK_PATTERNS_COMPILED = [re.compile(p, re.IGNORECASE) for p in JUNK_URL_PATTERNS]


def is_allowed_domain(url, allowed_domains, exclude_domains=None):
    """Check if URL belongs to an allowed domain and not in exclude list."""
    try:
        hostname = urlparse(url).hostname
        if not hostname:
            return False
        hostname = hostname.lower()
    except Exception:
        return False

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
    """
    Entry point. Called by Step Functions.

    Expected event:
    {
        "university_id": "phc",
        "refresh_mode": "full" | "incremental" | "domain",
        "domain_filter": "www.phc.edu"  (optional)
    }
    """
    university_id = event["university_id"]
    refresh_mode = event.get("refresh_mode", "full")
    domain_filter = event.get("domain_filter", "")

    logger.info(f"Seed discovery starting: university={university_id}, mode={refresh_mode}")

    # Load config for this university
    config = load_university_config(university_id)
    if not config:
        logger.error(f"No config found for {university_id}")
        return {"urls_discovered": 0, "subdomains_found": 0, "sitemaps_parsed": 0}

    root_domain = config["root_domain"]
    seed_urls = config["seed_urls"]
    crawl_config = config.get("crawl_config", {})
    exclude_domains = crawl_config.get("exclude_domains", [])
    allowed_domain_patterns = crawl_config.get("allowed_domain_patterns", [])
    max_crawl_depth = crawl_config.get("max_crawl_depth", 10)

    # ── Step 1: Discover subdomains via CT logs ──
    discover_via_ct = config.get("crawl_config", {}).get("discover_subdomains_via_ct", True)
    if discover_via_ct:
        subdomains = discover_subdomains(root_domain)
    else:
        subdomains = []
    logger.info(f"Found {len(subdomains)} subdomains via CT logs")

    # Merge seed URL domains with discovered subdomains
    all_domains = set()
    for url in seed_urls:
        all_domains.add(urlparse(url).netloc)
    for sub in subdomains:
        all_domains.add(sub)

    # Remove excluded domains
    all_domains = {d for d in all_domains if d not in exclude_domains}

    # Apply domain filter if this is a domain-specific refresh
    if domain_filter:
        all_domains = {d for d in all_domains if domain_filter in d}

    logger.info(f"Total domains to process: {len(all_domains)}")

    # ── Step 2: Fetch robots.txt for each domain ──
    for domain in all_domains:
        fetch_and_cache_robots(domain)

    # ── Step 3: Parse sitemaps and queue URLs ──
    total_urls = 0
    total_sitemaps = 0

    skipped_junk = 0
    skipped_domain = 0

    for domain in all_domains:
        urls_found, sitemap_count = parse_sitemaps(domain)
        total_sitemaps += sitemap_count

        for url in urls_found:
            # Domain allowlist check
            if allowed_domain_patterns and not is_allowed_domain(url, allowed_domain_patterns, exclude_domains):
                skipped_domain += 1
                continue
            # Junk URL check
            if is_junk_url(url):
                skipped_junk += 1
                continue
            if is_url_allowed(url, domain):
                if register_url(url, university_id, domain):
                    push_to_crawl_queue(url, university_id, domain, max_crawl_depth)
                    total_urls += 1

    # Also queue the seed URLs themselves
    for url in seed_urls:
        parsed = urlparse(url)
        if not domain_filter or domain_filter in parsed.netloc:
            if parsed.netloc not in exclude_domains:
                if register_url(url, university_id, parsed.netloc):
                    push_to_crawl_queue(url, university_id, parsed.netloc, max_crawl_depth)
                    total_urls += 1

    logger.info(f"Filtering stats: {skipped_domain} off-domain, {skipped_junk} junk URLs skipped")

    logger.info(
        f"Seed discovery complete: {total_urls} URLs queued, "
        f"{len(all_domains)} domains, {total_sitemaps} sitemaps parsed"
    )

    return {
        "urls_discovered": total_urls,
        "subdomains_found": len(all_domains),
        "sitemaps_parsed": total_sitemaps,
    }


# ═════════════════════════════════════════════
# CONFIG LOADING
# ═════════════════════════════════════════════
def load_university_config(university_id):
    """
    Load university config from S3.
    Falls back to embedded config for first-time bootstrapping.
    """
    try:
        response = s3.get_object(
            Bucket=CONTENT_BUCKET,
            Key=f"configs/{university_id}.json"
        )
        config = json.loads(response["Body"].read().decode("utf-8"))
        logger.info(f"Loaded config from S3 for {university_id}")
        return config
    except Exception:
        logger.warning(f"No S3 config for {university_id}, using embedded config")
        return get_embedded_config(university_id)


def get_embedded_config(university_id):
    """
    Hardcoded fallback config.
    Runs on the very first crawl before any config exists in S3.
    After first deploy, upload proper config to S3.
    """
    configs = {
    "university_id": "phc",
    "name": "Patrick Henry College",
    "root_domain": "phc.edu",

    "seed_urls": [
        "https://www.phc.edu",
        "https://www.phc.edu/admissions",
        "https://www.phc.edu/academics",
        "https://www.phc.edu/majors",
        "https://www.phc.edu/phc-student-life",
        "https://www.phc.edu/scholarship",
        "https://www.phc.edu/distance-learning",
        "https://www.phc.edu/alumni",
        "https://www.phc.edu/about",
        "https://www.phc.edu/contact-us",
        "https://www.phc.edu/cost-of-attendance",
        "https://www.phc.edu/applying-to-phc",
        "https://www.phc.edu/faculty",
        "https://www.phc.edu/news"
    ],

    "pdf_sources": [
        "https://www.phc.edu",
        "https://f.hubspotusercontent00.net/hubfs/1718959/"
    ],

    "crawl_config": {
        "max_concurrent_requests": 10,
        "requests_per_second_per_domain": 3,
        "max_pages": 5000,
        "request_timeout_seconds": 30,
        "max_crawl_depth": 5,

        "exclude_extensions": [
            ".zip", ".gz", ".tar",
            ".mp4", ".mp3", ".avi", ".mov", ".wmv",
            ".jpg", ".jpeg", ".png", ".gif", ".svg", ".ico", ".webp",
            ".css", ".js", ".woff", ".woff2", ".ttf", ".eot",
            ".xls", ".xlsx", ".ppt", ".pptx", ".doc", ".docx"
        ],

        "pdf_extensions": [".pdf"],

        "exclude_path_patterns": [
            ".*/wp-admin/.*",
            ".*/wp-login.*",
            ".*/wp-json/.*",
            ".*/login.*",
            ".*/logout.*",
            ".*/search\\?.*",
            ".*/print/.*",
            ".*/feed/.*",
            ".*/tag/.*",
            ".*/author/.*",
            ".*\\?utm_.*",
            ".*\\?ref=.*",
            ".*\\?replytocom=.*",
            ".*\\?preview=.*",
            ".*\\?nonce=.*",
            ".*/page/\\d+.*",
            ".*/_hcms/.*",
            ".*/hs-fs/.*",
            ".*/hs/hsstatic/.*",
            ".*/hubfs/(?!.*\\.pdf$).*",
            ".*/cs/.*",
            ".*/studentportal/.*",
            ".*/facultyportal/.*",
            ".*/payerportal/.*"
        ],

        "exclude_domains": [
            "share.phc.edu"
        ],

        "include_subdomains": True,
        "discover_subdomains_via_ct": True
    },

    "freshness_windows_days": {
        "admissions": 7,
        "financial_aid": 7,
        "scholarships": 7,
        "academic_programs": 14,
        "course_catalog": 14,
        "student_services": 14,
        "housing_dining": 14,
        "campus_life": 30,
        "spiritual_life": 30,
        "athletics": 7,
        "faculty_staff": 30,
        "library": 30,
        "distance_learning": 14,
        "career_services": 30,
        "alumni": 30,
        "about": 60,
        "policies": 60,
        "accreditation": 60,
        "events": 3,
        "news": 3,
        "campus_safety": 14,
        "international_students": 14,
        "student_organizations": 30,
        "commencement": 14,
        "orientation": 14,
        "registration": 7,
        "academic_calendar": 7,
        "tuition_fees": 14,
        "unknown": 14,
        "other": 30
    },

    "rate_limits": {
        "default_rps": 3,
        "subdomain_overrides": {}
    }
}
    return configs.get(university_id)


# ═════════════════════════════════════════════
# SUBDOMAIN DISCOVERY
# ═════════════════════════════════════════════
def discover_subdomains(root_domain):
    """
    Query Certificate Transparency logs to find all subdomains.

    CT logs record every SSL certificate ever issued. Since most
    subdomains have SSL certs, this catches subdomains that
    aren't linked from the main site.

    Example: crt.sh/?q=%.phc.edu returns all certs for *.phc.edu
    """
    subdomains = set()
    try:
        url = f"https://crt.sh/?q=%.{root_domain}&output=json"
        response = http.request("GET", url, timeout=30)

        if response.status == 200:
            certs = json.loads(response.data.decode("utf-8"))
            for cert in certs:
                name_value = cert.get("name_value", "")
                for name in name_value.split("\n"):
                    name = name.strip().lower()
                    if name.startswith("*."):
                        name = name[2:]
                    if name.endswith(f".{root_domain}") or name == root_domain:
                        # Skip wildcard-only entries and duplicates
                        if "*" not in name:
                            subdomains.add(name)

    except Exception as e:
        logger.warning(f"CT log lookup failed for {root_domain}: {e}")

    return list(subdomains)


# ═════════════════════════════════════════════
# ROBOTS.TXT
# ═════════════════════════════════════════════
def fetch_and_cache_robots(domain):
    """
    Fetch robots.txt for a domain and cache parsed rules
    in DynamoDB with a 24-hour TTL.
    """
    try:
        # Check if we already have a fresh cache
        cached = robots_table.get_item(Key={"domain": domain}).get("Item")
        if cached and float(cached.get("ttl", 0)) > time.time():
            return

        url = f"https://{domain}/robots.txt"
        response = http.request("GET", url, timeout=10)

        disallow_rules = []
        crawl_delay = None

        if response.status == 200:
            content = response.data.decode("utf-8", errors="ignore")
            current_agent = None

            for line in content.split("\n"):
                line = line.strip()

                # Skip comments and empty lines
                if not line or line.startswith("#"):
                    continue

                if line.lower().startswith("user-agent:"):
                    agent = line.split(":", 1)[1].strip()
                    current_agent = agent
                elif line.lower().startswith("disallow:"):
                    if current_agent in ("*", "InfinizeBot"):
                        path = line.split(":", 1)[1].strip()
                        if path:
                            disallow_rules.append(path)
                elif line.lower().startswith("crawl-delay:"):
                    if current_agent in ("*", "InfinizeBot"):
                        try:
                            crawl_delay = int(line.split(":", 1)[1].strip())
                        except ValueError:
                            pass

        # Cache in DynamoDB
        robots_table.put_item(Item={
            "domain": domain,
            "disallow_rules": disallow_rules,
            "crawl_delay": crawl_delay if crawl_delay else 0,
            "ttl": int(time.time()) + 86400,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        })

        logger.info(f"Cached robots.txt for {domain}: {len(disallow_rules)} disallow rules")

    except Exception as e:
        logger.warning(f"robots.txt fetch failed for {domain}: {e}")
        # Cache empty rules to prevent hammering on retry
        robots_table.put_item(Item={
            "domain": domain,
            "disallow_rules": [],
            "crawl_delay": 0,
            "ttl": int(time.time()) + 3600,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        })


def is_url_allowed(url, domain):
    """Check if a URL is allowed by the cached robots.txt rules."""
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
# SITEMAP PARSING
# ═════════════════════════════════════════════
def parse_sitemaps(domain):
    """
    Try common sitemap locations and parse all URLs.
    Handles nested sitemap index files recursively.
    Returns (list_of_urls, number_of_sitemaps_parsed).
    """
    all_urls = []
    total_sitemaps = 0

    sitemap_locations = [
        f"https://{domain}/sitemap.xml",
        f"https://{domain}/sitemap_index.xml",
        f"https://{domain}/sitemap/sitemap.xml",
    ]

    for sitemap_url in sitemap_locations:
        urls, count = _parse_sitemap_recursive(sitemap_url, depth=0)
        all_urls.extend(urls)
        total_sitemaps += count

    all_urls = list(set(all_urls))
    logger.info(f"Sitemaps for {domain}: {total_sitemaps} parsed, {len(all_urls)} URLs")
    return all_urls, total_sitemaps


def _parse_sitemap_recursive(sitemap_url, depth):
    """
    Parse a single sitemap. If it's a sitemap index,
    recursively parse each child sitemap.
    """
    if depth > 5:
        return [], 0

    urls = []
    sitemaps_parsed = 0

    try:
        response = http.request("GET", sitemap_url, timeout=15)
        if response.status != 200:
            return [], 0

        content = response.data.decode("utf-8", errors="ignore")
        root = ElementTree.fromstring(content)
        sitemaps_parsed = 1

        # Check if this is a sitemap INDEX
        sitemap_entries = root.findall(".//sm:sitemap/sm:loc", SITEMAP_NS)
        if sitemap_entries:
            for entry in sitemap_entries:
                child_url = entry.text.strip()
                child_urls, child_count = _parse_sitemap_recursive(child_url, depth + 1)
                urls.extend(child_urls)
                sitemaps_parsed += child_count
        else:
            # Regular sitemap — extract page URLs
            url_entries = root.findall(".//sm:url/sm:loc", SITEMAP_NS)
            if not url_entries:
                url_entries = root.findall(".//url/loc")

            for entry in url_entries:
                urls.append(entry.text.strip())

    except ElementTree.ParseError:
        logger.warning(f"XML parse error: {sitemap_url}")
    except Exception as e:
        logger.warning(f"Sitemap fetch failed {sitemap_url}: {e}")

    return urls, sitemaps_parsed


# ═════════════════════════════════════════════
# URL REGISTRATION AND QUEUING
# ═════════════════════════════════════════════
def register_url(url, university_id, domain):
    """
    Register a new URL in DynamoDB.
    Returns True if newly discovered, False if already known.

    Uses conditional put — if the URL already exists,
    the write fails silently and returns False.
    """
    url_hash = hashlib.sha256(url.encode()).hexdigest()[:16]
    now = datetime.now(timezone.utc).isoformat()
    parsed = urlparse(url)

    # Detect content type from URL
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
            },
            ConditionExpression="attribute_not_exists(#u)",
            ExpressionAttributeNames={"#u": "url"},
        )
        return True

    except dynamodb.meta.client.exceptions.ConditionalCheckFailedException:
        return False
    except Exception as e:
        logger.error(f"Failed to register {url}: {e}")
        return False


def get_message_group(url, domain):
    """
    Generate a message group ID from the URL path prefix.
    This creates multiple parallel groups within the same domain,
    allowing SQS to process them concurrently.

    Example:
      /admissions/apply → www.phc.edu#admissions
      /news/article-1   → www.phc.edu#news
      /                  → www.phc.edu#root
    """
    parsed = urlparse(url)
    path_parts = parsed.path.strip("/").split("/")

    if path_parts and path_parts[0]:
        prefix = path_parts[0]
    else:
        prefix = "root"

    # FIFO message group IDs have a 128 char limit
    group_id = f"{domain}#{prefix}"
    return group_id[:128]

def push_to_crawl_queue(url, university_id, domain, max_crawl_depth):
    """
    Push a URL to the SQS FIFO crawl queue.

    MessageGroupId = domain name, so SQS processes
    URLs from the same domain sequentially (helps rate limiting).
    """
    try:
        message_group = get_message_group(url, domain)
        sqs.send_message(
            QueueUrl=CRAWL_QUEUE_URL,
            MessageBody=json.dumps({
                "url": url,
                "university_id": university_id,
                "domain": domain,
                "queued_at": datetime.now(timezone.utc).isoformat(),
                "max_crawl_depth": max_crawl_depth
            }),
            MessageGroupId=message_group,
        )
    except Exception as e:
        logger.error(f"Failed to queue {url}: {e}")
