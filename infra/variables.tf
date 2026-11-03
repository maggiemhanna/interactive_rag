# ==============================================================================
# 1. Core Project, Single-Region Residency & Deployment Identity
# ==============================================================================

variable "project_id" {
  description = "GCP Project ID where InteractiveRAG will be deployed."
  type        = string
  default     = "interactive-rag"
}

# Enforces concrete single-region syntax (e.g., `us-central1`, `europe-west1`) so `"global"`
# and multi-regions (`"us"`, `"eu"`, `"asia"`) are rejected at `terraform plan` time.
# Note: Pre-deploy `make check-region-policy` additionally verifies that this region is
# permitted by the project's effective `constraints/gcp.resourceLocations` Organization Policy.
variable "region" {
  description = "Concrete single GCP region for Cloud Run, Artifact Registry, GCS, and Vertex AI (must satisfy project gcp.resourceLocations Organization Policy)."
  type        = string
  default     = "us-central1"

  validation {
    condition     = can(regex("^[a-z]+-[a-z]+[0-9]+$", var.region))
    error_message = "var.region must be a concrete single GCP region (e.g., 'us-central1', 'us-west1'), never 'global' or a multi-region ('us', 'eu', 'asia')."
  }
}

variable "app_name" {
  description = "Prefix used for naming GCP resources."
  type        = string
  default     = "interactive-rag"
}

# Controls environment naming (`dev`, `staging`, `prod`) and gates Cloud Run
# `deletion_protection = var.environment == "prod"` in `cloud_run.tf`.
variable "environment" {
  description = "Deployment environment label (e.g., dev, staging, prod)."
  type        = string
  default     = "prod"
}

variable "image_tag" {
  description = "Docker image tag in Artifact Registry to deploy (e.g., git SHA or 'latest')."
  type        = string
  default     = "latest"
}

# ==============================================================================
# 2. Cloud Run Per-Service Sizing, Concurrency, Autoscaling & /tmp Cache Quotas
# ==============================================================================

# Write-heavy ingest service (`ingest-api`: `POST /ingest` + `/health`)
# Sized with higher memory (`4Gi`) for PyPDF + ChromaDB HNSW segment construction in `/tmp`
# and low concurrency (`4`) so concurrent PDF uploads do not starve each other on CPU/RAM.
variable "ingest_cpu" {
  description = "CPU limit for the ingest-api Cloud Run container."
  type        = string
  default     = "2"
}

variable "ingest_memory" {
  description = "Memory limit for the ingest-api Cloud Run container (shared with /tmp tmpfs ChromaDB & upload staging)."
  type        = string
  default     = "4Gi"
}

variable "ingest_concurrency" {
  description = "Maximum concurrent requests per ingest-api Cloud Run container instance (low concurrency isolates CPU-bound PDF parsing)."
  type        = number
  default     = 4

  validation {
    condition     = var.ingest_concurrency >= 1 && var.ingest_concurrency <= 80 && floor(var.ingest_concurrency) == var.ingest_concurrency
    error_message = "ingest_concurrency must be an integer between 1 and 80."
  }
}

variable "ingest_min_instances" {
  description = "Minimum warm Cloud Run instances for ingest-api (0 enables scale-to-zero when no uploads are active)."
  type        = number
  default     = 0
}

variable "ingest_max_instances" {
  description = "Maximum Cloud Run instances for ingest-api."
  type        = number
  default     = 5
}

# Interactive read & control-plane service (`agent-api`: `/agent`, `/agent/stream`, `/query`, `/collections`, `/compliance/my-data`)
# Sized for high-concurrency I/O-bound Vertex AI streaming (`32`) and higher horizontal scale-out (`10`).
variable "agent_cpu" {
  description = "CPU limit for the agent-api Cloud Run container."
  type        = string
  default     = "2"
}

# Note: In Cloud Run Gen2, `/tmp` is an in-memory `tmpfs` filesystem that shares
# this container memory limit when syncing per-user ChromaDB vector stores.
variable "agent_memory" {
  description = "Memory limit for the agent-api Cloud Run container (shared with warm /tmp tmpfs ChromaDB cache)."
  type        = string
  default     = "2Gi"
}

