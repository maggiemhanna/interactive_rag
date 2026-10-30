# ==============================================================================
# Observability & AI Monitoring — Regional Telemetry Log Bucket, Sink,
# Log-Based Metrics & Cloud Monitoring Dashboard
#
# Observability architecture:
#   1. AI & Performance Telemetry Stream (`projects/<project>/logs/interactiverag-telemetry`):
#      Emits one structured summary record per completed request (`document.ingest`,
#      `vectorstore.query`, `agent.query`) containing total latency, streaming TTFT,
#      stage-by-stage latency breakdowns, hierarchical span counts, Gemini token usage
#      (`input_tokens`, `output_tokens`, `total_tokens`), ChromaDB similarity scores
#      (`top_similarity_score`, `mean_similarity_score`), and grounding outcomes.
#   2. Structured Cloud Run Operational Logs (`run.googleapis.com/stdout` & `stderr`):
#      Single-line JSON application logs enriched with `request_id`, `trace_id`, `span_id`,
#      `service`, `uid`, and `sourceLocation`.
#   3. Both streams remain searchable in `_Default` (Logs Explorer) AND are copied into the
#      regional `observability_telemetry` log bucket in `var.region`.
#   4. Log-based metrics extract latency distributions, TTFT, stage latencies, token
#      consumption, retrieval similarity scores, ungrounded response counts, and ingestion
#      throughput directly from `interactiverag-telemetry` into Cloud Monitoring with
#      zero extra network calls per request.
# ==============================================================================

locals {
  observability_bucket_id = "${var.app_name}-observability-${var.environment}"
}

# ==============================================================================
# 1. Dedicated Regional Cloud Logging Observability & Telemetry Bucket
# ==============================================================================

# Stores operational container logs and `interactiverag-telemetry` records strictly inside
# `var.region` (unlike `_Default`, which is global) with configurable retention
# (`var.observability_log_retention_days`, default 30 days) and Log Analytics enabled.
resource "google_logging_project_bucket_config" "observability_telemetry" {
  project          = var.project_id
  location         = var.region
  bucket_id        = local.observability_bucket_id
  retention_days   = var.observability_log_retention_days
  enable_analytics = true
  description      = "Regional observability and AI telemetry log bucket for InteractiveRAG (${var.environment})."

  lifecycle {
    precondition {
      condition     = can(regex("^[a-z]+-[a-z]+[0-9]+$", var.region))
      error_message = "Observability log bucket must reside in a concrete single GCP region (never 'global' or a multi-region)."
    }
  }

  depends_on = [
    google_project_service.required_apis["logging.googleapis.com"],
  ]
}

# ==============================================================================
# 2. Project-Level Observability & Telemetry Log Sink
# ==============================================================================

# Non-excluding log sink that copies:
#   - Structured AI & performance telemetry events (`interactiverag-telemetry`)
#   - Structured Cloud Run container logs (`run.googleapis.com/stdout` & `stderr`)
# into `observability_telemetry` while leaving `_Default` intact for immediate Logs Explorer queries.
resource "google_logging_project_sink" "observability_sink" {
  project     = var.project_id
  name        = "${var.app_name}-observability-sink-${var.environment}"
  description = "Copies InteractiveRAG AI telemetry events and structured Cloud Run container logs into the regional observability bucket."
  destination = "logging.googleapis.com/projects/${var.project_id}/locations/${var.region}/buckets/${google_logging_project_bucket_config.observability_telemetry.bucket_id}"
  filter      = "logName=\"projects/${var.project_id}/logs/interactiverag-telemetry\" OR (resource.type=\"cloud_run_revision\" AND (logName=\"projects/${var.project_id}/logs/run.googleapis.com%2Fstdout\" OR logName=\"projects/${var.project_id}/logs/run.googleapis.com%2Fstderr\"))"

  depends_on = [
    google_logging_project_bucket_config.observability_telemetry,
  ]
}

# ==============================================================================
# 3. Cloud Monitoring Log-Based Metrics over `interactiverag-telemetry`
# ==============================================================================

