locals {
  ingestion_service_name = "${var.app_name}-ingestion-${var.environment}"
  agent_service_name     = "${var.app_name}-agent-${var.environment}"
  frontend_service_name  = "${var.app_name}-frontend-${var.environment}"

  # Deterministic Cloud Run regional hostname breaks any circular dependency between CORS and frontend env vars
  frontend_deterministic_domain = "${local.frontend_service_name}-${data.google_project.current.number}.${var.region}.run.app"
  frontend_deterministic_url    = "https://${local.frontend_deterministic_domain}"

  allowed_origins_csv = join(
    ",",
    distinct(concat([local.frontend_deterministic_url], var.extra_allowed_origins))
  )

  backend_image  = "${local.artifact_registry_url}/backend:${var.image_tag}"
  frontend_image = "${local.artifact_registry_url}/frontend:${var.image_tag}"
}

# 1. Document Ingestion & Collection Management Service (src/add_documents.py)
resource "google_cloud_run_v2_service" "ingestion_api" {
  name                = local.ingestion_service_name
  location            = var.region
  project             = var.project_id
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = false

  template {
    service_account       = google_service_account.backend_sa.email
    execution_environment = "EXECUTION_ENVIRONMENT_GEN2"
    timeout               = "300s"

    scaling {
      min_instance_count = var.min_instance_count
      max_instance_count = var.max_instance_count
    }

    containers {
      image = local.backend_image

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = var.backend_cpu
          memory = var.backend_memory
        }
        cpu_idle          = true
        startup_cpu_boost = true
      }

      env {
        name  = "APP_MODULE"
        value = "src.add_documents:app"
      }
      env {
        name  = "GOOGLE_CLOUD_PROJECT"
        value = var.project_id
      }
      env {
        name  = "GOOGLE_CLOUD_LOCATION"
        value = var.region
      }
      env {
        name  = "GCS_BUCKET_NAME"
        value = google_storage_bucket.chroma_persistence.name
      }
      env {
        name  = "LOCAL_CHROMA_ROOT"
        value = "/tmp/chroma_db"
      }
      env {
        name  = "ALLOWED_ORIGINS"
        value = local.allowed_origins_csv
      }
    }
  }

  depends_on = [
    google_project_service.required_apis["run.googleapis.com"],
    google_project_iam_member.backend_vertex_ai_user,
    google_storage_bucket_iam_member.backend_chroma_bucket_object_user,
  ]
}

# 2. LangGraph Agentic RAG & SSE Streaming Service (src/agentic_rag.py)
resource "google_cloud_run_v2_service" "agent_api" {
  name                = local.agent_service_name
  location            = var.region
  project             = var.project_id
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = false

  template {
    service_account       = google_service_account.backend_sa.email
    execution_environment = "EXECUTION_ENVIRONMENT_GEN2"
    timeout               = "300s"
    session_affinity      = true

    scaling {
      min_instance_count = var.min_instance_count
      max_instance_count = var.max_instance_count
    }

    containers {
      image = local.backend_image

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = var.backend_cpu
          memory = var.backend_memory
        }
        cpu_idle          = true
        startup_cpu_boost = true
      }

      env {
        name  = "APP_MODULE"
        value = "src.agentic_rag:app"
      }
      env {
        name  = "GOOGLE_CLOUD_PROJECT"
        value = var.project_id
      }
      env {
        name  = "GOOGLE_CLOUD_LOCATION"
        value = var.region
      }
      env {
        name  = "GCS_BUCKET_NAME"
        value = google_storage_bucket.chroma_persistence.name
      }
      env {
        name  = "LOCAL_CHROMA_ROOT"
        value = "/tmp/chroma_db"
      }
      env {
        name  = "ALLOWED_ORIGINS"
        value = local.allowed_origins_csv
      }
    }
  }

  depends_on = [
    google_project_service.required_apis["run.googleapis.com"],
    google_project_iam_member.backend_vertex_ai_user,
    google_storage_bucket_iam_member.backend_chroma_bucket_object_user,
  ]
}

# 3. React SPA + Nginx Frontend Service
resource "google_cloud_run_v2_service" "frontend" {
  name                = local.frontend_service_name
  location            = var.region
  project             = var.project_id
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = false

  template {
    service_account       = google_service_account.frontend_sa.email
    execution_environment = "EXECUTION_ENVIRONMENT_GEN2"
    timeout               = "60s"

    scaling {
      min_instance_count = var.min_instance_count
      max_instance_count = var.max_instance_count
    }

    containers {
      image = local.frontend_image

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = var.frontend_cpu
          memory = var.frontend_memory
        }
        cpu_idle          = true
        startup_cpu_boost = true
      }

      env {
        name  = "INGESTION_API_URL"
        value = google_cloud_run_v2_service.ingestion_api.uri
      }
      env {
        name  = "AGENT_API_URL"
        value = google_cloud_run_v2_service.agent_api.uri
      }
      env {
        name  = "FIREBASE_API_KEY"
        value = var.firebase_api_key
      }
      env {
        name  = "FIREBASE_AUTH_DOMAIN"
        value = "${var.project_id}.firebaseapp.com"
      }
      env {
        name  = "FIREBASE_PROJECT_ID"
        value = var.project_id
      }
      env {
        name  = "FIREBASE_APP_ID"
        value = var.firebase_app_id
      }
    }
  }

  depends_on = [
    google_project_service.required_apis["run.googleapis.com"],
  ]
}

# Allow public HTTPS reachability (user access to backend APIs is strictly enforced via Firebase JWT middleware)
resource "google_cloud_run_v2_service_iam_member" "public_invoker" {
  for_each = {
    ingestion = google_cloud_run_v2_service.ingestion_api
    agent     = google_cloud_run_v2_service.agent_api
    frontend  = google_cloud_run_v2_service.frontend
  }

  project  = each.value.project
  location = each.value.location
  name     = each.value.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}
