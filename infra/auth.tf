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

# Fetch OAuth 2.0 Web Client ID & Client Secret directly from GCP Secret Manager (version = "latest")
data "google_secret_manager_secret_version" "oauth_client_id" {
  count   = var.enable_google_oauth ? 1 : 0
  project = var.project_id
  secret  = var.google_oauth_client_id_secret_name

  depends_on = [
    google_project_service.required_apis["secretmanager.googleapis.com"],
  ]
}

data "google_secret_manager_secret_version" "oauth_client_secret" {
  count   = var.enable_google_oauth ? 1 : 0
  project = var.project_id
  secret  = var.google_oauth_client_secret_name

  depends_on = [
    google_project_service.required_apis["secretmanager.googleapis.com"],
  ]
}

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