variable "agent_concurrency" {
  description = "Maximum concurrent requests per agent-api Cloud Run container instance (tuned for I/O-bound SSE streaming, /query, and /collections)."
  type        = number
  default     = 32

  validation {
    condition     = var.agent_concurrency >= 1 && var.agent_concurrency <= 1000 && floor(var.agent_concurrency) == var.agent_concurrency
    error_message = "agent_concurrency must be an integer between 1 and 1000."
  }
}

variable "agent_min_instances" {
  description = "Minimum warm Cloud Run instances for agent-api (set >= 1 in latency-sensitive prod to eliminate cold starts)."
  type        = number
  default     = 0
}

variable "agent_max_instances" {
  description = "Maximum Cloud Run instances for agent-api."
  type        = number
  default     = 10
}

# Static React SPA + Nginx frontend service (`frontend`)
variable "frontend_cpu" {
  description = "CPU limit for frontend Nginx Cloud Run container."
  type        = string
  default     = "1"
}

variable "frontend_memory" {
  description = "Memory limit for frontend Nginx Cloud Run container."
  type        = string
  default     = "512Mi"
}

variable "frontend_concurrency" {
  description = "Maximum concurrent requests per frontend Nginx Cloud Run container instance."
  type        = number
  default     = 80

  validation {
    condition     = var.frontend_concurrency >= 1 && var.frontend_concurrency <= 1000 && floor(var.frontend_concurrency) == var.frontend_concurrency
    error_message = "frontend_concurrency must be an integer between 1 and 1000."
  }
}

variable "frontend_min_instances" {
  description = "Minimum warm Cloud Run instances for frontend (0 enables scale-to-zero)."
  type        = number
  default     = 0
}

variable "frontend_max_instances" {
  description = "Maximum Cloud Run instances for frontend."
  type        = number
  default     = 5
}

# Ephemeral `/tmp/chroma_db` LRU cache eviction quotas
variable "chroma_tmp_max_bytes" {
  description = "Maximum total bytes allowed in /tmp/chroma_db before LRU tenant cache eviction runs (default 512 MiB)."
  type        = number
  default     = 536870912

  validation {
    condition     = var.chroma_tmp_max_bytes >= 67108864 && floor(var.chroma_tmp_max_bytes) == var.chroma_tmp_max_bytes
    error_message = "chroma_tmp_max_bytes must be an integer >= 67108864 (64 MiB)."
  }
}

variable "chroma_tmp_min_free_bytes" {
  description = "Minimum free bytes required on the /tmp RAM disk before LRU tenant cache eviction runs (default 256 MiB)."
  type        = number
  default     = 268435456

  validation {
    condition     = var.chroma_tmp_min_free_bytes >= 67108864 && floor(var.chroma_tmp_min_free_bytes) == var.chroma_tmp_min_free_bytes
    error_message = "chroma_tmp_min_free_bytes must be an integer >= 67108864 (64 MiB)."
  }
}

variable "bucket_force_destroy" {
  description = "Allow Terraform to delete the ChromaDB GCS bucket even if non-empty (set true only in dev/ephemeral environments)."
  type        = bool
  default     = false
}

# ==============================================================================
# 3. Authentication, CORS & Secret Manager References
# ==============================================================================

variable "extra_allowed_origins" {
  description = "Additional CORS origins allowed to call the backend APIs (e.g., http://localhost:3000 or custom domains)."
  type        = list(string)
  default     = ["http://localhost:3000"]
}

variable "enable_google_oauth" {
  description = "Enable Google OAuth Sign-In provider in Identity Platform (loads credentials from GCP Secret Manager)."
  type        = bool
  default     = false
}

# Stores ONLY the Secret Manager resource names (never the secret values themselves);
# `auth.tf` fetches the actual OAuth credentials from GCP Secret Manager at apply time.
variable "google_oauth_client_id_secret_name" {
  description = "Name of the GCP Secret Manager secret holding the Google OAuth 2.0 Web Client ID."
  type        = string
  default     = "google_oauth_client_id"
}

variable "google_oauth_client_secret_name" {
  description = "Name of the GCP Secret Manager secret holding the Google OAuth 2.0 Web Client Secret."
  type        = string
  default     = "google_oauth_client_secret"
}

# ==============================================================================
# 4. Private VPC Subnets (Direct VPC Egress & Regional Private Service Connect)
# ==============================================================================

