/**
 * University KB Crawler — API Type Definitions
 *
 * Auto-generated from the backend API routes.
 * Import these types directly in your frontend project.
 */

// ─── API Client Helper ─────────────────────────────────

const API_BASE = ""; // Set your base URL: https://{api-id}.execute-api.us-east-1.amazonaws.com/dev/v1

export async function apiFetch<T>(
  path: string,
  options?: RequestInit
): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    headers: {
      "Content-Type": "application/json",
      "X-Api-Key": process.env.NEXT_PUBLIC_API_KEY!,
      ...(options?.headers || {}),
    },
    ...options,
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({ error: res.statusText }));
    throw new ApiError(res.status, body.error || res.statusText);
  }
  return res.json();
}

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string
  ) {
    super(message);
    this.name = "ApiError";
  }
}

// ─── Enums & Constants ──────────────────────────────────

export const VALID_CATEGORIES = [
  "admissions",
  "financial_aid",
  "academic_programs",
  "course_catalog",
  "student_services",
  "housing_dining",
  "campus_life",
  "athletics",
  "faculty_staff",
  "library",
  "it_services",
  "policies",
  "events",
  "news",
  "about",
  "careers",
  "alumni",
  "other",
  "low_value",
  "excluded",
] as const;

export type Category = (typeof VALID_CATEGORIES)[number];

export const CATEGORY_LABELS: Record<Category, string> = {
  academic_programs: "Academic Programs",
  admissions: "Admissions",
  financial_aid: "Financial Aid",
  course_catalog: "Course Catalog",
  student_services: "Student Services",
  housing_dining: "Housing & Dining",
  campus_life: "Campus Life",
  athletics: "Athletics",
  faculty_staff: "Faculty & Staff",
  library: "Library",
  it_services: "IT Services",
  policies: "Policies",
  events: "Events",
  news: "News",
  about: "About",
  careers: "Careers",
  alumni: "Alumni",
  other: "Other",
  low_value: "Low Value",
  excluded: "Excluded",
};

export type CrawlStatus =
  | "pending"
  | "crawled"
  | "error"
  | "failed"
  | "dead"
  | "redirected"
  | "blocked_robots"
  | "skipped_depth";

export type ProcessingStatus =
  | "cleaned"
  | "classified"
  | "processed_media"
  | "unprocessed";

export type PipelineStatus = "running" | "completed" | "failed" | "cancelled";

export type RefreshMode = "full" | "incremental" | "domain";

export type StageStatus =
  | "pending"
  | "running"
  | "completed"
  | "waiting"
  | "skipped";

// ─── University ─────────────────────────────────────────

export interface University {
  university_id: string;
  name: string;
}

export interface UniversitiesResponse {
  universities: University[];
}

export interface UniversityConfig {
  university_id: string;
  name: string;
  root_domain: string;
  seed_urls: string[];
  crawl_config: CrawlConfig;
  freshness_windows_days: Record<string, number>;
  rate_limits: { default_rps: number };
  kb_config?: KBConfig;
}

export interface CrawlConfig {
  max_concurrent_requests: number;
  requests_per_second_per_domain: number;
  max_pages: number;
  max_crawl_depth: number;
  request_timeout_seconds: number;
  allowed_domain_patterns: string[];
  exclude_extensions: string[];
  pdf_extensions: string[];
  exclude_path_patterns: string[];
  exclude_domains: string[];
  include_subdomains: boolean;
}

export interface KBConfig {
  knowledge_base_id: string;
  data_source_ids: string[];
}

export interface SaveConfigRequest {
  name: string;
  root_domain: string;
  seed_urls: string[];
  crawl_config?: Partial<CrawlConfig>;
  freshness_windows_days?: Record<string, number>;
  rate_limits?: { default_rps: number };
  kb_config?: KBConfig;
}

export interface SaveConfigResponse {
  status: "saved";
  university_id: string;
  config: UniversityConfig;
}

// ─── Stats ──────────────────────────────────────────────

export interface KBIngestionStats {
  /** Total pages successfully in KB (scanned - failed) */
  ingested_pages: number;
  /** Pages that failed Bedrock ingestion (separate from pages_changed) */
  failed_pages: number;
  /** Total pages scanned by Bedrock in the latest job */
  scanned_pages: number;
  /** New documents indexed in the latest job */
  new_indexed: number;
  /** Modified documents re-indexed in the latest job */
  modified_indexed: number;
  /** Documents deleted from KB in the latest job */
  deleted: number;
  /** "STARTING" | "IN_PROGRESS" | "COMPLETE" | "FAILED" | null */
  last_sync_status: string | null;
  last_sync_at: string | null;
}

