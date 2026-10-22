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

output "frontend_service_uri" {
  description = "Internal Cloud Run URI for frontend (direct internet access blocked via INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER)."
  value       = google_cloud_run_v2_service.frontend.uri
}

output "ingestion_api_url" {
  description = "Internal Cloud Run URI for ingestion-api (direct internet access blocked via INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER)."
  value       = google_cloud_run_v2_service.ingestion_api.uri
}

output "agent_api_url" {
  description = "Internal Cloud Run URI for agent-api (direct internet access blocked via INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER)."
  value       = google_cloud_run_v2_service.agent_api.uri
}

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
