# ==============================================================================
# Availability Design — Cloud Monitoring Uptime Checks, Custom Services,
# Declarative SLOs & Error-Budget Burn-Rate Alert Policies
#
# Reliability architecture:
#   1. Multi-Region Synthetic Uptime Checks (`google_monitoring_uptime_check_config`):
#      Continuously probe the Global External HTTPS Load Balancer across all three
#      service entry points (`/healthz`, `/api/ingest/health`, `/api/agent/health`)
#      over TLS with certificate validation and response-body content matchers.
#   2. Cloud Monitoring Custom Services (`google_monitoring_custom_service`):
#      Declare explicit Service Monitoring identities for `agent-api`, `ingest-api`,
#      and `frontend` to anchor SLOs and error budgets.
#   3. Declarative Cloud Monitoring SLOs (`google_monitoring_slo`):
#      Enforce 28-day rolling availability SLOs (HTTP non-5xx `good_total_ratio`)
#      across all three services plus a request-based latency SLO (`distribution_cut`
#      over `agent_turn_latency_ms`) for interactive Agentic RAG turns.
#   4. Availability & Error-Budget Burn-Rate Alerts (`google_monitoring_alert_policy`):
#      Page operators on multi-region synthetic probe failures (`REDUCE_COUNT_FALSE > 1`)
#      and fast error-budget burn (`select_slo_burn_rate` over 60m > 14.4x).
# ==============================================================================

# ==============================================================================
# 1. Cloud Monitoring Uptime Checks (HTTPS Probes via External Load Balancer)
# ==============================================================================

# 1. Frontend Nginx `/healthz` Uptime Check
resource "google_monitoring_uptime_check_config" "frontend_health" {
  project      = var.project_id
  display_name = "InteractiveRAG — Frontend /healthz Uptime Check (${var.environment})"
  timeout      = "${var.uptime_check_timeout_seconds}s"
  period       = "${var.uptime_check_period_seconds}s"

  http_check {
    path           = "/healthz"
    port           = 443
    use_ssl        = true
    validate_ssl   = true
    request_method = "GET"

    accepted_response_status_codes {
      status_class = "STATUS_CLASS_2XX"
    }
  }

  monitored_resource {
    type = "uptime_url"
    labels = {
      project_id = var.project_id
      host       = local.lb_domain
    }
  }

  content_matchers {
    content = "ok"
    matcher = "CONTAINS_STRING"
  }

  depends_on = [
    google_project_service.required_apis["monitoring.googleapis.com"],
    google_compute_global_forwarding_rule.https,
    google_cloud_run_v2_service.frontend,
  ]
}

# 2. Document Ingestion API (`/api/ingest/health` -> `ingest-api` `/health`) Uptime Check
resource "google_monitoring_uptime_check_config" "ingest_api_health" {
  project      = var.project_id
  display_name = "InteractiveRAG — Ingest API /health Uptime Check (${var.environment})"
  timeout      = "${var.uptime_check_timeout_seconds}s"
  period       = "${var.uptime_check_period_seconds}s"

  http_check {
    path           = "/api/ingest/health"
    port           = 443
    use_ssl        = true
    validate_ssl   = true
    request_method = "GET"

    accepted_response_status_codes {
      status_class = "STATUS_CLASS_2XX"
    }
  }

  monitored_resource {
    type = "uptime_url"
    labels = {
      project_id = var.project_id
      host       = local.lb_domain
    }
  }

  content_matchers {
    content = "ingest-api"
    matcher = "CONTAINS_STRING"
  }

  depends_on = [
    google_project_service.required_apis["monitoring.googleapis.com"],
    google_compute_global_forwarding_rule.https,
    google_cloud_run_v2_service.ingest_api,
  ]
}

