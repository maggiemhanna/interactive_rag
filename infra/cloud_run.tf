# ==============================================================================
# Google Cloud Run Gen2 Microservices (`ingest-api`, `agent-api`, `frontend`)
#
# Shared security & residency properties across all three services:
# - `location = var.region`: Pinned to a single concrete GCP region.
# - `ingress = "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER"`: Blocks direct internet
#   access to `*.run.app` URLs; all external traffic must enter via the HTTPS Load Balancer.
# - `invoker_iam_disabled = true`: Allows the External HTTPS Load Balancer Serverless NEG
#   to forward browser requests without Cloud IAM headers, while FastAPI enforces
#   Firebase / Identity Platform JWT authentication (`Authorization: Bearer <ID_TOKEN>`).
# - `deletion_protection = var.environment == "prod"`: Prevents accidental service deletion
#   in production while keeping `dev` / `staging` environments easy to tear down.
# ==============================================================================

locals {
  ingest_service_name   = "${var.app_name}-ingest-${var.environment}"
  agent_service_name    = "${var.app_name}-agent-${var.environment}"
  frontend_service_name = "${var.app_name}-frontend-${var.environment}"

  # Deterministic Cloud Run regional hostname (`<service>-<project_number>.<region>.run.app`).
  # Computing this upfront avoids circular dependencies between `frontend`, `auth.tf`,
  # and backend `ALLOWED_ORIGINS` CORS lists during initial Terraform provisioning.
  frontend_deterministic_domain = "${local.frontend_service_name}-${data.google_project.current.number}.${var.region}.run.app"
  frontend_deterministic_url    = "https://${local.frontend_deterministic_domain}"

  # Combined CORS allowlist passed to FastAPI (`src/core/config.py`): includes the primary
  # HTTPS Load Balancer origin (`local.lb_url`), the deterministic frontend URL, and any
  # custom origins in `var.extra_allowed_origins`.
  allowed_origins_csv = join(
    ",",
    distinct(concat([local.lb_url, local.frontend_deterministic_url], var.extra_allowed_origins))
  )

  # Backend (`ingest-api` and `agent-api`) shares a single Python Docker image (`Dockerfile.backend`),
  # selecting its FastAPI entrypoint via the `APP_MODULE` environment variable.
  backend_image  = "${local.artifact_registry_url}/backend:${var.image_tag}"
  frontend_image = "${local.artifact_registry_url}/frontend:${var.image_tag}"
}

# ==============================================================================
# 1. Dedicated Write-Path Document Ingestion Service (`src/ingest/app.py`)
# ==============================================================================