# Cloud Run Direct VPC Egress reserves 2x IP addresses per max_instance_count during revision
# rollouts and autoscaling spikes. Default `/24` (256 IPs) subnets prevent IP exhaustion when
# scaling `max_instance_count` beyond 25–30 instances.
variable "ingest_subnet_cidr" {
  description = "Private CIDR range for the ingest-api Cloud Run Direct VPC Egress subnet (default /24 = 256 IPs; minimum /26)."
  type        = string
  default     = "10.10.1.0/24"

  validation {
    condition     = can(cidrhost(var.ingest_subnet_cidr, 0)) && tonumber(split("/", var.ingest_subnet_cidr)[1]) <= 26
    error_message = "ingest_subnet_cidr must be a valid IPv4 CIDR block with prefix length <= 26 (e.g., 10.10.1.0/24)."
  }
}

variable "agent_subnet_cidr" {
  description = "Private CIDR range for the agent-api Cloud Run Direct VPC Egress subnet (default /24 = 256 IPs; minimum /26)."
  type        = string
  default     = "10.10.2.0/24"

  validation {
    condition     = can(cidrhost(var.agent_subnet_cidr, 0)) && tonumber(split("/", var.agent_subnet_cidr)[1]) <= 26
    error_message = "agent_subnet_cidr must be a valid IPv4 CIDR block with prefix length <= 26 (e.g., 10.10.2.0/24)."
  }
}

variable "psc_subnet_cidr" {
  description = "Private CIDR range for internal regional Private Service Connect endpoints (e.g., Model Armor)."
  type        = string
  default     = "10.10.3.0/28"

  validation {
    condition     = can(cidrhost(var.psc_subnet_cidr, 0))
    error_message = "psc_subnet_cidr must be a valid IPv4 CIDR block (e.g., 10.10.3.0/28)."
  }
}

# ==============================================================================
# 5. AI-Specific Security (Model Allowlists, Safety Thresholds & Upload Ceilings)
# ==============================================================================

variable "allowed_llm_models" {
  description = "Comma-separated allowlist of approved regional Vertex AI generative LLM models."
  type        = string
  default     = "gemini-2.5-flash,gemini-2.5-pro,gemini-2.5-flash-lite"
}

variable "allowed_embedding_models" {
  description = "Comma-separated allowlist of approved regional Vertex AI embedding models."
  type        = string
  default     = "gemini-embedding-001,text-embedding-005"
}

variable "gemini_safety_threshold" {
  description = "Gemini HarmBlockThreshold applied to all content safety categories."
  type        = string
  default     = "BLOCK_LOW_AND_ABOVE"

  validation {
    condition = contains(
      ["BLOCK_LOW_AND_ABOVE", "BLOCK_MEDIUM_AND_ABOVE", "BLOCK_ONLY_HIGH"],
      var.gemini_safety_threshold
    )
    error_message = "gemini_safety_threshold must be BLOCK_LOW_AND_ABOVE, BLOCK_MEDIUM_AND_ABOVE, or BLOCK_ONLY_HIGH."
  }
}

variable "model_armor_pi_confidence" {
  description = "Model Armor detection confidence threshold for Prompt Injection and Jailbreak screening."
  type        = string
  default     = "MEDIUM_AND_ABOVE"

  validation {
    condition     = contains(["LOW_AND_ABOVE", "MEDIUM_AND_ABOVE", "HIGH"], var.model_armor_pi_confidence)
    error_message = "model_armor_pi_confidence must be LOW_AND_ABOVE, MEDIUM_AND_ABOVE, or HIGH."
  }
}

variable "model_armor_rai_confidence" {
  description = "Model Armor detection confidence threshold for Responsible AI harm categories."
  type        = string
  default     = "MEDIUM_AND_ABOVE"

  validation {
    condition     = contains(["LOW_AND_ABOVE", "MEDIUM_AND_ABOVE", "HIGH"], var.model_armor_rai_confidence)
    error_message = "model_armor_rai_confidence must be LOW_AND_ABOVE, MEDIUM_AND_ABOVE, or HIGH."
  }
}

variable "max_upload_mb" {
  description = "Maximum allowed file size (in MiB) per uploaded PDF."
  type        = number
  default     = 20
}

variable "max_files_per_request" {
  description = "Maximum number of PDF files accepted in a single ingestion request."
  type        = number
  default     = 10
}

