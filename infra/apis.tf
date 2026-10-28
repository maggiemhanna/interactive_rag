# ==============================================================================
# 1. Required Google Cloud APIs & Project Metadata
# ==============================================================================

locals {
  # Complete set of Google Cloud APIs required across all four architectural layers:
  # - Layer 1 (Compute, Storage & Auth): Cloud Run, Artifact Registry, Cloud Build, Cloud Storage,
  #   IAM, Identity Platform (`identitytoolkit`), API Keys, and Secret Manager.
  # - Layer 2 (Network Security): Compute Engine (VPC/Load Balancer), Cloud DNS, and Network Connectivity (PSC).
  # - Layer 3 (AI & Security): Vertex AI (`aiplatform`), Cloud Model Armor, and Sensitive Data Protection (`dlp`).
  # - Layer 4 (Compliance & Audit): Cloud Logging (`logging`).
  required_services = toset([
    "run.googleapis.com",
    "artifactregistry.googleapis.com",
    "cloudbuild.googleapis.com",
    "compute.googleapis.com",
    "dns.googleapis.com",
    "networkconnectivity.googleapis.com",
    "aiplatform.googleapis.com",
    "modelarmor.googleapis.com",
    "dlp.googleapis.com",
    "storage.googleapis.com",
    "iam.googleapis.com",
    "identitytoolkit.googleapis.com",
    "apikeys.googleapis.com",
    "secretmanager.googleapis.com",
    "logging.googleapis.com",
  ])

  # Base URL for pushing and pulling regional backend and frontend Docker images.
  artifact_registry_url = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.docker_repo.repository_id}"
}

# Enables each required API on the target project.
# `disable_on_destroy = false` prevents `terraform destroy` from accidentally deactivating
# project-wide APIs that could break shared state buckets or Cloud Build pipelines.
resource "google_project_service" "required_apis" {
  for_each           = local.required_services
  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

# Looks up the numeric `project.number` (used to construct globally unique GCS bucket names
# and deterministic Cloud Run service hostnames).
data "google_project" "current" {
  project_id = var.project_id
}

# ==============================================================================
# 2. Regional Artifact Registry Docker Repository
# ==============================================================================

# Stores the `backend` and `frontend` container images in `var.region`.
# Includes an automatic cleanup policy retaining the 10 most recent image versions
# so repeated CI/CD builds do not accumulate unbounded storage costs.
resource "google_artifact_registry_repository" "docker_repo" {
  project       = var.project_id
  location      = var.region
  repository_id = "${var.app_name}-repo"
  description   = "Container images for InteractiveRAG (${var.environment})"
  format        = "DOCKER"

  cleanup_policies {
    id     = "keep-recent-images"
    action = "KEEP"
    most_recent_versions {
      keep_count = 10
    }
  }

  depends_on = [
    google_project_service.required_apis["artifactregistry.googleapis.com"],
  ]
}
