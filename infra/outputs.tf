output "frontend_url" {
  description = "Primary HTTPS URL for the InteractiveRAG React web dashboard (matches configured CORS origin)."
  value       = local.frontend_deterministic_url
}

output "frontend_service_uri" {
  description = "Cloud Run default URI reported by Google Cloud for the frontend service."
  value       = google_cloud_run_v2_service.frontend.uri
}

output "ingestion_api_url" {
  description = "HTTPS URL for the Document Ingestion & Collection Management API."
  value       = google_cloud_run_v2_service.ingestion_api.uri
}

output "agent_api_url" {
  description = "HTTPS URL for the LangGraph Agentic RAG & SSE Streaming API."
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
