# ==============================================================================
# AI-Specific Security — Cloud DLP Templates & Google Cloud Model Armor
#
# How the pieces connect:
#   1. `ai_output_inspect` (Cloud DLP)    -> Defines WHICH sensitive infoTypes to detect
#   2. `ai_output_deidentify` (Cloud DLP) -> Defines HOW to mask findings (`[INFO_TYPE]`)
#   3. `modelarmor_sa` (Service Identity) -> Grants Model Armor permission to call Cloud DLP
#   4. `rag_guard` (Model Armor Template) -> Combines Prompt Injection, Jailbreak, Malicious URI,
#                                            Responsible AI, and Advanced Cloud DLP redaction
#                                            into a single regional screening endpoint.
# ==============================================================================

# ==============================================================================
# 1. Model Armor Service Agent & Cloud DLP Delegation Permissions
# ==============================================================================

# Triggers creation of Google's managed service agent for `modelarmor.googleapis.com`
# (`service-<project_number>@gcp-sa-modelarmor.iam.gserviceaccount.com`).
# When Model Armor executes Advanced SDP (`sdp_settings.advanced_config`), it calls Cloud DLP
# under this service agent identity rather than the caller's service account.
resource "google_project_service_identity" "modelarmor_sa" {
  provider = google-beta
  project  = var.project_id
  service  = "modelarmor.googleapis.com"

  depends_on = [
    google_project_service.required_apis["modelarmor.googleapis.com"],
  ]
}

# Allows the Model Armor service agent to execute Cloud DLP inspection and de-identification jobs.
resource "google_project_iam_member" "modelarmor_sa_dlp_user" {
  project = var.project_id
  role    = "roles/dlp.user"
  member  = "serviceAccount:${google_project_service_identity.modelarmor_sa.email}"
}

# Allows the Model Armor service agent to read the Cloud DLP template definitions below.
resource "google_project_iam_member" "modelarmor_sa_dlp_reader" {
  project = var.project_id
  role    = "roles/dlp.reader"
  member  = "serviceAccount:${google_project_service_identity.modelarmor_sa.email}"
}

# ==============================================================================
# 2. Sensitive Data Protection (Cloud DLP) Inspect Template
# ==============================================================================

# Regional Cloud DLP Inspect Template (`locations/${var.region}`) scanning retrieved document
# chunks and generated LLM answers for personal data (PII), financial identifiers, and secrets.
# `min_likelihood = "POSSIBLE"` favors privacy safety so borderline PII matches are still masked.
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

# ==============================================================================
# 3. Sensitive Data Protection (Cloud DLP) De-identify Template
# ==============================================================================

# Regional Cloud DLP De-identify Template replacing every matched span from `ai_output_inspect`
# with its bracketed infoType label (e.g., `alice@example.com` -> `[EMAIL_ADDRESS]`), preserving
# sentence readability and `[source, p.X]` citations while stripping raw sensitive values.
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

# ==============================================================================
# 4. Regional Google Cloud Model Armor Template (`rag_guard`)
# ==============================================================================

# Unified regional AI guardrail template invoked by `src/core/ai_security/armor.py`:
# - `pi_and_jailbreak_filter_settings`: Blocks prompt injection and jailbreak attempts on input.
# - `sdp_settings.advanced_config`: Masks PII and secrets in retrieved chunks and model outputs
#   using the Cloud DLP inspect + de-identify templates above.
# - `malicious_uri_filter_settings`: Blocks phishing and malware URLs in prompts and responses.
# - `rai_settings`: Enforces Responsible AI harm thresholds across Hate Speech, Harassment,
#   Sexually Explicit, and Dangerous content.
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

  # Data Residency Precondition:
  # Ensures the Model Armor template is always provisioned in a concrete single GCP region.
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