# 3. Interactive Agent, Search & Management API (`/api/agent/health` -> `agent-api` `/health`) Uptime Check
resource "google_monitoring_uptime_check_config" "agent_api_health" {
  project      = var.project_id
  display_name = "InteractiveRAG — Agent API /health Uptime Check (${var.environment})"
  timeout      = "${var.uptime_check_timeout_seconds}s"
  period       = "${var.uptime_check_period_seconds}s"

  http_check {
    path           = "/api/agent/health"
    port           = 443
    use_ssl        = true
    validate_ssl   = true
    request_method = "GET"

    accepted_response_status_codes {
      status_class = "STATUS_CLASS_2XX"
    }
  }

  monitored_resource {
    type = "uptime_url"
    labels = {
      project_id = var.project_id
      host       = local.lb_domain
    }
  }

  content_matchers {
    content = "agent-api"
    matcher = "CONTAINS_STRING"
  }

  depends_on = [
    google_project_service.required_apis["monitoring.googleapis.com"],
    google_compute_global_forwarding_rule.https,
    google_cloud_run_v2_service.agent_api,
  ]
}

# ==============================================================================
# 2. Cloud Monitoring Custom Services (SLO Anchors)
# ==============================================================================

resource "google_monitoring_custom_service" "agent_api" {
  project      = var.project_id
  service_id   = "${var.app_name}-agent-api-${var.environment}"
  display_name = "InteractiveRAG — Agent API (${var.environment})"

  user_labels = {
    environment = var.environment
    service     = "agent-api"
    managed_by  = "terraform"
  }

  depends_on = [
    google_project_service.required_apis["monitoring.googleapis.com"],
    google_cloud_run_v2_service.agent_api,
  ]
}

resource "google_monitoring_custom_service" "ingest_api" {
  project      = var.project_id
  service_id   = "${var.app_name}-ingest-api-${var.environment}"
  display_name = "InteractiveRAG — Ingest API (${var.environment})"

  user_labels = {
    environment = var.environment
    service     = "ingest-api"
    managed_by  = "terraform"
  }

  depends_on = [
    google_project_service.required_apis["monitoring.googleapis.com"],
    google_cloud_run_v2_service.ingest_api,
  ]
}

resource "google_monitoring_custom_service" "frontend" {
  project      = var.project_id
  service_id   = "${var.app_name}-frontend-${var.environment}"
  display_name = "InteractiveRAG — Frontend SPA (${var.environment})"

  user_labels = {
    environment = var.environment
    service     = "frontend"
    managed_by  = "terraform"
  }

  depends_on = [
    google_project_service.required_apis["monitoring.googleapis.com"],
    google_cloud_run_v2_service.frontend,
  ]
}

# ==============================================================================
# 3. Declarative Cloud Monitoring Service Level Objectives (SLOs)
# ==============================================================================

# 1. Agent API Request Availability SLO (Non-5xx HTTP Request Ratio)
resource "google_monitoring_slo" "agent_api_availability_slo" {
  project             = var.project_id
  service             = google_monitoring_custom_service.agent_api.service_id
  slo_id              = "agent-api-availability-${var.environment}"
  display_name        = "Agent API Availability (${var.slo_agent_availability_goal * 100}% non-5xx over ${var.slo_rolling_period_days}d)"
  goal                = var.slo_agent_availability_goal
  rolling_period_days = var.slo_rolling_period_days

  request_based_sli {
    good_total_ratio {
      bad_service_filter   = "metric.type=\"run.googleapis.com/request_count\" AND resource.type=\"cloud_run_revision\" AND resource.label.service_name=\"${local.agent_service_name}\" AND metric.label.response_code_class=\"5xx\""
      total_service_filter = "metric.type=\"run.googleapis.com/request_count\" AND resource.type=\"cloud_run_revision\" AND resource.label.service_name=\"${local.agent_service_name}\""
    }
  }
}