variable "max_chunks_per_ingest" {
  description = "Maximum total text chunks allowed in a single ingestion request."
  type        = number
  default     = 1000
}

variable "max_query_chars" {
  description = "Maximum character length allowed for user search and agent queries."
  type        = number
  default     = 4000
}

variable "agent_recursion_limit" {
  description = "LangGraph recursion_limit ceiling bounding back-and-forth retrieval loops."
  type        = number
  default     = 6
}

# ==============================================================================
# 6. Compliance, Audit Logging & Runtime Policy Enforcement
# ==============================================================================

variable "audit_log_retention_days" {
  description = "Retention period in days for the regional Cloud Logging compliance audit bucket (1 to 3650 days)."
  type        = number
  default     = 365

  validation {
    condition     = var.audit_log_retention_days >= 1 && var.audit_log_retention_days <= 3650
    error_message = "audit_log_retention_days must be between 1 and 3650 days."
  }
}

variable "audit_log_bucket_locked" {
  description = "Irreversibly lock retention on the regional Cloud Logging audit bucket (WORM immutability; enable only in strict regulated production)."
  type        = bool
  default     = false
}

variable "lb_log_sample_rate" {
  description = "Sampling rate (0.0 to 1.0) for External HTTPS Load Balancer HTTP access logs on backend APIs."
  type        = number
  default     = 1.0

  validation {
    condition     = var.lb_log_sample_rate >= 0.0 && var.lb_log_sample_rate <= 1.0
    error_message = "lb_log_sample_rate must be between 0.0 and 1.0."
  }
}

variable "model_armor_fail_closed" {
  description = "Fail closed with HTTP 503 when Cloud Model Armor or Cloud DLP screening is unconfigured or unavailable."
  type        = bool
  default     = true
}

variable "rate_limit_ingest_per_minute" {
  description = "Maximum PDF ingestion requests permitted per authenticated user per sliding 60-second window."
  type        = number
  default     = 10

  validation {
    condition     = var.rate_limit_ingest_per_minute >= 1 && floor(var.rate_limit_ingest_per_minute) == var.rate_limit_ingest_per_minute
    error_message = "rate_limit_ingest_per_minute must be a positive integer (>= 1)."
  }
}

variable "rate_limit_query_per_minute" {
  description = "Maximum vector search or agent query requests permitted per authenticated user per sliding 60-second window."
  type        = number
  default     = 30

  validation {
    condition     = var.rate_limit_query_per_minute >= 1 && floor(var.rate_limit_query_per_minute) == var.rate_limit_query_per_minute
    error_message = "rate_limit_query_per_minute must be a positive integer (>= 1)."
  }
}

variable "max_collections_per_user" {
  description = "Maximum number of distinct ChromaDB collections an authenticated user may create."
  type        = number
  default     = 20

  validation {
    condition     = var.max_collections_per_user >= 1 && floor(var.max_collections_per_user) == var.max_collections_per_user
    error_message = "max_collections_per_user must be a positive integer (>= 1)."
  }
}

# ==============================================================================
# 7. Observability, AI Monitoring & Alerting Thresholds
# ==============================================================================

variable "observability_log_retention_days" {
  description = "Retention period in days for the regional Cloud Logging observability & telemetry bucket (1 to 3650 days)."
  type        = number
  default     = 30

  validation {
    condition     = var.observability_log_retention_days >= 1 && var.observability_log_retention_days <= 3650
    error_message = "observability_log_retention_days must be between 1 and 3650 days."
  }
}

variable "alert_agent_latency_p95_ms" {
  description = "Threshold in milliseconds for the p95 Agentic RAG turn latency Cloud Monitoring alert policy."
  type        = number
  default     = 15000

  validation {
    condition     = var.alert_agent_latency_p95_ms >= 1000
    error_message = "alert_agent_latency_p95_ms must be >= 1000 ms."
  }
}

variable "alert_memory_utilization_threshold" {
  description = "Threshold ratio (0.50 to 0.99) for the Cloud Run container memory + /tmp tmpfs utilization alert policy."
  type        = number
  default     = 0.85

  validation {
    condition     = var.alert_memory_utilization_threshold >= 0.50 && var.alert_memory_utilization_threshold <= 0.99
    error_message = "alert_memory_utilization_threshold must be between 0.50 and 0.99."
  }
}

