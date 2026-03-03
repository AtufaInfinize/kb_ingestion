"""
University Crawler Dashboard — Streamlit App

Run: streamlit run dashboard.py
"""

import re
import json
import time
import requests
import streamlit as st

# ═══════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════
DEFAULT_API_BASE = "https://9mwsknkorc.execute-api.us-east-1.amazonaws.com/dev"

VALID_CATEGORIES = [
    'admissions', 'financial_aid', 'academic_programs', 'course_catalog',
    'student_services', 'housing_dining', 'campus_life', 'athletics',
    'faculty_staff', 'library', 'it_services', 'policies',
    'events', 'news', 'about', 'careers', 'alumni', 'other', 'low_value',
]

CATEGORY_LABELS = {
    'academic_programs': 'Academic Programs', 'admissions': 'Admissions',
    'financial_aid': 'Financial Aid', 'course_catalog': 'Course Catalog',
    'student_services': 'Student Services', 'housing_dining': 'Housing & Dining',
    'campus_life': 'Campus Life', 'athletics': 'Athletics',
    'faculty_staff': 'Faculty & Staff', 'library': 'Library',
    'it_services': 'IT Services', 'policies': 'Policies',
    'events': 'Events', 'news': 'News', 'about': 'About',
    'careers': 'Careers', 'alumni': 'Alumni', 'other': 'Other',
    'low_value': 'Low Value',
}

UID_PATTERN = re.compile(r'^[a-z][a-z0-9_-]*$')


# ═══════════════════════════════════════════
# API HELPER
# ═══════════════════════════════════════════
def api_base():
    return st.session_state.get("api_base", DEFAULT_API_BASE).rstrip("/")


def api_get(path):
    resp = requests.get(f"{api_base()}{path}", timeout=30)
    resp.raise_for_status()
    return resp.json()


def api_post(path, body=None):
    resp = requests.post(f"{api_base()}{path}", json=body or {}, timeout=30)
    if not resp.ok:
        try:
            detail = resp.json().get("error", resp.text)
        except Exception:
            detail = resp.text
        raise RuntimeError(f"{resp.status_code}: {detail}")
    cached_get.clear()
    return resp.json()


def api_delete(path, body=None):
    resp = requests.delete(f"{api_base()}{path}", json=body or {}, timeout=30)
    if not resp.ok:
        try:
            detail = resp.json().get("error", resp.text)
        except Exception:
            detail = resp.text
        raise RuntimeError(f"{resp.status_code}: {detail}")
    cached_get.clear()
    return resp.json()


@st.cache_data(ttl=15, show_spinner=False)
def cached_get(path):
    """Cached GET — avoids redundant API calls across tabs on the same render."""
    return api_get(path)


def has_running_pipeline(university_id):
    """Check if there's a running pipeline for this university. Returns job_id or None."""
    try:
        jobs_data = cached_get(f"/v1/universities/{university_id}/pipeline?limit=5")
        for j in jobs_data.get("jobs", []):
            if j.get("overall_status") == "running":
                return j["job_id"]
    except Exception:
        pass
    return None


# ═══════════════════════════════════════════
# PAGE CONFIG
# ═══════════════════════════════════════════
st.set_page_config(page_title="University Crawler Dashboard", layout="wide")
st.title("University Crawler Dashboard")

# ═══════════════════════════════════════════
# SIDEBAR — API + University selection
# ═══════════════════════════════════════════
with st.sidebar:
    st.header("Configuration")
    st.text_input("API Base URL", value=DEFAULT_API_BASE, key="api_base")

    st.divider()
    st.subheader("University")

    # Load universities
    try:
        unis = cached_get("/v1/universities").get("universities", [])
    except Exception as e:
        st.error(f"Failed to load universities: {e}")
        unis = []

    uni_options = {u["university_id"]: u["name"] for u in unis}
    if not uni_options:
        uni_options = {"phc": "Patrick Henry College"}

    # Default to PHC
    uni_ids = list(uni_options.keys())
    default_idx = uni_ids.index("phc") if "phc" in uni_ids else 0

    creating_new = st.session_state.get("creating_new", False)

    selected_uid = st.selectbox(
        "Select University",
        options=uni_ids,
        index=default_idx,
        format_func=lambda x: f"{uni_options.get(x, x)} ({x})",
        key="selected_uid",
        disabled=creating_new,
    )

    if st.button("+ New University", use_container_width=True):
        st.session_state["creating_new"] = True
        st.rerun()

    if creating_new:
        st.caption(":orange[Creating new university — fill in the Setup tab]")
        if st.button("Cancel New", use_container_width=True):
            st.session_state["creating_new"] = False
            st.rerun()

# ═══════════════════════════════════════════
# Detect university switch → clear stale state
# ═══════════════════════════════════════════
uid = st.session_state.get("selected_uid", "phc")
prev_uid = st.session_state.get("_prev_uid")
if prev_uid is not None and prev_uid != uid:
    st.session_state.pop("expanded_cat", None)
    st.session_state.pop("active_job_id", None)
    st.session_state.pop("confirm_full_recrawl", None)
st.session_state["_prev_uid"] = uid

