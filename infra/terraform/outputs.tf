# ==============================================================================
# 1. Public Entry Point (Global External HTTPS Load Balancer)
# ==============================================================================

# Primary URL for opening the InteractiveRAG web dashboard and calling `/api/ingest/*`
# and `/api/agent/*` over HTTPS (`https://<IP-with-dashes>.sslip.io`).
output "load_balancer_url" {
  description = "Primary public HTTPS URL for InteractiveRAG via the Global External Load Balancer."
  value       = local.lb_url
}

output "load_balancer_ip" {
  description = "Static global IPv4 address of the External HTTPS Load Balancer."
  value       = google_compute_global_address.lb_ip.address
}

output "frontend_url" {
  description = "Primary HTTPS URL for the InteractiveRAG React web dashboard (alias for load_balancer_url)."
  value       = local.lb_url
}

# ==============================================================================
# 2. Internal Cloud Run Service URIs (Direct Internet Access Blocked)
#    Useful for verifying Network Segmentation (confirming direct `*.run.app`
#    calls from the public internet return HTTP 404/403).
# ==============================================================================

output "frontend_service_uri" {
  description = "Internal Cloud Run URI for frontend (direct internet access blocked via INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER)."
  value       = google_cloud_run_v2_service.frontend.uri
}

output "ingest_api_url" {
  description = "Internal Cloud Run URI for ingest-api (direct internet access blocked via INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER)."
  value       = google_cloud_run_v2_service.ingest_api.uri
}

output "agent_api_url" {
  description = "Internal Cloud Run URI for agent-api (direct internet access blocked via INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER)."
  value       = google_cloud_run_v2_service.agent_api.uri
}

# ==============================================================================
# 3. Storage, Artifact Registry, IAM & Identity Platform Outputs
# ==============================================================================

output "chroma_gcs_bucket" {
  description = "Private GCS bucket storing isolated per-user ChromaDB vector stores and uploaded PDFs."
  value       = google_storage_bucket.chroma_persistence.name
}

output "artifact_registry_url" {
  description = "Artifact Registry base path for pushing backend and frontend container images."
  value       = local.artifact_registry_url
}

output "backend_service_account_email" {
  description = "Least-privilege service account attached to backend Cloud Run services."
  value       = google_service_account.backend_sa.email
}

output "firebase_web_api_key" {
  description = "Identity Platform / Firebase Web API Key (use `terraform -chdir=infra output -raw firebase_web_api_key`)."
  value       = try(google_identity_platform_config.auth.client[0].api_key, "")
  sensitive   = true
}

# ==============================================================================
# 4. AI Security & Private Service Connect Outputs
# ==============================================================================

output "model_armor_template_id" {
  description = "Full resource ID of the provisioned Google Cloud Model Armor template."
  value       = google_model_armor_template.rag_guard.id
}

output "dlp_inspect_template_id" {
  description = "Full resource ID of the Sensitive Data Protection (Cloud DLP) inspect template."
  value       = google_data_loss_prevention_inspect_template.ai_output_inspect.id
}

output "dlp_deidentify_template_id" {
  description = "Full resource ID of the Sensitive Data Protection (Cloud DLP) de-identify template."
  value       = google_data_loss_prevention_deidentify_template.ai_output_deidentify.id
}

output "model_armor_psc_endpoint_ip" {
  description = "Internal VPC IPv4 address of the regional Private Service Connect endpoint for Model Armor."
  value       = google_network_connectivity_regional_endpoint.modelarmor_psc.address
}

# ==============================================================================
# 5. Compliance, Data Residency & Audit Logging Outputs
# ==============================================================================

output "google_cloud_location" {
  description = "Pinned single GCP region enforced across Cloud Run, GCS, Model Armor, and Vertex AI."
  value       = var.region
}

output "compliance_audit_log_bucket_id" {
  description = "Regional Cloud Logging bucket storing retained compliance & audit logs."
  value       = google_logging_project_bucket_config.compliance_audit.id
}

output "compliance_audit_sink_name" {
  description = "Project-level Cloud Logging sink routing compliance & audit logs into the regional audit bucket."
  value       = google_logging_project_sink.compliance_audit_sink.name
}

output "application_audit_log_name" {
  description = "Full GCP Cloud Logging logName for structured InteractiveRAG compliance events."
  value       = "projects/${var.project_id}/logs/interactiverag-audit"
}

# ==============================================================================
# 6. Observability & AI Monitoring Outputs
# ==============================================================================

output "observability_log_bucket_id" {
  description = "Regional Cloud Logging bucket storing operational container logs and interactiverag-telemetry events."
  value       = google_logging_project_bucket_config.observability_telemetry.id
}

output "observability_log_sink_name" {
  description = "Project-level Cloud Logging sink routing telemetry and container logs into the regional observability bucket."
  value       = google_logging_project_sink.observability_sink.name
}

output "application_telemetry_log_name" {
  description = "Full GCP Cloud Logging logName for structured InteractiveRAG AI & performance telemetry events."
  value       = "projects/${var.project_id}/logs/interactiverag-telemetry"
}

output "observability_dashboard_id" {
  description = "Google Cloud Monitoring dashboard resource ID for InteractiveRAG Observability & AI Monitoring."
  value       = google_monitoring_dashboard.observability_dashboard.id
}

output "alert_policy_ids" {
  description = "Cloud Monitoring alert policy IDs for latency SLO, Cloud Run memory saturation, and 5xx error spikes."
  value = {
    agent_high_latency          = google_monitoring_alert_policy.agent_high_latency_alert.id
    cloud_run_memory_saturation = google_monitoring_alert_policy.cloud_run_memory_saturation_alert.id
    cloud_run_5xx_error_rate    = google_monitoring_alert_policy.cloud_run_5xx_error_rate_alert.id
  }
}

