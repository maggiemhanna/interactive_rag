# ==============================================================================
# Compliance & Governance — Cloud Audit Logs, Regional Audit Log Bucket & Sink
# ==============================================================================

locals {
  audited_gcp_services = toset([
    "storage.googleapis.com",
    "aiplatform.googleapis.com",
    "dlp.googleapis.com",
    "modelarmor.googleapis.com",
  ])

  compliance_audit_bucket_id = "${var.app_name}-audit-${var.environment}"
}

# 1. Enable ADMIN_READ, DATA_READ, and DATA_WRITE Cloud Audit Logs for core data & AI services
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

# 2. Regional Cloud Logging Bucket storing retained compliance & audit records in var.region
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

# 3. Non-excluding Project Sink copying interactiverag-audit, Cloud Audit Logs, and LB access logs
#    into the regional compliance bucket while keeping _Default populated for immediate Logs Explorer queries.
#    Note: Sinks routing to a Cloud Logging bucket in the SAME project do not have a writer_identity
#    and require no additional IAM grant (GCP Cloud Logging writes directly within the project).
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