# Load existing config once (reused by Setup, Ingestion, Maintenance)
existing_config = {}
if not creating_new:
    try:
        existing_config = cached_get(f"/v1/universities/{uid}/config")
    except Exception:
        pass

# On university select: show notification if pages changed since last crawl
if not creating_new:
    try:
        _stats = cached_get(f"/v1/universities/{uid}/stats")
        _pending_sync = _stats.get("pending_kb_sync", False)
        _pages_changed = _stats.get("pages_changed", 0)
        if _pending_sync and _pages_changed > 0:
            st.sidebar.warning(
                f"{_pages_changed:,} page(s) changed since last crawl — "
                "go to the **Ingestion** tab and run **KB Sync**."
            )
    except Exception:
        pass

# ═══════════════════════════════════════════
# TABS
# ═══════════════════════════════════════════
tab_setup, tab_crawling, tab_review, tab_ingestion, tab_maintenance = st.tabs(
    ["1. Setup", "2. Crawling", "3. Review", "4. Ingestion", "5. Maintenance"]
)


# ═══════════════════════════════════════════
# TAB 1 — SETUP
# ═══════════════════════════════════════════
with tab_setup:
    if creating_new:
        st.header("New University")
        st.caption("Fill in the details below to configure a new university for crawling.")
    else:
        st.header(f"Configuration — {existing_config.get('name', uid)}")

    with st.form("config_form"):
        col1, col2 = st.columns(2)
        with col1:
            cfg_name = st.text_input(
                "University Name",
                value="" if creating_new else existing_config.get("name", ""),
            )
            cfg_domain = st.text_input(
                "Root Domain",
                value="" if creating_new else existing_config.get("root_domain", ""),
                placeholder="example.edu",
            )
        with col2:
            cfg_id = st.text_input(
                "University ID (slug)",
                value="" if creating_new else existing_config.get("university_id", uid),
                disabled=not creating_new,
                help="Lowercase letters, numbers, hyphens only. Cannot be changed after creation.",
            )
            cfg_depth = st.number_input(
                "Max Crawl Depth",
                value=5 if creating_new else existing_config.get("crawl_config", {}).get("max_crawl_depth", 5),
                min_value=1, max_value=10,
            )

        cfg_seeds = st.text_area(
            "Seed URLs (one per line)",
            value="" if creating_new else "\n".join(existing_config.get("seed_urls", [])),
            height=150,
            placeholder="https://www.example.edu\nhttps://www.example.edu/admissions\nhttps://www.example.edu/academics",
        )

        with st.expander("Advanced Configuration"):
            adv_col1, adv_col2 = st.columns(2)
            with adv_col1:
                cfg_rps = st.number_input(
                    "Rate Limit (requests/sec/domain)",
                    value=3 if creating_new else existing_config.get("rate_limits", {}).get("default_rps", 3),
                    min_value=1, max_value=20,
                )
            with adv_col2:
                cfg_kb_id = st.text_input(
                    "Knowledge Base ID",
                    value="" if creating_new else existing_config.get("kb_config", {}).get("knowledge_base_id", ""),
                )
                cfg_ds_ids = st.text_input(
                    "Data Source IDs (comma-separated)",
                    value="" if creating_new else ", ".join(existing_config.get("kb_config", {}).get("data_source_ids", [])),
                )

            cfg_exclude_domains = st.text_area(
                "Exclude Domains (one per line)",
                value="" if creating_new else "\n".join(existing_config.get("crawl_config", {}).get("exclude_domains", [])),
                height=80,
            )
            cfg_exclude_paths = st.text_area(
                "Exclude Path Patterns (regex, one per line)",
                value="" if creating_new else "\n".join(existing_config.get("crawl_config", {}).get("exclude_path_patterns", [])),
                height=80,
            )

        submit_col1, submit_col2 = st.columns(2)
        with submit_col1:
            save_btn = st.form_submit_button(
                "Create University" if creating_new else "Save Configuration",
                type="secondary", use_container_width=True,
            )
        with submit_col2:
            crawl_btn = st.form_submit_button(
                "Create & Start Full Crawl" if creating_new else "Save & Start Full Crawl",
                type="primary", use_container_width=True,
            )

    if save_btn or crawl_btn:
        target_uid = cfg_id.strip() if creating_new else uid
        validation_errors = []

        if creating_new and not target_uid:
            validation_errors.append("University ID is required.")
        elif creating_new and not UID_PATTERN.match(target_uid):
            validation_errors.append(
                "University ID must start with a lowercase letter and contain "
                "only lowercase letters, numbers, underscores, and hyphens."
            )
        if creating_new and target_uid in uni_options:
            validation_errors.append(
                f"University **{target_uid}** already exists. Select it from the dropdown instead."
            )
        if not cfg_name.strip():
            validation_errors.append("University Name is required.")
        if not cfg_domain.strip():
            validation_errors.append("Root Domain is required.")

        seeds = [s.strip() for s in cfg_seeds.split("\n") if s.strip()]
        if not seeds:
            validation_errors.append("At least one Seed URL is required.")

        if validation_errors:
            for err in validation_errors:
                st.error(err)
        else:
            exc_domains = [s.strip() for s in cfg_exclude_domains.split("\n") if s.strip()]
            exc_paths = [s.strip() for s in cfg_exclude_paths.split("\n") if s.strip()]
            ds_ids = [s.strip() for s in cfg_ds_ids.split(",") if s.strip()]

            config_body = {
                "name": cfg_name,
                "root_domain": cfg_domain,
                "seed_urls": seeds,
                "crawl_config": {
                    "max_crawl_depth": cfg_depth,
                    "requests_per_second_per_domain": cfg_rps,
                    "exclude_domains": exc_domains,
                    "exclude_path_patterns": exc_paths,
                },
                "rate_limits": {"default_rps": cfg_rps},
            }
            if cfg_kb_id:
                config_body["kb_config"] = {
                    "knowledge_base_id": cfg_kb_id,
                    "data_source_ids": ds_ids,
                }

            try:
                api_post(f"/v1/universities/{target_uid}/config", config_body)
                st.success("Configuration saved!")

                if crawl_btn:
                    running = has_running_pipeline(target_uid)
                    if running:
                        st.warning(
                            f"Pipeline already running: `{running}`. "
                            "Wait for it to finish or check the Crawling tab."
                        )
                    else:
                        result = api_post(
                            f"/v1/universities/{target_uid}/pipeline",
                            {"refresh_mode": "full"},
                        )
                        st.session_state["active_job_id"] = result.get("job_id")
                        st.success(f"Pipeline started: {result.get('job_id')}")

                # Switch to the new university after creation
                if creating_new:
                    st.session_state["creating_new"] = False
                    st.session_state["selected_uid"] = target_uid
                    st.rerun()

            except Exception as e:
                st.error(f"Error: {e}")