# 1. Agent Turn Latency Distribution (`total_latency_ms` on `agent.query` events)
resource "google_logging_metric" "agent_turn_latency_ms" {
  project         = var.project_id
  name            = "${var.app_name}/agent_turn_latency_ms"
  description     = "End-to-end latency in milliseconds for Agentic RAG turns (interactiverag-telemetry)."
  filter          = "logName=\"projects/${var.project_id}/logs/interactiverag-telemetry\" AND jsonPayload.event_type=\"agent.query\" AND jsonPayload.total_latency_ms > 0"
  value_extractor = "EXTRACT(jsonPayload.total_latency_ms)"

  metric_descriptor {
    metric_kind  = "DELTA"
    value_type   = "DISTRIBUTION"
    unit         = "ms"
    display_name = "InteractiveRAG Agent Turn Latency (ms)"

    labels {
      key         = "model_name"
      value_type  = "STRING"
      description = "Vertex AI Gemini model name"
    }
    labels {
      key         = "status"
      value_type  = "STRING"
      description = "Request completion status"
    }
  }

  label_extractors = {
    "model_name" = "EXTRACT(jsonPayload.model_name)"
    "status"     = "EXTRACT(jsonPayload.status)"
  }

  bucket_options {
    exponential_buckets {
      num_finite_buckets = 64
      growth_factor      = 1.4
      scale              = 10.0
    }
  }

  depends_on = [
    google_project_service.required_apis["logging.googleapis.com"],
    google_project_service.required_apis["monitoring.googleapis.com"],
  ]
}

# 2. Streaming Time-to-First-Token Distribution (`ttft_ms` on `/agent/stream` events)
resource "google_logging_metric" "agent_ttft_ms" {
  project         = var.project_id
  name            = "${var.app_name}/agent_ttft_ms"
  description     = "Streaming Time-to-First-Token (TTFT) in milliseconds for SSE Agentic RAG turns (interactiverag-telemetry)."
  filter          = "logName=\"projects/${var.project_id}/logs/interactiverag-telemetry\" AND jsonPayload.event_type=\"agent.query\" AND jsonPayload.ttft_ms > 0"
  value_extractor = "EXTRACT(jsonPayload.ttft_ms)"

  metric_descriptor {
    metric_kind  = "DELTA"
    value_type   = "DISTRIBUTION"
    unit         = "ms"
    display_name = "InteractiveRAG Streaming TTFT (ms)"

    labels {
      key         = "model_name"
      value_type  = "STRING"
      description = "Vertex AI Gemini model name"
    }
  }

  label_extractors = {
    "model_name" = "EXTRACT(jsonPayload.model_name)"
  }

  bucket_options {
    exponential_buckets {
      num_finite_buckets = 64
      growth_factor      = 1.4
      scale              = 10.0
    }
  }

  depends_on = [
    google_project_service.required_apis["logging.googleapis.com"],
    google_project_service.required_apis["monitoring.googleapis.com"],
  ]
}

# 3. Gemini LLM Stage Latency Distribution (`stage_latency_ms.llm_ms` on `agent.query` events)
resource "google_logging_metric" "agent_llm_stage_latency_ms" {
  project         = var.project_id
  name            = "${var.app_name}/agent_llm_stage_latency_ms"
  description     = "Cumulative millisecond latency spent in Vertex AI Gemini model_call stages per turn."
  filter          = "logName=\"projects/${var.project_id}/logs/interactiverag-telemetry\" AND jsonPayload.event_type=\"agent.query\" AND jsonPayload.stage_latency_ms.llm_ms > 0"
  value_extractor = "EXTRACT(jsonPayload.stage_latency_ms.llm_ms)"

  metric_descriptor {
    metric_kind  = "DELTA"
    value_type   = "DISTRIBUTION"
    unit         = "ms"
    display_name = "InteractiveRAG Gemini LLM Stage Latency (ms)"

    labels {
      key         = "model_name"
      value_type  = "STRING"
      description = "Vertex AI Gemini model name"
    }
  }

  label_extractors = {
    "model_name" = "EXTRACT(jsonPayload.model_name)"
  }

  bucket_options {
    exponential_buckets {
      num_finite_buckets = 64
      growth_factor      = 1.4
      scale              = 10.0
    }
  }

  depends_on = [
    google_project_service.required_apis["logging.googleapis.com"],
    google_project_service.required_apis["monitoring.googleapis.com"],
  ]
}

