# ==============================================================================
# Regional Multi-Tenant Persistence Bucket (ChromaDB Snapshots & PDF Archives)
#
# Bucket layout per authenticated user (`uid`):
#   gs://<bucket>/users/<uid>/chroma_db/  -> SQLite + HNSW vector index + `version.json`
#   gs://<bucket>/users/<uid>/uploads/    -> Archived original PDF uploads
#   gs://<bucket>/users/<uid>/locks/      -> Distributed write lock (`chroma.lock`)
# ==============================================================================

# Private regional Cloud Storage bucket pinned to `var.region`.
# - `uniform_bucket_level_access = true`: Disables legacy per-object ACLs so access is governed
#   exclusively by IAM (`backend_chroma_bucket_object_user` in `iam.tf`).
# - `public_access_prevention = "enforced"`: Blocks any accidental public bucket/object sharing.
# - `versioning.enabled = true`: Protects against accidental overwrite during concurrent syncs;
#   note that GDPR Art. 17 (`DELETE /compliance/my-data`) explicitly purges both live and
#   noncurrent object generations (`versions=True`).
resource "google_storage_bucket" "chroma_persistence" {
  name                        = "${var.app_name}-chroma-${var.environment}-${data.google_project.current.number}"
  project                     = var.project_id
  location                    = var.region
  storage_class               = "STANDARD"
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = var.bucket_force_destroy

  versioning {
    enabled = true
  }

  # Lifecycle Rule 1 (Time-Based Hygiene):
  # Automatically deletes archived (noncurrent) object versions 7 days after they are superseded
  # so frequent ChromaDB SQLite syncs do not accumulate unbounded historical storage costs.
  lifecycle_rule {
    action {
      type = "Delete"
    }
    condition {
      days_since_noncurrent_time = 7
      with_state                 = "ARCHIVED"
    }
  }

  # Lifecycle Rule 2 (Version-Count Hygiene):
  # Retains at most 3 noncurrent snapshots per file, pruning older generations automatically.
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

  # Data Residency Precondition:
  # Rejects `"global"` or multi-regional (`"us"`, `"eu"`, `"asia"`) bucket locations at plan time.
  lifecycle {
    precondition {
      condition     = can(regex("^[a-z]+-[a-z]+[0-9]+$", var.region))
      error_message = "ChromaDB persistence bucket must be provisioned in a concrete single GCP region (never 'global' or a multi-region)."
    }
  }

  depends_on = [
    google_project_service.required_apis["storage.googleapis.com"],
  ]
}