# Handles ONLY PDF uploads (`POST /ingest`) and health checks (`GET /health`).
# - `max_instance_request_concurrency = var.ingest_concurrency` (default `4`):
#   Prevents CPU-bound PyPDF parsing and Chroma HNSW indexing from starving concurrent requests.
# - `resources` (`var.ingest_cpu = "2"`, `var.ingest_memory = "4Gi"`): Dedicated
#   vertical memory headroom for `/tmp` PDF staging and ChromaDB segment writes.
# - `vpc_access` (`egress = "ALL_TRAFFIC"` -> `ingest-subnet`): Forces 100% of outbound
#   packets into the zero-internet VPC so untrusted PDFs can never trigger external connections.
resource "google_cloud_run_v2_service" "ingest_api" {
  name                 = local.ingest_service_name
  location             = var.region
  project              = var.project_id
  ingress              = "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER"
  invoker_iam_disabled = true
  deletion_protection  = var.environment == "prod"

  template {
    service_account                  = google_service_account.backend_sa.email
    execution_environment            = "EXECUTION_ENVIRONMENT_GEN2"
    timeout                          = "300s"
    max_instance_request_concurrency = var.ingest_concurrency

    vpc_access {
      egress = "ALL_TRAFFIC"
      network_interfaces {
        network    = google_compute_network.main.name
        subnetwork = google_compute_subnetwork.ingest.name
      }
    }

    scaling {
      min_instance_count = var.ingest_min_instances
      max_instance_count = var.ingest_max_instances
    }

    containers {
      image = local.backend_image

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = var.ingest_cpu
          memory = var.ingest_memory
        }
        cpu_idle          = true
        startup_cpu_boost = true
      }

      env {
        name  = "APP_MODULE"
        value = "src.ingest.app:app"
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
        name  = "CHROMA_TMP_MAX_BYTES"
        value = tostring(var.chroma_tmp_max_bytes)
      }
      env {
        name  = "CHROMA_TMP_MIN_FREE_BYTES"
        value = tostring(var.chroma_tmp_min_free_bytes)
      }
      env {
        name  = "ALLOWED_ORIGINS"
        value = local.allowed_origins_csv
      }
      env {
        name  = "ANONYMIZED_TELEMETRY"
        value = "FALSE"
      }
      env {
        name  = "MODEL_ARMOR_TEMPLATE_ID"
        value = google_model_armor_template.rag_guard.id
      }
      env {
        name  = "MODEL_ARMOR_LOCATION"
        value = var.region
      }
      env {
        name  = "MODEL_ARMOR_FAIL_CLOSED"
        value = tostring(var.model_armor_fail_closed)
      }
      env {
        name  = "ALLOWED_EMBEDDING_MODELS"
        value = var.allowed_embedding_models
      }
      env {
        name  = "MAX_UPLOAD_MB"
        value = tostring(var.max_upload_mb)
      }
      env {
        name  = "MAX_FILES_PER_REQUEST"
        value = tostring(var.max_files_per_request)
      }
      env {
        name  = "MAX_CHUNKS_PER_INGEST"
        value = tostring(var.max_chunks_per_ingest)
      }
      env {
        name  = "RATE_LIMIT_INGEST_PER_MINUTE"
        value = tostring(var.rate_limit_ingest_per_minute)
      }
      env {
        name  = "MAX_COLLECTIONS_PER_USER"
        value = tostring(var.max_collections_per_user)
      }
      env {
        name  = "AUDIT_LOG_NAME"
        value = "interactiverag-audit"
      }
      env {
        name  = "AUDIT_CLOUD_LOGGING_ENABLED"
        value = "true"
      }
      env {
        name  = "TELEMETRY_LOG_NAME"
        value = "interactiverag-telemetry"
      }
      env {
        name  = "STRUCTURED_LOGGING"
        value = "true"
      }
      env {
        name  = "CLOUD_TRACE_ENABLED"
        value = "true"
      }
    }
  }

  # Ensures IAM bindings, Model Armor templates, and Private Google Access / PSC DNS records
  # are active before Cloud Run starts serving traffic.
  depends_on = [
    google_project_service.required_apis["run.googleapis.com"],
    google_project_service.required_apis["logging.googleapis.com"],
    google_project_service.required_apis["monitoring.googleapis.com"],
    google_project_service.required_apis["cloudtrace.googleapis.com"],
    google_project_iam_member.backend_vertex_ai_user,
    google_project_iam_member.backend_model_armor_user,
    google_project_iam_member.backend_dlp_user,
    google_project_iam_member.backend_log_writer,
    google_project_iam_member.backend_metric_writer,
    google_project_iam_member.backend_trace_agent,
    google_storage_bucket_iam_member.backend_chroma_bucket_object_user,
    google_model_armor_template.rag_guard,
    google_compute_route.private_googleapis,
    google_dns_record_set.private_googleapis_a,
    google_dns_record_set.googleapis_wildcard_cname,
    google_network_connectivity_regional_endpoint.modelarmor_psc,
    google_dns_record_set.modelarmor_rep_a,
  ]
}

# ==============================================================================
# 2. Interactive Read & Control-Plane Service (`src/agent/app.py`)
# ==============================================================================

