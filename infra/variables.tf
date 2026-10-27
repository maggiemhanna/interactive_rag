variable "project_id" {
  description = "GCP Project ID where InteractiveRAG will be deployed."
  type        = string
  default     = "interactive-rag"
}

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

variable "backend_cpu" {
  description = "CPU limit for ingestion-api and agent-api Cloud Run containers."
  type        = string
  default     = "2"
}

variable "backend_memory" {
  description = "Memory limit for backend containers (shared with /tmp tmpfs ChromaDB cache)."
  type        = string
  default     = "2Gi"
}

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

variable "min_instance_count" {
  description = "Minimum warm Cloud Run instances per service (0 enables scale-to-zero)."
  type        = number
  default     = 0
}

variable "max_instance_count" {
  description = "Maximum Cloud Run instances per service."
  type        = number
  default     = 5
}

variable "bucket_force_destroy" {
  description = "Allow Terraform to delete the ChromaDB GCS bucket even if non-empty (set true only in dev/ephemeral environments)."
  type        = bool
  default     = false
}

variable "extra_allowed_origins" {
  description = "Additional CORS origins allowed to call the backend APIs (e.g., http://localhost:3000 or custom domains)."
  type        = list(string)
  default     = ["http://localhost:3000"]
}

variable "firebase_api_key" {
  description = "Identity Platform / Firebase Web API Key injected into frontend /env-config.js."
  type        = string
  default     = ""
  sensitive   = true
}

variable "firebase_app_id" {
  description = "Optional Firebase Web App ID injected into frontend /env-config.js."
  type        = string
  default     = ""
}

variable "enable_google_oauth" {
  description = "Enable Google OAuth Sign-In provider in Identity Platform (loads credentials from GCP Secret Manager)."
  type        = bool
  default     = false
}

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

variable "ingestion_subnet_cidr" {
  description = "Private CIDR range for the ingestion-api Cloud Run Direct VPC Egress subnet (minimum /26 recommended)."
  type        = string
  default     = "10.10.1.0/26"

  validation {
    condition     = can(cidrhost(var.ingestion_subnet_cidr, 0))
    error_message = "ingestion_subnet_cidr must be a valid IPv4 CIDR block (e.g., 10.10.1.0/26)."
  }
}

variable "agent_subnet_cidr" {
  description = "Private CIDR range for the agent-api Cloud Run Direct VPC Egress subnet (minimum /26 recommended)."
  type        = string
  default     = "10.10.2.0/26"

  validation {
    condition     = can(cidrhost(var.agent_subnet_cidr, 0))
    error_message = "agent_subnet_cidr must be a valid IPv4 CIDR block (e.g., 10.10.2.0/26)."
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