# 2. Agent API Turn Latency SLO (Distribution Cut on `agent_turn_latency_ms`)
resource "google_monitoring_slo" "agent_api_latency_slo" {
  project             = var.project_id
  service             = google_monitoring_custom_service.agent_api.service_id
  slo_id              = "agent-api-latency-${var.environment}"
  display_name        = "Agent API Turn Latency (${var.slo_agent_latency_goal * 100}% <= ${var.slo_agent_latency_threshold_ms}ms over ${var.slo_rolling_period_days}d)"
  goal                = var.slo_agent_latency_goal
  rolling_period_days = var.slo_rolling_period_days

  request_based_sli {
    distribution_cut {
      distribution_filter = "metric.type=\"logging.googleapis.com/user/${var.app_name}/agent_turn_latency_ms\" AND resource.type=\"cloud_run_revision\""
      range {
        max = var.slo_agent_latency_threshold_ms
      }
    }
  }

  depends_on = [
    google_logging_metric.agent_turn_latency_ms,
  ]
}

# 3. Ingest API Request Availability SLO (Non-5xx HTTP Request Ratio)
resource "google_monitoring_slo" "ingest_api_availability_slo" {
  project             = var.project_id
  service             = google_monitoring_custom_service.ingest_api.service_id
  slo_id              = "ingest-api-availability-${var.environment}"
  display_name        = "Ingest API Availability (${var.slo_ingest_availability_goal * 100}% non-5xx over ${var.slo_rolling_period_days}d)"
  goal                = var.slo_ingest_availability_goal
  rolling_period_days = var.slo_rolling_period_days

  request_based_sli {
    good_total_ratio {
      bad_service_filter   = "metric.type=\"run.googleapis.com/request_count\" AND resource.type=\"cloud_run_revision\" AND resource.label.service_name=\"${local.ingest_service_name}\" AND metric.label.response_code_class=\"5xx\""
      total_service_filter = "metric.type=\"run.googleapis.com/request_count\" AND resource.type=\"cloud_run_revision\" AND resource.label.service_name=\"${local.ingest_service_name}\""
    }
  }
}

# 4. Frontend SPA Request Availability SLO (Non-5xx HTTP Request Ratio)
resource "google_monitoring_slo" "frontend_availability_slo" {
  project             = var.project_id
  service             = google_monitoring_custom_service.frontend.service_id
  slo_id              = "frontend-availability-${var.environment}"
  display_name        = "Frontend SPA Availability (${var.slo_frontend_availability_goal * 100}% non-5xx over ${var.slo_rolling_period_days}d)"
  goal                = var.slo_frontend_availability_goal
  rolling_period_days = var.slo_rolling_period_days

  request_based_sli {
    good_total_ratio {
      bad_service_filter   = "metric.type=\"run.googleapis.com/request_count\" AND resource.type=\"cloud_run_revision\" AND resource.label.service_name=\"${local.frontend_service_name}\" AND metric.label.response_code_class=\"5xx\""
      total_service_filter = "metric.type=\"run.googleapis.com/request_count\" AND resource.type=\"cloud_run_revision\" AND resource.label.service_name=\"${local.frontend_service_name}\""
    }
  }
}

# ==============================================================================
# 4. Availability Alert Policies (Multi-Region Uptime Failures & Fast SLO Burn)
# ==============================================================================

