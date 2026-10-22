resource "google_storage_bucket" "chroma_persistence" {
  name                        = var.project_id == var.app_name ? "${var.project_id}-chroma-${var.environment}" : "${var.project_id}-${var.app_name}-chroma-${var.environment}"
  project                     = var.project_id
  location                    = var.region
  storage_class               = "STANDARD"
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = var.bucket_force_destroy

  versioning {
    enabled = true
  }

  # Prune archived object versions after 7 days so frequent Chroma SQLite syncs do not bloat storage
  lifecycle_rule {
    action {
      type = "Delete"
    }
    condition {
      days_since_noncurrent_time = 7
      with_state                 = "ARCHIVED"
    }
  }

  # Keep at most 3 noncurrent snapshots per file
  lifecycle_rule {
    action {
      type = "Delete"
    }
    condition {
      num_newer_versions = 3
      with_state         = "ARCHIVED"
    }
  }

  labels = {
    app         = var.app_name
    environment = var.environment
    purpose     = "per-user-chromadb-persistence"
  }

  depends_on = [
    google_project_service.required_apis["storage.googleapis.com"],
  ]
}