# 4. Vector Retrieval Stage Latency Distribution (`stage_latency_ms.retrieval_ms` across search & agent turns)
resource "google_logging_metric" "agent_retrieval_stage_latency_ms" {
  project         = var.project_id
  name            = "${var.app_name}/agent_retrieval_stage_latency_ms"
  description     = "Cumulative millisecond latency spent in ChromaDB vector retrieval stages per request."
  filter          = "logName=\"projects/${var.project_id}/logs/interactiverag-telemetry\" AND (jsonPayload.event_type=\"agent.query\" OR jsonPayload.event_type=\"vectorstore.query\") AND jsonPayload.stage_latency_ms.retrieval_ms > 0"
  value_extractor = "EXTRACT(jsonPayload.stage_latency_ms.retrieval_ms)"

  metric_descriptor {
    metric_kind  = "DELTA"
    value_type   = "DISTRIBUTION"
    unit         = "ms"
    display_name = "InteractiveRAG Vector Retrieval Stage Latency (ms)"

    labels {
      key         = "event_type"
      value_type  = "STRING"
      description = "Telemetry event type (agent.query or vectorstore.query)"
    }
  }

  label_extractors = {
    "event_type" = "EXTRACT(jsonPayload.event_type)"
  }

  bucket_options {
    exponential_buckets {
      num_finite_buckets = 64
      growth_factor      = 1.4
      scale              = 5.0
    }
  }

  depends_on = [
    google_project_service.required_apis["logging.googleapis.com"],
    google_project_service.required_apis["monitoring.googleapis.com"],
  ]
}

# 5. Gemini Input (Prompt) Token Distribution (`input_tokens` on `agent.query` events)
resource "google_logging_metric" "agent_input_tokens" {
  project         = var.project_id
  name            = "${var.app_name}/agent_input_tokens"
  description     = "Prompt / input Gemini tokens consumed per Agentic RAG turn (interactiverag-telemetry)."
  filter          = "logName=\"projects/${var.project_id}/logs/interactiverag-telemetry\" AND jsonPayload.event_type=\"agent.query\" AND jsonPayload.input_tokens > 0"
  value_extractor = "EXTRACT(jsonPayload.input_tokens)"

  metric_descriptor {
    metric_kind  = "DELTA"
    value_type   = "DISTRIBUTION"
    unit         = "1"
    display_name = "InteractiveRAG Agent Input Tokens per Turn"

    labels {
      key         = "model_name"
      value_type  = "STRING"
      description = "Vertex AI Gemini model name"
    }
  }

  label_extractors = {
    "model_name" = "EXTRACT(jsonPayload.model_name)"
  }

  bucket_options {
    exponential_buckets {
      num_finite_buckets = 64
      growth_factor      = 1.4
      scale              = 10.0
    }
  }

  depends_on = [
    google_project_service.required_apis["logging.googleapis.com"],
    google_project_service.required_apis["monitoring.googleapis.com"],
  ]
}

# 6. Gemini Output (Completion) Token Distribution (`output_tokens` on `agent.query` events)
resource "google_logging_metric" "agent_output_tokens" {
  project         = var.project_id
  name            = "${var.app_name}/agent_output_tokens"
  description     = "Completion / output Gemini tokens generated per Agentic RAG turn (interactiverag-telemetry)."
  filter          = "logName=\"projects/${var.project_id}/logs/interactiverag-telemetry\" AND jsonPayload.event_type=\"agent.query\" AND jsonPayload.output_tokens > 0"
  value_extractor = "EXTRACT(jsonPayload.output_tokens)"

  metric_descriptor {
    metric_kind  = "DELTA"
    value_type   = "DISTRIBUTION"
    unit         = "1"
    display_name = "InteractiveRAG Agent Output Tokens per Turn"

    labels {
      key         = "model_name"
      value_type  = "STRING"
      description = "Vertex AI Gemini model name"
    }
  }

  label_extractors = {
    "model_name" = "EXTRACT(jsonPayload.model_name)"
  }

  bucket_options {
    exponential_buckets {
      num_finite_buckets = 64
      growth_factor      = 1.4
      scale              = 10.0
    }
  }

  depends_on = [
    google_project_service.required_apis["logging.googleapis.com"],
    google_project_service.required_apis["monitoring.googleapis.com"],
  ]
}

