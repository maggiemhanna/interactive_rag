resource "google_service_account" "backend_sa" {
  project      = var.project_id
  account_id   = trimsuffix(substr("${var.app_name}-be-${var.environment}", 0, 28), "-")
  display_name = "InteractiveRAG Backend Service Account (${var.environment})"
  description  = "Least-privilege identity for ingestion-api and agent-api Cloud Run services."

  depends_on = [
    google_project_service.required_apis["iam.googleapis.com"],
  ]
}

resource "google_service_account" "frontend_sa" {
  project      = var.project_id
  account_id   = trimsuffix(substr("${var.app_name}-fe-${var.environment}", 0, 28), "-")
  display_name = "InteractiveRAG Frontend Service Account (${var.environment})"
  description  = "Zero-privilege identity for static Nginx frontend Cloud Run service."

  depends_on = [
    google_project_service.required_apis["iam.googleapis.com"],
  ]
}

# Allow backend services to invoke Vertex AI models (gemini-3.8-flash & gemini-embedding-001)
resource "google_project_iam_member" "backend_vertex_ai_user" {
  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.backend_sa.email}"
}

# Scope GCS read/write/lock permissions strictly to the ChromaDB persistence bucket
resource "google_storage_bucket_iam_member" "backend_chroma_bucket_object_user" {
  bucket = google_storage_bucket.chroma_persistence.name
  role   = "roles/storage.objectUser"
  member = "serviceAccount:${google_service_account.backend_sa.email}"
}