# ═══════════════════════════════════════════
# TAB 2 — CRAWLING
# ═══════════════════════════════════════════
with tab_crawling:
    st.header("Pipeline Progress")

    if creating_new:
        st.info("Create a university configuration first in the Setup tab.")
    else:
        # Job selector
        try:
            jobs_data = cached_get(f"/v1/universities/{uid}/pipeline?limit=10")
            jobs_list = jobs_data.get("jobs", [])
        except Exception:
            jobs_list = []

        active_job = st.session_state.get("active_job_id")

        if jobs_list:
            job_ids = [j["job_id"] for j in jobs_list]
            default_job_idx = job_ids.index(active_job) if active_job in job_ids else 0
            selected_job = st.selectbox("Select Job", options=job_ids, index=default_job_idx)
        elif active_job:
            selected_job = active_job
            st.info(f"Tracking job: {active_job}")
        else:
            selected_job = None
            st.info("No pipeline jobs yet. Start a crawl from the Setup or Maintenance tab.")

        if selected_job:
            try:
                job = api_get(f"/v1/universities/{uid}/pipeline/{selected_job}")
            except Exception as e:
                st.error(f"Failed to load job: {e}")
                job = None

            if job:
                status = job.get("overall_status", "unknown")

                col_refresh, col_auto = st.columns([1, 3])
                with col_refresh:
                    st.button("Refresh", key="poll_refresh")
                with col_auto:
                    auto_refresh = st.checkbox("Auto-refresh (5s)", value=False)

                if status == "completed":
                    st.success(f"Pipeline **{status}** — Job: `{selected_job}`")
                elif status == "failed":
                    st.error(f"Pipeline **{status}** — Job: `{selected_job}`")
                else:
                    st.info(f"Pipeline **{status}** — Job: `{selected_job}`")

                # Stage progress bars
                stages = [
                    ("crawl_stage", "Crawling"),
                    ("clean_stage", "Content Cleaning"),
                    ("classify_stage", "Classification"),
                ]

                for key, label in stages:
                    stage = job.get(key, {})
                    s_status = stage.get("status", "pending")
                    total = int(stage.get("total", 0) or 0)
                    completed = int(stage.get("completed", 0) or 0)
                    failed = int(stage.get("failed", 0) or 0)
                    queue = stage.get("queue", {})
                    pct = completed / total if total > 0 else (1.0 if s_status == "completed" else 0.0)

                    col_label, col_status = st.columns([3, 1])
                    with col_label:
                        st.markdown(f"**{label}**")
                    with col_status:
                        if s_status == "completed":
                            st.markdown(":green[Completed]")
                        elif s_status == "running":
                            st.markdown(":blue[Running]")
                        else:
                            st.markdown(":gray[Pending]")

                    st.progress(min(pct, 1.0))

                    metrics_cols = st.columns(4)
                    with metrics_cols[0]:
                        st.metric("Completed", f"{completed:,}")
                    with metrics_cols[1]:
                        st.metric("Total", f"{total:,}" if total else "—")
                    with metrics_cols[2]:
                        st.metric("Queue", f"{queue.get('available', 0)} + {queue.get('in_flight', 0)} in-flight")
                    with metrics_cols[3]:
                        st.metric("Failed", f"{failed:,}" if failed else "0")

                    st.divider()

                # Data changed notification — shown when pipeline is complete
                if status == "completed":
                    classify = job.get("classify_stage", {})
                    pages_changed = int(classify.get("total", 0) or 0)
                    batch_jobs    = classify.get("batch_jobs", [])

                    if pages_changed > 0 and batch_jobs:
                        st.info(
                            f"{pages_changed:,} page(s) were new or had content changes — "
                            "batch classification job submitted. Run KB Sync after it completes."
                        )
                    elif pages_changed > 0 and not batch_jobs:
                        st.info(
                            f"{pages_changed:,} page(s) needed classification but were already "
                            "up to date (no new batch job created)."
                        )
                    else:
                        st.success("No content changes detected — all pages are up to date.")

                # Auto-refresh only while running
                if auto_refresh and status == "running":
                    time.sleep(5)
                    st.rerun()


