# 1. Provision the Google-managed Model Armor Service Agent and authorize it to read & run Cloud DLP templates
resource "google_project_service_identity" "modelarmor_sa" {
  provider = google-beta
  project  = var.project_id
  service  = "modelarmor.googleapis.com"

  depends_on = [
    google_project_service.required_apis["modelarmor.googleapis.com"],
  ]
}

resource "google_project_iam_member" "modelarmor_sa_dlp_user" {
  project = var.project_id
  role    = "roles/dlp.user"
  member  = "serviceAccount:${google_project_service_identity.modelarmor_sa.email}"
}

resource "google_project_iam_member" "modelarmor_sa_dlp_reader" {
  project = var.project_id
  role    = "roles/dlp.reader"
  member  = "serviceAccount:${google_project_service_identity.modelarmor_sa.email}"
}

# 2. Sensitive Data Protection (Cloud DLP) Inspect Template — detects PII & secret credentials
resource "google_data_loss_prevention_inspect_template" "ai_output_inspect" {
  parent       = "projects/${var.project_id}/locations/${var.region}"
  template_id  = "${var.app_name}-inspect-${var.environment}"
  display_name = "InteractiveRAG AI Output PII & Credential Inspector (${var.environment})"
  description  = "Inspects LLM outputs for PII, financial identifiers, and credentials."

  inspect_config {
    min_likelihood = "POSSIBLE"

    info_types {
      name = "EMAIL_ADDRESS"
    }
    info_types {
      name = "PHONE_NUMBER"
    }
    info_types {
      name = "CREDIT_CARD_NUMBER"
    }
    info_types {
      name = "US_SOCIAL_SECURITY_NUMBER"
    }
    info_types {
      name = "IBAN_CODE"
    }
    info_types {
      name = "GCP_API_KEY"
    }
    info_types {
      name = "AUTH_TOKEN"
    }
  }

  depends_on = [
    google_project_service.required_apis["dlp.googleapis.com"],
  ]
}

# 3. Sensitive Data Protection (Cloud DLP) De-identify Template — masks findings with [INFO_TYPE] placeholders
resource "google_data_loss_prevention_deidentify_template" "ai_output_deidentify" {
  parent       = "projects/${var.project_id}/locations/${var.region}"
  template_id  = "${var.app_name}-deidentify-${var.environment}"
  display_name = "InteractiveRAG AI Output Redactor (${var.environment})"
  description  = "Replaces detected PII and credentials with their [INFO_TYPE] label."

  deidentify_config {
    info_type_transformations {
      transformations {
        primitive_transformation {
          replace_with_info_type_config = true
        }
      }
    }
  }

  depends_on = [
    google_project_service.required_apis["dlp.googleapis.com"],
  ]
}

# 4. Google Cloud Model Armor Template — screens prompts (Prompt Injection/Jailbreak) & responses (SDP + RAI)
resource "google_model_armor_template" "rag_guard" {
  project     = var.project_id
  location    = var.region
  template_id = "${var.app_name}-guard-${var.environment}"

  filter_config {
    pi_and_jailbreak_filter_settings {
      filter_enforcement = "ENABLED"
      confidence_level   = var.model_armor_pi_confidence
    }

    sdp_settings {
      advanced_config {
        inspect_template    = google_data_loss_prevention_inspect_template.ai_output_inspect.id
        deidentify_template = google_data_loss_prevention_deidentify_template.ai_output_deidentify.id
      }
    }

    malicious_uri_filter_settings {
      filter_enforcement = "ENABLED"
    }

    rai_settings {
      rai_filters {
        filter_type      = "HATE_SPEECH"
        confidence_level = var.model_armor_rai_confidence
      }
      rai_filters {
        filter_type      = "HARASSMENT"
        confidence_level = var.model_armor_rai_confidence
      }
      rai_filters {
        filter_type      = "SEXUALLY_EXPLICIT"
        confidence_level = var.model_armor_rai_confidence
      }
      rai_filters {
        filter_type      = "DANGEROUS"
        confidence_level = var.model_armor_rai_confidence
      }
    }
  }

  template_metadata {
    multi_language_detection {
      enable_multi_language_detection = true
    }
  }

  lifecycle {
    precondition {
      condition     = can(regex("^[a-z]+-[a-z]+[0-9]+$", var.region))
      error_message = "Model Armor template must reside in a concrete single GCP region."
    }
  }

  depends_on = [
    google_project_service.required_apis["modelarmor.googleapis.com"],
    google_project_iam_member.modelarmor_sa_dlp_user,
    google_project_iam_member.modelarmor_sa_dlp_reader,
  ]
}