# 7. Gemini Total Token Consumption Distribution (`total_tokens` on `agent.query` events)
resource "google_logging_metric" "agent_total_tokens" {
  project         = var.project_id
  name            = "${var.app_name}/agent_total_tokens"
  description     = "Total Gemini tokens (prompt + output) consumed per Agentic RAG turn (interactiverag-telemetry)."
  filter          = "logName=\"projects/${var.project_id}/logs/interactiverag-telemetry\" AND jsonPayload.event_type=\"agent.query\" AND jsonPayload.total_tokens > 0"
  value_extractor = "EXTRACT(jsonPayload.total_tokens)"

  metric_descriptor {
    metric_kind  = "DELTA"
    value_type   = "DISTRIBUTION"
    unit         = "1"
    display_name = "InteractiveRAG Agent Total Tokens per Turn"

    labels {
      key         = "model_name"
      value_type  = "STRING"
      description = "Vertex AI Gemini model name"
    }
  }

  label_extractors = {
    "model_name" = "EXTRACT(jsonPayload.model_name)"
  }

  bucket_options {
    exponential_buckets {
      num_finite_buckets = 64
      growth_factor      = 1.4
      scale              = 10.0
    }
  }

  depends_on = [
    google_project_service.required_apis["logging.googleapis.com"],
    google_project_service.required_apis["monitoring.googleapis.com"],
  ]
}

# 8. Retrieval Similarity Score Distribution (`mean_similarity_score` across search & agent turns)
resource "google_logging_metric" "retrieval_mean_similarity_score" {
  project         = var.project_id
  name            = "${var.app_name}/retrieval_mean_similarity_score"
  description     = "Normalized mean ChromaDB vector similarity score (0.0 to 1.0) of retrieved passages."
  filter          = "logName=\"projects/${var.project_id}/logs/interactiverag-telemetry\" AND (jsonPayload.event_type=\"agent.query\" OR jsonPayload.event_type=\"vectorstore.query\") AND jsonPayload.mean_similarity_score > 0"
  value_extractor = "EXTRACT(jsonPayload.mean_similarity_score)"

  metric_descriptor {
    metric_kind  = "DELTA"
    value_type   = "DISTRIBUTION"
    unit         = "1"
    display_name = "InteractiveRAG Retrieval Mean Similarity Score"

    labels {
      key         = "event_type"
      value_type  = "STRING"
      description = "Telemetry event type (agent.query or vectorstore.query)"
    }
  }

  label_extractors = {
    "event_type" = "EXTRACT(jsonPayload.event_type)"
  }

  bucket_options {
    linear_buckets {
      num_finite_buckets = 20
      width              = 0.05
      offset             = 0.0
    }
  }

  depends_on = [
    google_project_service.required_apis["logging.googleapis.com"],
    google_project_service.required_apis["monitoring.googleapis.com"],
  ]
}