# ═══════════════════════════════════════════
# TAB 3 — REVIEW
# ═══════════════════════════════════════════
with tab_review:
    st.header("Categories")

    if creating_new:
        st.info("Create a university configuration first in the Setup tab.")
    else:
        try:
            _sync = cached_get(f"/v1/universities/{uid}/sync-status")
            if _sync.get("sync_needed") and _sync.get("message"):
                st.warning(f"{_sync['message']} Go to the **Ingestion** tab and run **KB Sync**.")
            elif _sync.get("pipeline_running") and _sync.get("message"):
                st.info(_sync["message"])
        except Exception:
            pass

        if st.button("Refresh Categories", key="refresh_cats"):
            cached_get.clear()
            st.rerun()

        try:
            cat_data = cached_get(f"/v1/universities/{uid}/categories")
            categories = cat_data.get("categories", [])
        except Exception as e:
            st.error(f"Failed to load categories: {e}")
            categories = []

        if not categories:
            st.info("No categories yet. Run a crawl and classification first.")
        else:
            total_pages = sum(c["count"] for c in categories)
            st.caption(f"Total classified pages: **{total_pages:,}** across **{len(categories)}** categories")

            cols = st.columns(min(len(categories), 5))
            for i, cat in enumerate(categories):
                with cols[i % len(cols)]:
                    label = cat.get("label", cat["category"])
                    count = cat["count"]
                    if st.button(f"**{count:,}**\n\n{label}", key=f"cat_{cat['category']}", use_container_width=True):
                        st.session_state["expanded_cat"] = cat["category"]

            st.divider()

            # Expanded category detail — validate it still exists for this university
            expanded = st.session_state.get("expanded_cat")
            if expanded:
                cat_info = next((c for c in categories if c["category"] == expanded), None)
                if not cat_info:
                    # Stale: category doesn't exist for this university
                    st.session_state.pop("expanded_cat", None)
                    expanded = None

            if expanded:
                cat_label = cat_info["label"] if cat_info else expanded
                st.subheader(f"{cat_label}")

                action_col1, action_col2, action_col3 = st.columns(3)

                with action_col1:
                    with st.popover("Rename Category"):
                        with st.form(key=f"rename_form_{expanded}"):
                            new_cat = st.selectbox(
                                "Move all pages to:",
                                [c for c in VALID_CATEGORIES if c != expanded],
                                format_func=lambda x: CATEGORY_LABELS.get(x, x),
                            )
                            submitted = st.form_submit_button("Confirm Rename", type="primary")
                        if submitted:
                            try:
                                result = api_post(
                                    f"/v1/universities/{uid}/categories/{expanded}/rename",
                                    {"new_category": new_cat},
                                )
                                st.success(f"Moved {result.get('pages_moved', 0)} pages to {new_cat}")

                                st.session_state["expanded_cat"] = None
                                st.rerun()
                            except Exception as e:
                                st.error(f"Rename failed: {e}")

                with action_col2:
                    with st.popover("Delete Category"):
                        st.warning("All pages will be marked as **excluded** and removed from KB ingestion.")
                        if st.button("Confirm Delete", type="primary", key="do_delete"):
                            try:
                                result = api_post(f"/v1/universities/{uid}/categories/{expanded}/delete", {})
                                st.success(f"Excluded {result.get('pages_deleted', 0)} pages")

                                st.session_state["expanded_cat"] = None
                                st.rerun()
                            except Exception as e:
                                st.error(f"Delete failed: {e}")

                with action_col3:
                    with st.popover("Upload Media"):
                        uploaded_file = st.file_uploader(
                            "Choose a file",
                            type=["pdf", "jpg", "jpeg", "png", "gif", "webp", "bmp",
                                  "mp3", "wav", "ogg", "flac", "aac", "m4a",
                                  "mp4", "avi", "mov", "mkv", "webm"],
                            key="media_upload",
                        )
                        if uploaded_file and st.button("Upload", key="do_upload"):
                            try:
                                content_type = uploaded_file.type or "application/octet-stream"
                                url_data = api_post(
                                    f"/v1/universities/{uid}/categories/{expanded}/media/upload-url",
                                    {"filename": uploaded_file.name, "content_type": content_type},
                                )
                                requests.put(
                                    url_data["upload_url"],
                                    data=uploaded_file.getvalue(),
                                    headers={"Content-Type": content_type},
                                    timeout=60,
                                )
                                api_post(
                                    f"/v1/universities/{uid}/categories/{expanded}/media/process",
                                    {
                                        "filename": url_data["filename"],
                                        "s3_key": url_data["s3_key"],
                                        "content_type": content_type,
                                    },
                                )
                                st.success(f"Uploaded: {uploaded_file.name}")

                            except Exception as e:
                                st.error(f"Upload failed: {e}")

                with st.expander("Add URLs"):
                    add_urls_input = st.text_area(
                        "URLs (one per line or comma-separated)",
                        key="add_urls_text",
                        height=80,
                    )
                    trigger_crawl = st.checkbox("Trigger crawl for new URLs", value=True, key="trigger_crawl")
                    if st.button("Add URLs", key="do_add_urls"):
                        urls = [
                            u.strip()
                            for u in add_urls_input.replace(",", "\n").split("\n")
                            if u.strip()
                        ]
                        if urls:
                            try:
                                result = api_post(
                                    f"/v1/universities/{uid}/categories/{expanded}/pages",
                                    {"urls": urls, "trigger_crawl": trigger_crawl},
                                )
                                added = result.get("added", [])
                                errors_list = result.get("errors", [])
                                st.success(f"Added {len(added)} URL(s)")
                                if errors_list:
                                    st.warning(f"{len(errors_list)} error(s): {errors_list}")

                                st.rerun()
                            except Exception as e:
                                st.error(f"Failed: {e}")

                # Pages table
                try:
                    pages_data = cached_get(f"/v1/universities/{uid}/categories/{expanded}/pages?limit=100")
                    pages = pages_data.get("pages", [])
                except Exception as e:
                    st.error(f"Failed to load pages: {e}")
                    pages = []

                if pages:
                    for page in pages:
                        col_url, col_domain, col_status, col_action = st.columns([5, 2, 1, 1])
                        with col_url:
                            url = page.get("url", "")
                            display = f"[{url[:80]}...]({url})" if len(url) > 80 else f"[{url}]({url})"
                            st.markdown(display)
                        with col_domain:
                            st.caption(page.get("domain", ""))
                        with col_status:
                            page_status = page.get("processing_status") or page.get("crawl_status", "")
                            st.caption(page_status)
                        with col_action:
                            if st.button("Remove", key=f"rm_{page.get('url_hash', '')}", type="secondary"):
                                try:
                                    api_delete(
                                        f"/v1/universities/{uid}/categories/{expanded}/pages",
                                        {"urls": [page["url"]], "action": "mark_excluded"},
                                    )
    
                                    st.rerun()
                                except Exception as e:
                                    st.error(f"Remove failed: {e}")

                    next_token = pages_data.get("next_token")
                    if next_token:
                        st.caption("Showing first 100 pages. More available.")
                else:
                    st.info("No pages in this category.")