export interface DashboardStats {
  university_id: string;
  name: string;
  /** Total URLs in DynamoDB url-registry (all categories) */
  total_urls: number;
  /** Sum of all crawl_status counts in DynamoDB */
  total_discovered_urls: number;
  urls_by_crawl_status: Record<string, number>;
  urls_by_processing_status: Record<string, number>;
  /** S3 .md files in clean-content/ (source of truth for what can go into KB) */
  total_content_pages: number;
  /** S3 .md.metadata.json sidecars (classified pages) */
  classified_pages: number;
  /** total_content_pages - classified_pages */
  unclassified_pages: number;
  /** Files in S3 raw-pdf/ */
  total_media_files: number;
  /** Breakdown by type: pdf, image, audio, video, other */
  media_by_type: Record<string, number>;
  /** Bedrock Agent ingestion statistics */
  kb_ingestion: KBIngestionStats;
  /** New/modified pages from latest crawl (tracked by content-cleaner, NOT Bedrock failures) */
  pages_changed: number;
  /** Number of category operations (rename/delete/add/remove) since last KB sync */
  categories_modified: number;
  /** True if data was reset since last KB sync */
  data_reset: boolean;
  /** True when any sync trigger is active: pages_changed > 0, categories_modified > 0, data_reset, or status == "pending_sync" */
  pending_kb_sync: boolean;
  /** Count of URLs with crawl_status=dead */
  dead_urls: number;
  last_crawled_at: string | null;
  days_since_crawl: number | null;
  stale_categories: string[];
  crawl_completed_at: string | null;
  config: {
    seed_urls_count: number;
    max_crawl_depth: number;
    allowed_domain_patterns: string[];
  } | null;
}

// ─── Categories ─────────────────────────────────────────

export interface CategoryCard {
  category: string;
  label: string;
  count: number;
}

export interface CategoriesResponse {
  university_id: string;
  total_pages: number;
  /** Only categories with count > 0 are returned. 'excluded' is never included. */
  categories: CategoryCard[];
}

// ─── Pages ──────────────────────────────────────────────

export interface Page {
  url: string;
  url_hash: string;
  domain: string;
  subcategory: string | null;
  processing_status: string;
  crawl_status: string;
  content_type: string | null;
  content_length: number | null;
  last_crawled_at: string | null;
}

export interface PagesResponse {
  university_id: string;
  category: string;
  pages: Page[];
  count: number;
  /** Pass to next request as ?next_token= for pagination. Null when no more pages. */
  next_token: string | null;
}

export interface AddPagesRequest {
  urls: string[];
  /** Default true. Set false to register URLs without triggering a crawl. */
  trigger_crawl?: boolean;
}

export interface AddPagesResponse {
  added: Array<{
    url: string;
    status:
      | "category_updated"
      | "queued_for_crawl"
      | "registered_and_queued"
      | "registered";
  }>;
  errors: Array<{ url: string; error: string }>;
}

export interface RemovePagesRequest {
  urls: string[];
  /** "mark_excluded" (default) or "delete_content" (also deletes S3 objects) */
  action?: "mark_excluded" | "delete_content";
}

export interface RemovePagesResponse {
  removed: Array<{ url: string; action: string }>;
  errors: Array<{ url: string; error: string }>;
}

// ─── Media Upload ───────────────────────────────────────

export interface UploadUrlRequest {
  filename: string;
  /** MIME type, e.g. "application/pdf", "image/jpeg". Default: "application/pdf" */
  content_type?: string;
}

export interface UploadUrlResponse {
  /** Presigned S3 URL — PUT file bytes here with matching Content-Type header */
  upload_url: string;
  s3_key: string;
  category: string;
  /** Sanitized filename (spaces/slashes replaced with underscores) */
  filename: string;
}

export interface ProcessMediaRequest {
  /** Required — from UploadUrlResponse.s3_key */
  s3_key: string;
  filename?: string;
  content_type?: string;
}

export interface ProcessMediaResponse {
  status: "processing";
  s3_key: string;
}

// ─── Category Operations ────────────────────────────────

export interface RenameCategoryRequest {
  new_category: Category;
}

export interface RenameCategoryResponse {
  old_category: string;
  new_category: string;
  pages_moved: number;
  errors: Array<{ url: string; error: string }>;
}

export interface DeleteCategoryRequest {
  /** Default: "excluded" */
  target_category?: Category;
}

export interface DeleteCategoryResponse {
  category: string;
  target_category: string;
  pages_deleted: number;
  errors: Array<{ url: string; error: string }>;
}

// ─── Pipeline ───────────────────────────────────────────

export interface StartPipelineRequest {
  /** "full" | "incremental" | "domain". Default: "incremental" */
  refresh_mode?: RefreshMode;
  /** Required only when refresh_mode is "domain" */
  domain?: string;
}

export interface QueueDepth {
  available: number;
  in_flight: number;
}

export interface CrawlStage {
  status: StageStatus;
  started_at?: string;
  completed_at?: string;
  total?: number;
  completed?: number;
  pending?: number;
  failed?: number;
  queue?: QueueDepth;
}

export interface CleanStage {
  status: StageStatus;
  started_at?: string;
  completed_at?: string;
  completed?: number;
  queue?: QueueDepth;
}

export interface ClassifyStage {
  status: StageStatus;
  started_at?: string;
  completed_at?: string;
  completed?: number;
  total?: number;
  classify_triggered?: boolean;
  batch_jobs?: Array<{ job_arn: string; status: string }>;
  batch_jobs_done?: boolean;
}