# 9. Ungrounded Agent Answer Counter (`grounded = false` on `agent.query` events)
resource "google_logging_metric" "agent_ungrounded_answers" {
  project     = var.project_id
  name        = "${var.app_name}/agent_ungrounded_answers"
  description = "Count of Agentic RAG turns where the final answer was ungrounded (grounded=false)."
  filter      = "logName=\"projects/${var.project_id}/logs/interactiverag-telemetry\" AND jsonPayload.event_type=\"agent.query\" AND jsonPayload.grounded=false"

  metric_descriptor {
    metric_kind  = "DELTA"
    value_type   = "INT64"
    unit         = "1"
    display_name = "InteractiveRAG Ungrounded Agent Answers"

    labels {
      key         = "model_name"
      value_type  = "STRING"
      description = "Vertex AI Gemini model name"
    }
    labels {
      key         = "status"
      value_type  = "STRING"
      description = "Request completion status"
    }
  }

  label_extractors = {
    "model_name" = "EXTRACT(jsonPayload.model_name)"
    "status"     = "EXTRACT(jsonPayload.status)"
  }

  depends_on = [
    google_project_service.required_apis["logging.googleapis.com"],
    google_project_service.required_apis["monitoring.googleapis.com"],
  ]
}

# 10. Document Ingestion Chunks Indexed (`chunks_added` on `document.ingest` events)
resource "google_logging_metric" "ingestion_chunks_added" {
  project         = var.project_id
  name            = "${var.app_name}/ingestion_chunks_added"
  description     = "Number of PDF text chunks embedded and indexed per document.ingest request."
  filter          = "logName=\"projects/${var.project_id}/logs/interactiverag-telemetry\" AND jsonPayload.event_type=\"document.ingest\" AND jsonPayload.chunks_added > 0"
  value_extractor = "EXTRACT(jsonPayload.chunks_added)"

  metric_descriptor {
    metric_kind  = "DELTA"
    value_type   = "DISTRIBUTION"
    unit         = "1"
    display_name = "InteractiveRAG Ingested Chunks per Upload"

    labels {
      key         = "embedding_model"
      value_type  = "STRING"
      description = "Vertex AI embedding model name"
    }
  }

  label_extractors = {
    "embedding_model" = "EXTRACT(jsonPayload.embedding_model)"
  }

  bucket_options {
    exponential_buckets {
      num_finite_buckets = 32
      growth_factor      = 1.4
      scale              = 1.0
    }
  }

  depends_on = [
    google_project_service.required_apis["logging.googleapis.com"],
    google_project_service.required_apis["monitoring.googleapis.com"],
  ]
}

# ==============================================================================
# 4. Unified Cloud Monitoring Observability & AI Monitoring Dashboard
# ==============================================================================

