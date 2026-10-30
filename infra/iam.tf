# ==============================================================================
# 1. Runtime Service Accounts (Principle of Least Privilege)
#    Why needed: By default, Cloud Run attaches the project's default Compute Engine
#    service account, which often holds broad `roles/editor` permissions. Instead, we
#    provision dedicated, minimal runtime identities for the backend and frontend.
# ==============================================================================

# Shared runtime identity for `ingestion-api` and `agent-api` Cloud Run services.
# Note: GCP enforces a 6–30 character limit on `account_id`, so `substr(..., 0, 28)`
# guarantees valid naming even with longer `app_name` or `environment` values.
resource "google_service_account" "backend_sa" {
  project      = var.project_id
  account_id   = trimsuffix(substr("${var.app_name}-be-${var.environment}", 0, 28), "-")
  display_name = "InteractiveRAG Backend Service Account (${var.environment})"
  description  = "Least-privilege identity for ingestion-api and agent-api Cloud Run services."

  depends_on = [
    google_project_service.required_apis["iam.googleapis.com"],
  ]
}

# Dedicated zero-privilege runtime identity for the static `frontend` Nginx service.
# Intentionally granted ZERO IAM roles below: the frontend container only serves static
# assets and `/env-config.js`, and never calls GCP APIs directly from the server side.
resource "google_service_account" "frontend_sa" {
  project      = var.project_id
  account_id   = trimsuffix(substr("${var.app_name}-fe-${var.environment}", 0, 28), "-")
  display_name = "InteractiveRAG Frontend Service Account (${var.environment})"
  description  = "Zero-privilege identity for static Nginx frontend Cloud Run service."

  depends_on = [
    google_project_service.required_apis["iam.googleapis.com"],
  ]
}

# ==============================================================================
# 2. Scoped IAM Role Grants for `backend_sa`
# ==============================================================================

# 1. Vertex AI (`roles/aiplatform.user`):
#    Allows `ingestion-api` to generate document/query embeddings and `agent-api`
#    to invoke regional Gemini chat models.
resource "google_project_iam_member" "backend_vertex_ai_user" {
  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.backend_sa.email}"
}

# 2. Bucket-Scoped Cloud Storage Access (`roles/storage.objectUser`):
#    Granted on `google_storage_bucket.chroma_persistence` ONLY—not at the project level—so
#    backend containers can read/write per-user ChromaDB snapshots, PDF archives, and lock files
#    without having access to any other bucket in the project (such as the Terraform state bucket).
resource "google_storage_bucket_iam_member" "backend_chroma_bucket_object_user" {
  bucket = google_storage_bucket.chroma_persistence.name
  role   = "roles/storage.objectUser"
  member = "serviceAccount:${google_service_account.backend_sa.email}"
}

# 3. Google Cloud Model Armor (`roles/modelarmor.user`):
#    Allows backend services to call `sanitizeUserPrompt` and `sanitizeModelResponse`
#    against the regional Model Armor template over Private Service Connect.
resource "google_project_iam_member" "backend_model_armor_user" {
  project = var.project_id
  role    = "roles/modelarmor.user"
  member  = "serviceAccount:${google_service_account.backend_sa.email}"
}

# 4. Sensitive Data Protection / Cloud DLP (`roles/dlp.user`):
#    Required alongside `roles/modelarmor.user` when a Model Armor template uses Advanced SDP
#    (`inspect_template` + `deidentify_template`) to redact PII in chunks and responses.
resource "google_project_iam_member" "backend_dlp_user" {
  project = var.project_id
  role    = "roles/dlp.user"
  member  = "serviceAccount:${google_service_account.backend_sa.email}"
}

# 5. Cloud Logging (`roles/logging.logWriter`):
#    Allows `emit_audit_event()` (`src/core/compliance/audit.py`) and `emit_telemetry_event()`
#    (`src/core/observability/telemetry.py`) to publish structured JSON audit and telemetry
#    records directly to `interactiverag-audit` and `interactiverag-telemetry`.
resource "google_project_iam_member" "backend_log_writer" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.backend_sa.email}"
}

# 6. Cloud Monitoring (`roles/monitoring.metricWriter`):
#    Allows backend services to write custom monitoring metrics when needed.
resource "google_project_iam_member" "backend_metric_writer" {
  project = var.project_id
  role    = "roles/monitoring.metricWriter"
  member  = "serviceAccount:${google_service_account.backend_sa.email}"
}

# 7. Cloud Trace (`roles/cloudtrace.agent`):
#    Allows `start_span()` (`src/core/observability/tracing.py`) to export hierarchical per-stage
#    latency spans directly to Google Cloud Trace when `CLOUD_TRACE_ENABLED = "true"`.
resource "google_project_iam_member" "backend_trace_agent" {
  project = var.project_id
  role    = "roles/cloudtrace.agent"
  member  = "serviceAccount:${google_service_account.backend_sa.email}"
}
