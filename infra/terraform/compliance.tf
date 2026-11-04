# ==============================================================================
# Compliance & Governance — Cloud Audit Logs, Regional Audit Log Bucket & Sink
#
# Audit architecture:
#   1. Application audit events (`projects/<project>/logs/interactiverag-audit`)
#   2. GCP Data Access Audit Logs (`cloudaudit.googleapis.com/data_access`)
#   3. HTTPS Load Balancer HTTP access logs (`resource.type="http_load_balancer"`)
#      -> All three remain searchable in `_Default` (Logs Explorer) AND are copied into
#         the regional `compliance_audit` log bucket in `var.region` for long-term retention.
# ==============================================================================

locals {
  # Core data-bearing and AI services where `DATA_READ` and `DATA_WRITE` Cloud Audit Logs
  # are enabled (GCP only enables `ADMIN_READ` by default).
  audited_gcp_services = toset([
    "storage.googleapis.com",
    "aiplatform.googleapis.com",
    "dlp.googleapis.com",
    "modelarmor.googleapis.com",
  ])

  compliance_audit_bucket_id = "${var.app_name}-audit-${var.environment}"
}

# ==============================================================================
# 1. Google Cloud Data-Access Audit Log Configuration
# ==============================================================================

# Enables `ADMIN_READ`, `DATA_READ`, and `DATA_WRITE` Cloud Audit Logs across Cloud Storage,
# Vertex AI, Cloud DLP, and Model Armor so every object read/write, embedding/LLM call, and
# DLP/Model Armor screening call is recorded in `cloudaudit.googleapis.com` and correlated
# via `X-Cloud-Trace-Context`.
resource "google_project_iam_audit_config" "compliance_data_access" {
  for_each = local.audited_gcp_services
  project  = var.project_id
  service  = each.value

  audit_log_config {
    log_type = "ADMIN_READ"
  }

  audit_log_config {
    log_type = "DATA_READ"
  }

  audit_log_config {
    log_type = "DATA_WRITE"
  }
}

# ==============================================================================
# 2. Dedicated Regional Cloud Logging Audit Bucket
# ==============================================================================

# Stores compliance and audit records strictly inside `var.region` (unlike `_Default`, which is
# global) with configurable long-term retention (`var.audit_log_retention_days`, default 365 days)
# and optional WORM retention lock (`var.audit_log_bucket_locked`).
resource "google_logging_project_bucket_config" "compliance_audit" {
  project          = var.project_id
  location         = var.region
  bucket_id        = local.compliance_audit_bucket_id
  retention_days   = var.audit_log_retention_days
  locked           = var.audit_log_bucket_locked
  enable_analytics = true
  description      = "Regional compliance audit log bucket for InteractiveRAG (${var.environment})."

  lifecycle {
    precondition {
      condition     = can(regex("^[a-z]+-[a-z]+[0-9]+$", var.region))
      error_message = "Compliance audit log bucket must reside in a concrete single GCP region (never 'global' or a multi-region)."
    }
  }

  depends_on = [
    google_project_service.required_apis["logging.googleapis.com"],
  ]
}

# ==============================================================================
# 3. Project-Level Compliance Log Sink
# ==============================================================================

# Non-excluding log sink that copies:
#   - Application compliance events (`interactiverag-audit`)
#   - GCP Cloud Audit Logs (`cloudaudit.googleapis.com`)
#   - External HTTPS Load Balancer access logs (`resource.type="http_load_balancer"`)
# into `compliance_audit` while leaving `_Default` intact for immediate Logs Explorer queries.
# Note: Sinks routing to a Cloud Logging bucket in the SAME project write directly within
# the project and do not require a separate `writer_identity` IAM binding.
resource "google_logging_project_sink" "compliance_audit_sink" {
  project     = var.project_id
  name        = "${var.app_name}-audit-sink-${var.environment}"
  description = "Copies InteractiveRAG application audit events, GCP Cloud Audit Logs, and HTTPS LB logs into the regional compliance bucket."
  destination = "logging.googleapis.com/projects/${var.project_id}/locations/${var.region}/buckets/${google_logging_project_bucket_config.compliance_audit.bucket_id}"
  filter      = "logName=\"projects/${var.project_id}/logs/interactiverag-audit\" OR logName:\"cloudaudit.googleapis.com\" OR resource.type=\"http_load_balancer\""

  depends_on = [
    google_logging_project_bucket_config.compliance_audit,
  ]
}
