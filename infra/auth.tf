# ==============================================================================
# 1. Google Cloud Identity Platform (Firebase Authentication) Configuration
# ==============================================================================

# Enables Identity Platform on the GCP project for zero-trust user authentication:
# - `email.enabled = true`: Supports standard Email & Password sign-up and sign-in.
# - `client {}`: Instructs Identity Platform to generate and expose the browser Web API Key
#   (`client[0].api_key`), which Terraform injects directly into the `frontend` Cloud Run
#   container (`cloud_run.tf`) without requiring manual copy-pasting.
# - `authorized_domains`: Whitelists the exact domains permitted to initiate OAuth / Firebase
#   sign-in flows, including the Load Balancer's `<IP-with-dashes>.sslip.io` domain.
resource "google_identity_platform_config" "auth" {
  provider = google-beta
  project  = var.project_id

  autodelete_anonymous_users = true

  sign_in {
    allow_duplicate_emails = false

    email {
      enabled           = true
      password_required = true
    }
  }

  # Ensures Identity Platform generates & exposes client[0].api_key for the frontend
  client {}

  authorized_domains = distinct(concat(
    [
      "localhost",
      "127.0.0.1",
      "${var.project_id}.firebaseapp.com",
      "${var.project_id}.web.app",
      local.frontend_deterministic_domain,
      local.lb_domain,
    ],
    [
      for origin in var.extra_allowed_origins :
      split(":", replace(replace(origin, "https://", ""), "http://", ""))[0]
      if origin != "*" && trimspace(origin) != ""
    ]
  ))

  depends_on = [
    google_project_service.required_apis["identitytoolkit.googleapis.com"],
  ]
}

# ==============================================================================
# 2. Optional Google OAuth 2.0 Sign-In Provider (Loaded from Secret Manager)
#    Why Secret Manager: OAuth Client Secrets must never be stored in `.tf` files
#    or `terraform.tfvars`. Instead, `variables.tf` only holds the Secret Manager
#    resource names, and Terraform fetches the secret values at apply time below.
# ==============================================================================

# Reads the latest version of the Google OAuth 2.0 Web Client ID from Secret Manager
# when `var.enable_google_oauth = true`.
data "google_secret_manager_secret_version" "oauth_client_id" {
  count   = var.enable_google_oauth ? 1 : 0
  project = var.project_id
  secret  = var.google_oauth_client_id_secret_name

  depends_on = [
    google_project_service.required_apis["secretmanager.googleapis.com"],
  ]
}

# Reads the latest version of the Google OAuth 2.0 Web Client Secret from Secret Manager
# when `var.enable_google_oauth = true`.
data "google_secret_manager_secret_version" "oauth_client_secret" {
  count   = var.enable_google_oauth ? 1 : 0
  project = var.project_id
  secret  = var.google_oauth_client_secret_name

  depends_on = [
    google_project_service.required_apis["secretmanager.googleapis.com"],
  ]
}

# Registers Google (`google.com`) as an enabled Federated Identity Provider in Identity Platform.
resource "google_identity_platform_default_supported_idp_config" "google_sign_in" {
  count         = var.enable_google_oauth ? 1 : 0
  provider      = google-beta
  project       = var.project_id
  enabled       = true
  idp_id        = "google.com"
  client_id     = trimspace(data.google_secret_manager_secret_version.oauth_client_id[0].secret_data)
  client_secret = trimspace(data.google_secret_manager_secret_version.oauth_client_secret[0].secret_data)

  depends_on = [
    google_identity_platform_config.auth,
  ]
}
