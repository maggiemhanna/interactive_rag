variable "project_id" {
  description = "GCP Project ID where InteractiveRAG will be deployed."
  type        = string
}

variable "region" {
  description = "GCP region for Cloud Run, Artifact Registry, GCS, and Vertex AI."
  type        = string
  default     = "us-central1"
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
  description = "Enable Google OAuth Sign-In provider in Identity Platform (requires google_oauth_client_id & secret)."
  type        = bool
  default     = false
}

variable "google_oauth_client_id" {
  description = "OAuth 2.0 Web Client ID for Google Sign-In."
  type        = string
  default     = ""
}

variable "google_oauth_client_secret" {
  description = "OAuth 2.0 Web Client Secret for Google Sign-In."
  type        = string
  default     = ""
  sensitive   = true
}