# ═══════════════════════════════════════════
# TAB 4 — INGESTION
# ═══════════════════════════════════════════
with tab_ingestion:
    st.header("Knowledge Base Sync")

    if creating_new:
        st.info("Create a university configuration first in the Setup tab.")
    else:
        has_kb = bool(existing_config.get("kb_config", {}).get("knowledge_base_id"))

        try:
            _sync_ing = cached_get(f"/v1/universities/{uid}/sync-status")
            if _sync_ing.get("sync_needed") and _sync_ing.get("message"):
                st.warning(f"{_sync_ing['message']} Run **KB Sync** below to apply.")
            elif _sync_ing.get("pipeline_running") and _sync_ing.get("message"):
                st.info(_sync_ing["message"])
        except Exception:
            pass

        if not has_kb:
            st.warning(
                "No Knowledge Base configured for this university. "
                "Add **Knowledge Base ID** and **Data Source IDs** in the Setup tab first."
            )

        col_sync, col_status = st.columns(2)
        with col_sync:
            if st.button("Start KB Sync Now", type="primary", use_container_width=True,
                         disabled=not has_kb):
                try:
                    result = api_post(f"/v1/universities/{uid}/kb/sync", {})
                    jobs = result.get("ingestion_jobs", [])
                    st.success(f"KB sync started — {len(jobs)} ingestion job(s)")
                    cached_get.clear()
                except Exception as e:
                    st.error(f"Failed to start sync: {e}")
        with col_status:
            if st.button("Refresh Status", use_container_width=True, key="refresh_kb"):
                st.rerun()

        st.divider()

        if has_kb:
            try:
                kb_data = cached_get(f"/v1/universities/{uid}/kb/sync")
                data_sources = kb_data.get("data_sources", [])
                if not data_sources:
                    st.info("No ingestion jobs found yet.")
                else:
                    st.caption(f"Knowledge Base: `{kb_data.get('knowledge_base_id', 'N/A')}`")
                    for ds in data_sources:
                        with st.container(border=True):
                            st.markdown(f"**Data Source:** `{ds['data_source_id']}`")
                            for ingestion_job in ds.get("recent_jobs", []):
                                job_status = ingestion_job.get("status", "UNKNOWN")
                                stats = ingestion_job.get("statistics", {})
                                if job_status == "COMPLETE":
                                    st.success(f"**{job_status}** — {ingestion_job.get('started_at', '')}")
                                elif job_status == "IN_PROGRESS":
                                    st.info(f"**{job_status}** — {ingestion_job.get('started_at', '')}")
                                elif job_status == "FAILED":
                                    st.error(f"**{job_status}** — {ingestion_job.get('started_at', '')}")
                                else:
                                    st.caption(f"**{job_status}** — {ingestion_job.get('started_at', '')}")

                                if stats:
                                    m1, m2, m3 = st.columns(3)
                                    with m1:
                                        st.metric("Scanned", stats.get("numberOfDocumentsScanned", 0))
                                    with m2:
                                        st.metric("Indexed", stats.get("numberOfNewDocumentsIndexed", 0))
                                    with m3:
                                        st.metric("Failed", stats.get("numberOfDocumentsFailed", 0))
            except Exception as e:
                st.error(f"Failed to load KB status: {e}")


