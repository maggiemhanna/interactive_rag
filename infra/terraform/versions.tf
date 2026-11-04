# ==============================================================================
# Terraform Core Version, Remote GCS Backend & Google Cloud Providers
# ==============================================================================

terraform {
  required_version = ">= 1.5.0"

  # Remote state is stored in a versioned Google Cloud Storage bucket (`gs://<app>-tfstate-<project_number>`).
  # Bucket and prefix parameters are injected dynamically during `make tf-init` via `-backend-config`
  # so this file stays portable across projects and environments without hardcoding bucket names.
  backend "gcs" {}

  required_providers {
    # Standard GA provider used for Cloud Run, VPC, Load Balancer, Cloud Storage, DLP, Model Armor, and Logging.
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
    # Beta provider required for `google_identity_platform_config` (`client {}` API key auto-generation)
    # and `google_project_service_identity` (provisioning Google-managed service agents).
    google-beta = {
      source  = "hashicorp/google-beta"
      version = "~> 6.0"
    }
  }
}

# Default regional provider configuration. All regional resources explicitly pin `location = var.region`
# or `region = var.region` to enforce single-region data residency.
provider "google" {
  project = var.project_id
  region  = var.region
}

provider "google-beta" {
  project = var.project_id
  region  = var.region
}