# Mounts `src.agent` (`POST /agent`, `POST /agent/stream`), `src.query` (`POST /query`),
# and `src.manage` (`GET`/`DELETE /collections`, `GET`/`DELETE /compliance/my-data`).
# - `max_instance_request_concurrency = var.agent_concurrency` (default `32`):
#   High concurrency for I/O-bound Vertex AI streaming, similarity search, and collection management.
# - `session_affinity = true`: Routes multi-turn chat, search, and collection requests from the
#   same browser session to the same warm Cloud Run instance so `MEMORY_SAVER` checkpoints,
#   `_QUERY_RATE_LIMITER`, and synced `/tmp/chroma_db/{uid}` caches remain warm across turns.
# - `vpc_access` (`egress = "ALL_TRAFFIC"` -> `agent-subnet`): Isolates interactive read/agent
#   traffic in its own private `/24` subnet separate from `ingest-subnet`.
resource "google_cloud_run_v2_service" "agent_api" {
  name                 = local.agent_service_name
  location             = var.region
  project              = var.project_id
  ingress              = "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER"
  invoker_iam_disabled = true
  deletion_protection  = var.environment == "prod"

  template {
    service_account                  = google_service_account.backend_sa.email
    execution_environment            = "EXECUTION_ENVIRONMENT_GEN2"
    timeout                          = "300s"
    session_affinity                 = true
    max_instance_request_concurrency = var.agent_concurrency

    vpc_access {
      egress = "ALL_TRAFFIC"
      network_interfaces {
        network    = google_compute_network.main.name
        subnetwork = google_compute_subnetwork.agent.name
      }
    }

    scaling {
      min_instance_count = var.agent_min_instances
      max_instance_count = var.agent_max_instances
    }

    containers {
      image = local.backend_image

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = var.agent_cpu
          memory = var.agent_memory
        }
        cpu_idle          = true
        startup_cpu_boost = true
      }

      env {
        name  = "APP_MODULE"
        value = "src.agent.app:app"
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
        name  = "CHROMA_TMP_MAX_BYTES"
        value = tostring(var.chroma_tmp_max_bytes)
      }
      env {
        name  = "CHROMA_TMP_MIN_FREE_BYTES"
        value = tostring(var.chroma_tmp_min_free_bytes)
      }
      env {
        name  = "ALLOWED_ORIGINS"
        value = local.allowed_origins_csv
      }
      env {
        name  = "ANONYMIZED_TELEMETRY"
        value = "FALSE"
      }
      env {
        name  = "MODEL_ARMOR_TEMPLATE_ID"
        value = google_model_armor_template.rag_guard.id
      }
      env {
        name  = "MODEL_ARMOR_LOCATION"
        value = var.region
      }
      env {
        name  = "MODEL_ARMOR_FAIL_CLOSED"
        value = tostring(var.model_armor_fail_closed)
      }
      env {
        name  = "GEMINI_SAFETY_THRESHOLD"
        value = var.gemini_safety_threshold
      }
      env {
        name  = "ALLOWED_LLM_MODELS"
        value = var.allowed_llm_models
      }
      env {
        name  = "ALLOWED_EMBEDDING_MODELS"
        value = var.allowed_embedding_models
      }
      env {
        name  = "MAX_QUERY_CHARS"
        value = tostring(var.max_query_chars)
      }
      env {
        name  = "AGENT_RECURSION_LIMIT"
        value = tostring(var.agent_recursion_limit)
      }
      env {
        name  = "RATE_LIMIT_QUERY_PER_MINUTE"
        value = tostring(var.rate_limit_query_per_minute)
      }
      env {
        name  = "MAX_COLLECTIONS_PER_USER"
        value = tostring(var.max_collections_per_user)
      }
      env {
        name  = "AUDIT_LOG_NAME"
        value = "interactiverag-audit"
      }
      env {
        name  = "AUDIT_CLOUD_LOGGING_ENABLED"
        value = "true"
      }
      env {
        name  = "TELEMETRY_LOG_NAME"
        value = "interactiverag-telemetry"
      }
      env {
        name  = "STRUCTURED_LOGGING"
        value = "true"
      }
      env {
        name  = "CLOUD_TRACE_ENABLED"
        value = "true"
      }
    }
  }

  depends_on = [
    google_project_service.required_apis["run.googleapis.com"],
    google_project_service.required_apis["logging.googleapis.com"],
    google_project_service.required_apis["monitoring.googleapis.com"],
    google_project_service.required_apis["cloudtrace.googleapis.com"],
    google_project_iam_member.backend_vertex_ai_user,
    google_project_iam_member.backend_model_armor_user,
    google_project_iam_member.backend_dlp_user,
    google_project_iam_member.backend_log_writer,
    google_project_iam_member.backend_metric_writer,
    google_project_iam_member.backend_trace_agent,
    google_storage_bucket_iam_member.backend_chroma_bucket_object_user,
    google_model_armor_template.rag_guard,
    google_compute_route.private_googleapis,
    google_dns_record_set.private_googleapis_a,
    google_dns_record_set.googleapis_wildcard_cname,
    google_network_connectivity_regional_endpoint.modelarmor_psc,
    google_dns_record_set.modelarmor_rep_a,
  ]
}

# ==============================================================================
# 3. React SPA + Nginx Frontend Service (`docker/Dockerfile.frontend`)
# ==============================================================================

# Serves static React assets and generates `/usr/share/nginx/html/env-config.js` at container
# startup (`docker/entrypoint-frontend.sh`) from the environment variables below:
# - `INGESTION_API_URL` & `AGENT_API_URL`: Same-origin relative paths (`/api/ingest` and
#   `/api/agent`) routed by the External HTTPS Load Balancer (`load_balancer.tf`).
# - `FIREBASE_API_KEY`: Auto-populated directly from `google_identity_platform_config.auth`
#   (`auth.tf`) after Identity Platform is provisioned.
resource "google_cloud_run_v2_service" "frontend" {
  name                 = local.frontend_service_name
  location             = var.region
  project              = var.project_id
  ingress              = "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER"
  invoker_iam_disabled = true
  deletion_protection  = var.environment == "prod"

  template {
    service_account                  = google_service_account.frontend_sa.email
    execution_environment            = "EXECUTION_ENVIRONMENT_GEN2"
    timeout                          = "60s"
    max_instance_request_concurrency = var.frontend_concurrency

    scaling {
      min_instance_count = var.frontend_min_instances
      max_instance_count = var.frontend_max_instances
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
        value = "/api/ingest"
      }
      env {
        name  = "AGENT_API_URL"
        value = "/api/agent"
      }
      env {
        name  = "FIREBASE_API_KEY"
        value = try(google_identity_platform_config.auth.client[0].api_key, "")
      }
      env {
        name  = "FIREBASE_AUTH_DOMAIN"
        value = "${var.project_id}.firebaseapp.com"
      }
      env {
        name  = "FIREBASE_PROJECT_ID"
        value = var.project_id
      }
    }
  }

  depends_on = [
    google_project_service.required_apis["run.googleapis.com"],
    google_identity_platform_config.auth,
  ]
}