export interface PipelineJob {
  job_id: string;
  university_id: string;
  created_at: string;
  refresh_mode: RefreshMode;
  domain: string;
  triggered_by: string;
  overall_status: PipelineStatus;
  execution_arn: string;
  crawl_stage: CrawlStage;
  clean_stage: CleanStage;
  classify_stage: ClassifyStage;
  /** Unix timestamp — job record auto-deletes after 90 days */
  ttl: number;
}

export interface PipelineJobsResponse {
  jobs: PipelineJob[];
  count: number;
  next_token: string | null;
}

// ─── KB Sync ────────────────────────────────────────────

export interface IngestionJob {
  ingestion_job_id: string;
  /** "STARTING" | "IN_PROGRESS" | "COMPLETE" | "FAILED" */
  status: string;
  started_at: string;
  updated_at: string;
  statistics: {
    numberOfDocumentsScanned?: number;
    numberOfDocumentsFailed?: number;
    numberOfNewDocumentsIndexed?: number;
    numberOfModifiedDocumentsIndexed?: number;
    numberOfDocumentsDeleted?: number;
  };
}

export interface KBSyncStartResponse {
  university_id: string;
  knowledge_base_id: string;
  ingestion_jobs: Array<{
    data_source_id: string;
    ingestion_job_id?: string;
    status?: string;
    error?: string;
  }>;
}

export interface KBSyncStatusResponse {
  university_id: string;
  knowledge_base_id: string;
  data_sources: Array<{
    data_source_id: string;
    recent_jobs?: IngestionJob[];
    error?: string;
  }>;
}

// ─── Sync Status Health Check ────────────────────────────

export interface SyncStatusResponse {
  university_id: string;
  /** True when any sync trigger is active */
  sync_needed: boolean;
  /** Human-readable banner text. null when no action needed. Display directly in UI */
  message: string | null;
  /** Breakdown of why sync is needed */
  reasons: {
    pages_changed: number;
    categories_modified: number;
    data_reset: boolean;
  };
  /** Raw kb_sync_status value: "running", "pending_sync", "no_changes", "syncing" */
  status: string | null;
  crawl_completed_at: string | null;
  synced_at: string | null;
  /** Whether a pipeline is currently running */
  pipeline_running: boolean;
}

// ─── Freshness & Schedule ───────────────────────────────

export interface FreshnessResponse {
  university_id: string;
  /** Per-category freshness windows in days. e.g. { "admissions": 7, "events": 3 } */
  windows: Record<string, number>;
  schedule: ScheduleFlags;
}

export interface ScheduleFlags {
  /** If false, daily incremental crawl is skipped for this university */
  incremental_enabled: boolean;
  /** If false, weekly full crawl is skipped for this university */
  full_enabled: boolean;
}

export interface SaveFreshnessRequest {
  /** Each value must be an integer between 1 and 3650 */
  windows: Record<string, number>;
  schedule?: Partial<ScheduleFlags>;
}

export interface SaveFreshnessResponse {
  status: "saved";
  university_id: string;
  windows: Record<string, number>;
  schedule: ScheduleFlags;
}

// ─── Classification ─────────────────────────────────────

export interface ClassificationJob {
  job_name: string;
  job_arn: string;
  /** "Submitted" | "Validating" | "Scheduled" | "InProgress" | "Completed" | "PartiallyCompleted" | "Failed" | "Stopped" */
  status: string;
  model_id: string;
  submitted_at: string | null;
  last_modified_at: string | null;
  end_time: string | null;
  message: string;
  input_s3_uri: string;
  output_s3_uri: string;
}

export interface ClassificationResponse {
  university_id: string;
  total_jobs: number;
  status_summary: Record<string, number>;
  jobs: ClassificationJob[];
}

// ─── DLQ ────────────────────────────────────────────────

export interface DLQMessage {
  url: string;
  university_id: string;
  /** Number of processing attempts before landing in DLQ (always >= 3) */
  receive_count: number;
  /** Unix timestamp in milliseconds */
  sent_at: string;
  /** First 200 chars of the raw SQS message body */
  body_preview: string;
}

export interface DLQQueue {
  name: "crawl-dlq" | "processing-dlq" | "pdf-processing-dlq";
  /** Approximate count — SQS provides approximate values */
  depth: number;
  /** Up to 10 messages sampled (non-destructive peek) */
  messages: DLQMessage[];
  error?: string;
}

export interface DLQResponse {
  university_id: string;
  total_failed: number;
  queues: DLQQueue[];
}

// ─── Reset ──────────────────────────────────────────────

export interface ResetRequest {
  /** "all" — delete everything except config. "classification" — delete only classification results */
  scope: "all" | "classification";
}

export interface ResetResponse {
  scope: "all" | "classification";
  university_id: string;
  /** Number of running pipelines that were stopped */
  pipelines_stopped: number;
  // scope="all" fields:
  url_registry_deleted?: number;
  entity_store_deleted?: number;
  pipeline_jobs_deleted?: number;
  s3_objects_deleted?: number;
  // scope="classification" fields:
  sidecars_deleted?: number;
  batch_jobs_deleted?: number;
  urls_reset?: number;
}