# ═══════════════════════════════════════════
# TAB 5 — MAINTENANCE
# ═══════════════════════════════════════════
with tab_maintenance:
    st.header("Recrawl")

    if creating_new:
        st.info("Create a university configuration first in the Setup tab.")
    else:
        # ── Schedule & Freshness Windows ─────────────────────────────────────
        with st.expander("Schedule & Freshness Windows"):
            # Load freshness windows + schedule config from DynamoDB
            try:
                _fw_data = cached_get(f"/v1/universities/{uid}/freshness")
                current_windows = _fw_data.get("windows", {})
                current_schedule = _fw_data.get("schedule", {"incremental_enabled": True, "full_enabled": True})
            except Exception:
                current_windows = {}
                current_schedule = {"incremental_enabled": True, "full_enabled": True}

            st.subheader("Scheduled Crawls")
            incr_enabled = st.toggle(
                "Daily incremental crawl (2:00 AM UTC)",
                value=current_schedule.get("incremental_enabled", True),
                key=f"schedule_incremental_{uid}",
            )
            full_enabled = st.toggle(
                "Weekly full crawl (Sundays 3:00 AM UTC)",
                value=current_schedule.get("full_enabled", True),
                key=f"schedule_full_{uid}",
            )
            if not incr_enabled and not full_enabled:
                st.warning("Both scheduled crawls are disabled — data will not be refreshed automatically.")
            st.divider()

            # Active categories: only those with pages for this university
            try:
                _cats_data = cached_get(f"/v1/universities/{uid}/categories")
                active_categories = [c["category"] for c in _cats_data.get("categories", [])]
            except Exception:
                active_categories = []
            if not active_categories:
                active_categories = VALID_CATEGORIES  # fallback to full list

            st.subheader("Freshness Windows")
            use_same = st.toggle(
                "Use same freshness window for all categories",
                value=True,
                key="fw_use_same",
            )

            if use_same:
                current_default = int(list(current_windows.values())[0]) if current_windows else 1
                global_days = st.number_input(
                    "Days before re-crawl (all categories)",
                    min_value=1, max_value=365,
                    value=current_default,
                    step=1,
                    key="fw_global",
                )
                # Apply global value to all active categories
                new_windows = {cat: global_days for cat in active_categories}
            else:
                st.caption("Set per-category freshness (days before re-crawl):")
                new_windows = {}
                fw_cols = st.columns(3)
                for i, cat in enumerate(active_categories):
                    with fw_cols[i % 3]:
                        new_windows[cat] = st.number_input(
                            CATEGORY_LABELS.get(cat, cat),
                            min_value=1, max_value=365,
                            value=int(current_windows.get(cat, 1)),
                            step=1,
                            key=f"fw_{cat}",
                        )

            if st.button("Save Settings", key="save_fw"):
                try:
                    api_post(f"/v1/universities/{uid}/freshness", {
                        "windows": new_windows,
                        "schedule": {
                            "incremental_enabled": incr_enabled,
                            "full_enabled": full_enabled,
                        },
                    })
                    cached_get.clear()
                    st.success("Schedule and freshness settings saved.")
                    st.rerun()
                except Exception as e:
                    st.error(f"Failed to save: {e}")

        st.divider()

        # Check for running pipeline to guard against duplicate starts
        running_job_id = has_running_pipeline(uid)
        if running_job_id:
            st.warning(f"Pipeline already running: `{running_job_id}`. Check the Crawling tab for progress.")

        recrawl_col1, recrawl_col2, recrawl_col3 = st.columns(3)
        with recrawl_col1:
            if st.button("Incremental Recrawl", type="primary", use_container_width=True,
                         disabled=bool(running_job_id)):
                try:
                    result = api_post(f"/v1/universities/{uid}/pipeline", {"refresh_mode": "incremental"})
                    st.session_state["active_job_id"] = result.get("job_id")
                    st.success(f"Started: {result.get('job_id')}")
                except Exception as e:
                    st.error(f"Failed: {e}")

        with recrawl_col2:
            full_clicked = st.button("Full Recrawl", use_container_width=True,
                                     disabled=bool(running_job_id))
            if full_clicked:
                st.session_state["confirm_full_recrawl"] = True

        with recrawl_col3:
            root_domain = existing_config.get("root_domain", "example.edu")
            domain_input = st.text_input("Domain", placeholder=f"www.{root_domain}",
                                         key="recrawl_domain")
            if st.button("Recrawl Domain", use_container_width=True,
                         disabled=bool(running_job_id)):
                if domain_input:
                    try:
                        result = api_post(
                            f"/v1/universities/{uid}/pipeline",
                            {"refresh_mode": "domain", "domain": domain_input},
                        )
                        st.session_state["active_job_id"] = result.get("job_id")
                        st.success(f"Started: {result.get('job_id')}")
                    except Exception as e:
                        st.error(f"Failed: {e}")
                else:
                    st.warning("Enter a domain first")

        # Full recrawl confirmation
        if st.session_state.get("confirm_full_recrawl"):
            st.warning("Full recrawl will re-crawl **all** pages from scratch. This can take a long time.")
            confirm_col1, confirm_col2 = st.columns(2)
            with confirm_col1:
                if st.button("Yes, start full recrawl", type="primary"):
                    try:
                        result = api_post(f"/v1/universities/{uid}/pipeline", {"refresh_mode": "full"})
                        st.session_state["active_job_id"] = result.get("job_id")
                        st.session_state.pop("confirm_full_recrawl", None)
                        st.success(f"Started: {result.get('job_id')}")
                    except Exception as e:
                        st.error(f"Failed: {e}")
            with confirm_col2:
                if st.button("Cancel"):
                    st.session_state.pop("confirm_full_recrawl", None)
                    st.rerun()

        st.divider()

        # Recent pipeline jobs
        st.subheader("Recent Pipeline Jobs")
        try:
            jobs_resp = cached_get(f"/v1/universities/{uid}/pipeline?limit=10")
            maint_jobs = jobs_resp.get("jobs", [])
            if maint_jobs:
                rows = []
                for j in maint_jobs:
                    rows.append({
                        "Job ID": j.get("job_id", ""),
                        "Mode": j.get("refresh_mode", ""),
                        "Status": j.get("overall_status", ""),
                        "Created": j.get("created_at", ""),
                    })
                st.dataframe(rows, use_container_width=True, hide_index=True)
            else:
                st.info("No pipeline jobs yet.")
        except Exception as e:
            st.error(f"Failed to load jobs: {e}")

        st.divider()

        # Classification status
        st.subheader("Classification Jobs")
        try:
            class_resp = cached_get(f"/v1/universities/{uid}/classification?limit=5")
            class_jobs = class_resp.get("jobs", [])
            if class_jobs:
                summary = class_resp.get("status_summary", {})
                if summary:
                    sum_cols = st.columns(len(summary))
                    for i, (k, v) in enumerate(summary.items()):
                        with sum_cols[i]:
                            st.metric(k, v)

                rows = []
                for j in class_jobs:
                    rows.append({
                        "Job Name": j.get("job_name", ""),
                        "Status": j.get("status", ""),
                        "Submitted": j.get("submitted_at", ""),
                    })
                st.dataframe(rows, use_container_width=True, hide_index=True)
            else:
                st.info("No classification jobs.")
        except Exception as e:
            st.error(f"Failed to load classification: {e}")

        st.divider()

        # DLQ report
        st.subheader("Dead Letter Queue (DLQ) Report")
        try:
            dlq_data = cached_get(f"/v1/universities/{uid}/dlq")
            dlq_queues = dlq_data.get("queues", [])
            any_failed = any(q.get("depth", 0) > 0 for q in dlq_queues)
            if not any_failed:
                st.success("All queues healthy — no failed messages.")
            else:
                total_failed = dlq_data.get("total_failed", 0)
                st.warning(f"{total_failed} failed message(s) across DLQs. These URLs failed processing and need attention.")
                for q in dlq_queues:
                    depth = q.get("depth", 0)
                    err   = q.get("error", "")
                    if err:
                        st.caption(f"{q['name']}: {err}")
                        continue
                    label = f"**{q['name']}** — {depth} failed message{'s' if depth != 1 else ''}"
                    with st.expander(label, expanded=depth > 0):
                        msgs = q.get("messages", [])
                        if msgs:
                            rows = [
                                {
                                    "URL": m.get("url", "(no URL)"),
                                    "Receive Count": m.get("receive_count", "?"),
                                }
                                for m in msgs
                            ]
                            st.dataframe(rows, use_container_width=True, hide_index=True)
                            st.caption(f"Showing up to 10 of {depth} messages. Messages remain in DLQ — use AWS console to retry or purge.")
                        elif depth > 0:
                            st.caption("Messages present in queue but could not be sampled (FIFO visibility constraints).")
        except Exception as e:
            st.error(f"Failed to load DLQ report: {e}")

        st.divider()

        # Quick stats
        st.subheader("Quick Stats")
        try:
            stats_resp = cached_get(f"/v1/universities/{uid}/stats")

            # ── Row 1: Top-level metrics ──
            kb = stats_resp.get("kb_ingestion", {})
            m1, m2, m3, m4 = st.columns(4)
            with m1:
                st.metric("Total URLs", f"{stats_resp.get('total_urls', 0):,}")
            with m2:
                st.metric("Content Pages", f"{stats_resp.get('total_content_pages', 0):,}")
            with m3:
                st.metric("Ingested (in KB)", f"{kb.get('ingested_pages', 0):,}")
            with m4:
                pages_changed = stats_resp.get('pages_changed', 0)
                pending_kb = stats_resp.get('pending_kb_sync', False)
                if pending_kb and pages_changed > 0:
                    st.metric("Awaiting KB Sync", f"{pages_changed:,}")
                else:
                    st.metric("Pages Changed", f"{pages_changed:,}")

            # ── Row 2: Secondary metrics ──
            s1, s2, s3, s4 = st.columns(4)
            with s1:
                st.metric("Dead URLs", f"{stats_resp.get('dead_urls', 0):,}")
            with s2:
                st.metric("Classified Pages", f"{stats_resp.get('classified_pages', 0):,}")
            with s3:
                st.metric("Unclassified", f"{stats_resp.get('unclassified_pages', 0):,}")
            with s4:
                st.metric("Media Files", f"{stats_resp.get('total_media_files', 0):,}")

            # ── Crawl status breakdown ──
            with st.expander("Crawl Status Breakdown"):
                cs = stats_resp.get("urls_by_crawl_status", {})
                if cs:
                    cols = st.columns(min(len(cs), 4))
                    for i, (k, v) in enumerate(sorted(cs.items(), key=lambda x: -x[1])):
                        if v > 0:
                            with cols[i % len(cols)]:
                                st.metric(k.replace('_', ' ').title(), f"{v:,}")

            # ── KB Ingestion details ──
            with st.expander("KB Ingestion Details"):
                kb = stats_resp.get("kb_ingestion", {})
                ki1, ki2, ki3, ki4 = st.columns(4)
                with ki1:
                    st.metric("Scanned", f"{kb.get('scanned_pages', 0):,}")
                with ki2:
                    st.metric("New Indexed", f"{kb.get('new_indexed', 0):,}")
                with ki3:
                    st.metric("Modified", f"{kb.get('modified_indexed', 0):,}")
                with ki4:
                    st.metric("Ingestion Failed", f"{kb.get('failed_pages', 0):,}")

                ki5, ki6, ki7 = st.columns(3)
                with ki5:
                    pc = stats_resp.get('pages_changed', 0)
                    st.metric("Pages Changed (Crawl)", f"{pc:,}")
                with ki6:
                    st.metric("Deleted", f"{kb.get('deleted', 0):,}")
                with ki7:
                    status = kb.get('last_sync_status') or 'N/A'
                    st.metric("Last Sync Status", status)
                sync_at = kb.get('last_sync_at')
                if sync_at:
                    st.caption(f"Last sync: {sync_at}")

            # ── Media breakdown ──
            media_by_type = stats_resp.get("media_by_type", {})
            if any(v > 0 for v in media_by_type.values()):
                with st.expander("Media Breakdown"):
                    mcols = st.columns(len(media_by_type))
                    for i, (mtype, mcount) in enumerate(media_by_type.items()):
                        with mcols[i]:
                            st.metric(mtype.upper(), f"{mcount:,}")

        except Exception as e:
            st.error(f"Failed to load stats: {e}")

        st.divider()

        # Reset university data
        st.subheader("Reset Data")
        reset_col1, reset_col2 = st.columns(2)

        with reset_col1:
            with st.container(border=True):
                st.markdown("**Reset All Data**")
                st.caption(
                    "Deletes all crawl results, cleaned content, classification, "
                    "entity store, and job history. Keeps the university config."
                )
                if st.button("Reset All Data", type="secondary", use_container_width=True):
                    st.session_state["confirm_reset"] = "all"

        with reset_col2:
            with st.container(border=True):
                st.markdown("**Reset Classification Only**")
                st.caption(
                    "Deletes classification results and metadata sidecars only. "
                    "Keeps crawled HTML and cleaned markdown so you can re-classify."
                )
                if st.button("Reset Classification", type="secondary", use_container_width=True):
                    st.session_state["confirm_reset"] = "classification"

        confirm_scope = st.session_state.get("confirm_reset")
        if confirm_scope:
            scope_label = "ALL data (crawl + clean + classify + jobs)" if confirm_scope == "all" else "classification results only"
            st.warning(f"This will permanently delete **{scope_label}** for **{uid}**. Config will be preserved.")
            confirm_c1, confirm_c2 = st.columns(2)
            with confirm_c1:
                if st.button("Yes, delete and reset", type="primary"):
                    try:
                        with st.spinner("Deleting data..."):
                            result = api_post(f"/v1/universities/{uid}/reset", {"scope": confirm_scope})
                        st.session_state.pop("confirm_reset", None)
                        st.session_state.pop("active_job_id", None)
                        st.session_state.pop("expanded_cat", None)
                        stopped = result.get('pipelines_stopped', 0)
                        msg = "Reset complete."
                        if stopped:
                            msg += f" Pipelines stopped: {stopped}."
                        msg += (
                            f" URL records: {result.get('url_registry_deleted', result.get('urls_reset', 0))},"
                            f" S3 objects: {result.get('s3_objects_deleted', result.get('sidecars_deleted', 0))},"
                            f" Entities: {result.get('entity_store_deleted', 0)}"
                        )
                        st.success(msg)
                    except Exception as e:
                        st.error(f"Reset failed: {e}")
            with confirm_c2:
                if st.button("Cancel reset"):
                    st.session_state.pop("confirm_reset", None)
                    st.rerun()
