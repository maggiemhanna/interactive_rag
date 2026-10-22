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

  authorized_domains = distinct([
    "localhost",
    "127.0.0.1",
    "${var.project_id}.firebaseapp.com",
    "${var.project_id}.web.app",
    local.frontend_deterministic_domain,
  ])

  depends_on = [
    google_project_service.required_apis["identitytoolkit.googleapis.com"],
  ]
}

resource "google_identity_platform_default_supported_idp_config" "google_sign_in" {
  count         = var.enable_google_oauth ? 1 : 0
  provider      = google-beta
  project       = var.project_id
  enabled       = true
  idp_id        = "google.com"
  client_id     = var.google_oauth_client_id
  client_secret = var.google_oauth_client_secret

  depends_on = [
    google_identity_platform_config.auth,
  ]
}