# 1. Multi-Region Uptime Check Failure Alert (fires if >= 2 probe locations fail for 120s)
resource "google_monitoring_alert_policy" "uptime_check_failure_alert" {
  project      = var.project_id
  display_name = "InteractiveRAG — Synthetic Uptime Check Failure (${var.environment})"
  combiner     = "OR"

  conditions {
    display_name = "Frontend /healthz failing across >= 2 probe regions"
    condition_threshold {
      filter          = "metric.type=\"monitoring.googleapis.com/uptime_check/check_passed\" AND resource.type=\"uptime_url\" AND metric.label.check_id=\"${google_monitoring_uptime_check_config.frontend_health.uptime_check_id}\""
      duration        = "120s"
      comparison      = "COMPARISON_GT"
      threshold_value = 1

      aggregations {
        alignment_period     = "60s"
        per_series_aligner   = "ALIGN_NEXT_OLDER"
        cross_series_reducer = "REDUCE_COUNT_FALSE"
        group_by_fields      = ["resource.label.host"]
      }
    }
  }

  conditions {
    display_name = "Ingest API /api/ingest/health failing across >= 2 probe regions"
    condition_threshold {
      filter          = "metric.type=\"monitoring.googleapis.com/uptime_check/check_passed\" AND resource.type=\"uptime_url\" AND metric.label.check_id=\"${google_monitoring_uptime_check_config.ingest_api_health.uptime_check_id}\""
      duration        = "120s"
      comparison      = "COMPARISON_GT"
      threshold_value = 1

      aggregations {
        alignment_period     = "60s"
        per_series_aligner   = "ALIGN_NEXT_OLDER"
        cross_series_reducer = "REDUCE_COUNT_FALSE"
        group_by_fields      = ["resource.label.host"]
      }
    }
  }

  conditions {
    display_name = "Agent API /api/agent/health failing across >= 2 probe regions"
    condition_threshold {
      filter          = "metric.type=\"monitoring.googleapis.com/uptime_check/check_passed\" AND resource.type=\"uptime_url\" AND metric.label.check_id=\"${google_monitoring_uptime_check_config.agent_api_health.uptime_check_id}\""
      duration        = "120s"
      comparison      = "COMPARISON_GT"
      threshold_value = 1

      aggregations {
        alignment_period     = "60s"
        per_series_aligner   = "ALIGN_NEXT_OLDER"
        cross_series_reducer = "REDUCE_COUNT_FALSE"
        group_by_fields      = ["resource.label.host"]
      }
    }
  }

  alert_strategy {
    auto_close = "1800s"
  }

  documentation {
    content   = "One or more InteractiveRAG HTTPS Load Balancer health endpoints (`/healthz`, `/api/ingest/health`, `/api/agent/health`) failed synthetic probes from multiple regions for over 2 minutes."
    mime_type = "text/markdown"
  }

  depends_on = [
    google_project_service.required_apis["monitoring.googleapis.com"],
  ]
}

# 2. SLO Fast Error-Budget Burn-Rate Alert (60m lookback > var.slo_fast_burn_rate_threshold, default 14.4x)
resource "google_monitoring_alert_policy" "slo_fast_burn_alert" {
  project      = var.project_id
  display_name = "InteractiveRAG — Availability SLO Fast Error-Budget Burn (${var.environment})"
  combiner     = "OR"

  conditions {
    display_name = "Agent API availability error-budget burn rate > ${var.slo_fast_burn_rate_threshold}x (60m)"
    condition_threshold {
      filter          = "select_slo_burn_rate(\"${google_monitoring_slo.agent_api_availability_slo.id}\", \"60m\")"
      duration        = "0s"
      comparison      = "COMPARISON_GT"
      threshold_value = var.slo_fast_burn_rate_threshold
    }
  }

  conditions {
    display_name = "Ingest API availability error-budget burn rate > ${var.slo_fast_burn_rate_threshold}x (60m)"
    condition_threshold {
      filter          = "select_slo_burn_rate(\"${google_monitoring_slo.ingest_api_availability_slo.id}\", \"60m\")"
      duration        = "0s"
      comparison      = "COMPARISON_GT"
      threshold_value = var.slo_fast_burn_rate_threshold
    }
  }

  conditions {
    display_name = "Frontend SPA availability error-budget burn rate > ${var.slo_fast_burn_rate_threshold}x (60m)"
    condition_threshold {
      filter          = "select_slo_burn_rate(\"${google_monitoring_slo.frontend_availability_slo.id}\", \"60m\")"
      duration        = "0s"
      comparison      = "COMPARISON_GT"
      threshold_value = var.slo_fast_burn_rate_threshold
    }
  }

  alert_strategy {
    auto_close = "1800s"
  }

  documentation {
    content   = "An InteractiveRAG availability SLO is burning its 28-day error budget faster than ${var.slo_fast_burn_rate_threshold}x over the past 60 minutes (at 14.4x, 2% of the monthly error budget is consumed in 1 hour)."
    mime_type = "text/markdown"
  }

  depends_on = [
    google_project_service.required_apis["monitoring.googleapis.com"],
  ]
}