resource "google_monitoring_dashboard" "observability_dashboard" {
  project = var.project_id

  dashboard_json = jsonencode({
    displayName = "InteractiveRAG — Observability & AI Monitoring (${var.environment})"
    gridLayout = {
      columns = "2"
      widgets = [
        {
          title = "1. Agent Turn Latency (p50 / p95 ms)"
          xyChart = {
            dataSets = [
              {
                plotType       = "LINE"
                legendTemplate = "p95 ({{metric.label.model_name}})"
                timeSeriesQuery = {
                  timeSeriesFilter = {
                    filter = "metric.type=\"logging.googleapis.com/user/${var.app_name}/agent_turn_latency_ms\""
                    aggregation = {
                      alignmentPeriod    = "60s"
                      perSeriesAligner   = "ALIGN_PERCENTILE_95"
                      crossSeriesReducer = "REDUCE_MEAN"
                      groupByFields      = ["metric.label.model_name"]
                    }
                  }
                }
              },
              {
                plotType       = "LINE"
                legendTemplate = "p50 ({{metric.label.model_name}})"
                timeSeriesQuery = {
                  timeSeriesFilter = {
                    filter = "metric.type=\"logging.googleapis.com/user/${var.app_name}/agent_turn_latency_ms\""
                    aggregation = {
                      alignmentPeriod    = "60s"
                      perSeriesAligner   = "ALIGN_PERCENTILE_50"
                      crossSeriesReducer = "REDUCE_MEAN"
                      groupByFields      = ["metric.label.model_name"]
                    }
                  }
                }
              }
            ]
          }
        },
        {
          title = "2. Streaming Time-to-First-Token — TTFT (p50 / p95 ms)"
          xyChart = {
            dataSets = [
              {
                plotType       = "LINE"
                legendTemplate = "TTFT p95 ({{metric.label.model_name}})"
                timeSeriesQuery = {
                  timeSeriesFilter = {
                    filter = "metric.type=\"logging.googleapis.com/user/${var.app_name}/agent_ttft_ms\""
                    aggregation = {
                      alignmentPeriod    = "60s"
                      perSeriesAligner   = "ALIGN_PERCENTILE_95"
                      crossSeriesReducer = "REDUCE_MEAN"
                      groupByFields      = ["metric.label.model_name"]
                    }
                  }
                }
              },
              {
                plotType       = "LINE"
                legendTemplate = "TTFT p50 ({{metric.label.model_name}})"
                timeSeriesQuery = {
                  timeSeriesFilter = {
                    filter = "metric.type=\"logging.googleapis.com/user/${var.app_name}/agent_ttft_ms\""
                    aggregation = {
                      alignmentPeriod    = "60s"
                      perSeriesAligner   = "ALIGN_PERCENTILE_50"
                      crossSeriesReducer = "REDUCE_MEAN"
                      groupByFields      = ["metric.label.model_name"]
                    }
                  }
                }
              }
            ]
          }
        },
        {
          title = "3. Pipeline Stage Latency Breakdown: Gemini LLM vs. Vector Retrieval (p95 ms)"
          xyChart = {
            dataSets = [
              {
                plotType       = "LINE"
                legendTemplate = "LLM Stage p95 ({{metric.label.model_name}})"
                timeSeriesQuery = {
                  timeSeriesFilter = {
                    filter = "metric.type=\"logging.googleapis.com/user/${var.app_name}/agent_llm_stage_latency_ms\""
                    aggregation = {
                      alignmentPeriod    = "60s"
                      perSeriesAligner   = "ALIGN_PERCENTILE_95"
                      crossSeriesReducer = "REDUCE_MEAN"
                      groupByFields      = ["metric.label.model_name"]
                    }
                  }
                }
              },
              {
                plotType       = "LINE"
                legendTemplate = "Retrieval Stage p95 ({{metric.label.event_type}})"
                timeSeriesQuery = {
                  timeSeriesFilter = {
                    filter = "metric.type=\"logging.googleapis.com/user/${var.app_name}/agent_retrieval_stage_latency_ms\""
                    aggregation = {
                      alignmentPeriod    = "60s"
                      perSeriesAligner   = "ALIGN_PERCENTILE_95"
                      crossSeriesReducer = "REDUCE_MEAN"
                      groupByFields      = ["metric.label.event_type"]
                    }
                  }
                }
              }
            ]
          }
        },
        {
          title = "4. Gemini Token Usage: Input vs. Output vs. Total Tokens by Model"
          xyChart = {
            dataSets = [
              {
                plotType       = "STACKED_BAR"
                legendTemplate = "Input Tokens ({{metric.label.model_name}})"
                timeSeriesQuery = {
                  timeSeriesFilter = {
                    filter = "metric.type=\"logging.googleapis.com/user/${var.app_name}/agent_input_tokens\""
                    aggregation = {
                      alignmentPeriod    = "60s"
                      perSeriesAligner   = "ALIGN_DELTA"
                      crossSeriesReducer = "REDUCE_MEAN"
                      groupByFields      = ["metric.label.model_name"]
                    }
                  }
                }
              },
              {
                plotType       = "STACKED_BAR"
                legendTemplate = "Output Tokens ({{metric.label.model_name}})"
                timeSeriesQuery = {
                  timeSeriesFilter = {
                    filter = "metric.type=\"logging.googleapis.com/user/${var.app_name}/agent_output_tokens\""
                    aggregation = {
                      alignmentPeriod    = "60s"
                      perSeriesAligner   = "ALIGN_DELTA"
                      crossSeriesReducer = "REDUCE_MEAN"
                      groupByFields      = ["metric.label.model_name"]
                    }
                  }
                }
              },
              {
                plotType       = "LINE"
                legendTemplate = "Total Tokens ({{metric.label.model_name}})"
                timeSeriesQuery = {
                  timeSeriesFilter = {
                    filter = "metric.type=\"logging.googleapis.com/user/${var.app_name}/agent_total_tokens\""
                    aggregation = {
                      alignmentPeriod    = "60s"
                      perSeriesAligner   = "ALIGN_DELTA"
                      crossSeriesReducer = "REDUCE_MEAN"
                      groupByFields      = ["metric.label.model_name"]
                    }
                  }
                }
              }
            ]
          }
        },
        {
          title = "5. Retrieval Quality: Mean Vector Similarity Score (p50 / Mean)"
          xyChart = {
            dataSets = [
              {
                plotType       = "LINE"
                legendTemplate = "Similarity p50 ({{metric.label.event_type}})"
                timeSeriesQuery = {
                  timeSeriesFilter = {
                    filter = "metric.type=\"logging.googleapis.com/user/${var.app_name}/retrieval_mean_similarity_score\""
                    aggregation = {
                      alignmentPeriod    = "60s"
                      perSeriesAligner   = "ALIGN_PERCENTILE_50"
                      crossSeriesReducer = "REDUCE_MEAN"
                      groupByFields      = ["metric.label.event_type"]
                    }
                  }
                }
              }
            ]
          }
        },
        {
          title = "6. Grounding & Safety: Ungrounded / Guardrail-Stopped Answers"
          xyChart = {
            dataSets = [
              {
                plotType       = "STACKED_BAR"
                legendTemplate = "{{metric.label.status}} ({{metric.label.model_name}})"
                timeSeriesQuery = {
                  timeSeriesFilter = {
                    filter = "metric.type=\"logging.googleapis.com/user/${var.app_name}/agent_ungrounded_answers\""
                    aggregation = {
                      alignmentPeriod    = "60s"
                      perSeriesAligner   = "ALIGN_DELTA"
                      crossSeriesReducer = "REDUCE_SUM"
                      groupByFields      = ["metric.label.model_name", "metric.label.status"]
                    }
                  }
                }
              }
            ]
          }
        },
        {
          title = "7. Document Ingestion Throughput (Chunks Indexed by Embedding Model)"
          xyChart = {
            dataSets = [
              {
                plotType       = "STACKED_BAR"
                legendTemplate = "{{metric.label.embedding_model}}"
                timeSeriesQuery = {
                  timeSeriesFilter = {
                    filter = "metric.type=\"logging.googleapis.com/user/${var.app_name}/ingestion_chunks_added\""
                    aggregation = {
                      alignmentPeriod    = "60s"
                      perSeriesAligner   = "ALIGN_DELTA"
                      crossSeriesReducer = "REDUCE_MEAN"
                      groupByFields      = ["metric.label.embedding_model"]
                    }
                  }
                }
              }
            ]
          }
        },
        {
          title = "8. Cloud Run Request Volume by Response Code Class"
          xyChart = {
            dataSets = [
              {
                plotType       = "STACKED_AREA"
                legendTemplate = "{{resource.label.service_name}} ({{metric.label.response_code_class}})"
                timeSeriesQuery = {
                  timeSeriesFilter = {
                    filter = "metric.type=\"run.googleapis.com/request_count\" AND resource.type=\"cloud_run_revision\""
                    aggregation = {
                      alignmentPeriod    = "60s"
                      perSeriesAligner   = "ALIGN_RATE"
                      crossSeriesReducer = "REDUCE_SUM"
                      groupByFields      = ["resource.label.service_name", "metric.label.response_code_class"]
                    }
                  }
                }
              }
            ]
          }
        }
      ]
    }
  })

  depends_on = [
    google_project_service.required_apis["monitoring.googleapis.com"],
    google_logging_metric.agent_turn_latency_ms,
    google_logging_metric.agent_ttft_ms,
    google_logging_metric.agent_llm_stage_latency_ms,
    google_logging_metric.agent_retrieval_stage_latency_ms,
    google_logging_metric.agent_input_tokens,
    google_logging_metric.agent_output_tokens,
    google_logging_metric.agent_total_tokens,
    google_logging_metric.retrieval_mean_similarity_score,
    google_logging_metric.agent_ungrounded_answers,
    google_logging_metric.ingestion_chunks_added,
  ]
}
