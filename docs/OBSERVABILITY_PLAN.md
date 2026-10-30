# Observability & AI Monitoring Plan (Structured Logging, Metrics, Distributed Tracing & AI Telemetry)

This document outlines the architecture and implementation plan for the fifth defense-in-depth layer of **InteractiveRAG**: **Observability & AI Monitoring**.

While earlier layers secure cloud deployment, private networking, AI prompts/outputs, and regulatory compliance, this layer gives operators clear visibility into how the system and its AI models perform in real time. Whereas compliance audit logging records **who** performed **what** action and **when** for legal accountability, observability answers **how fast**, **at what token cost**, and **with what retrieval and grounding quality** each request executed—all while keeping user data private and staying strictly inside our single-region, zero-internet network perimeter. It is organized around four pillars:

1. **Structured Logging** — Formats application logs in cloud deployments as single-line, machine-readable JSON records rather than plain colored text. Every log message is automatically tagged with the active request ID, Google Cloud trace ID, active step (span) ID, service name, and source code location, and is stored inside a dedicated single-region log bucket while keeping readable colored logs for local terminal development.
2. **Metrics** — Tracks numerical performance and reliability indicators over time—including request latency distributions (`p50`, `p95`, `p99`), token consumption by model, ungrounded response counts, and safety guardrail blocks—using **Google Cloud Monitoring** log-based metrics, dashboards, and alert policies.
3. **Distributed Tracing** — Breaks down every multi-step document upload, search, and AI chat request into a timeline of individually timed sub-steps (spans)—such as syncing the vector database from Cloud Storage, screening the prompt with Model Armor, running each language model reasoning step, searching ChromaDB, redacting sensitive data, and screening the final answer—linked together under a single trace in **Google Cloud Trace**.
4. **AI-Specific Monitoring (Latency, Token Usage & Quality)** — Tracks generative AI behavior that standard web server monitoring cannot see:
   - **Latency Breakdown & Time-to-First-Token (TTFT):** Measures total response time, how quickly the first streaming event reaches the user, and how many milliseconds were spent in safety screening versus vector search versus language model generation.
   - **Token Usage:** Captures prompt tokens, completion tokens, and total tokens across all language model tool-calling steps in a conversation turn, attributed by model and collection.
   - **Retrieval & Grounding Quality:** Computes deterministic real-time quality signals on every turn—including vector similarity scores of retrieved passages, number of search steps taken, retrieved passage count, inline page citation count, and whether the final answer is grounded in cited source documents.

---

## 1. Target Architecture (Before vs. After)

### Before
- **Plain-Text Application Logs in the Cloud:** While compliance events were written as structured JSON, general application logs used terminal color formatting unconditionally. In Cloud Run, those logs appeared as unindexed plain text without automatic trace links, step IDs, or request correlation, and landed only in Google Cloud's default global log bucket.
- **Compliance & Request Context Coupled Together:** Per-request correlation state (such as request IDs, trace IDs, and user IDs) lived inside the compliance package under audit-specific naming, forcing authentication, AI security, and agent modules to depend on compliance internals.
- **No Aggregated Metrics or Dashboards:** Operators could search individual log events, but there were no aggregated latency percentiles (`p50`, `p95`, `p99`), token consumption counters, or Cloud Monitoring dashboards to spot slowdowns or error spikes at a glance.
- **No Step-by-Step Timing, Token Tracking, or Retrieval Quality Scores:** When an AI chat turn took several seconds, there was no breakdown showing which internal step was slow. Token counts returned by Gemini and similarity scores returned by ChromaDB were discarded after each turn.

### After
- **Structured, Region-Pinned Application Logs:** In Cloud Run, all application logs are emitted as single-line JSON enriched with request, trace, and step correlation IDs and routed into a dedicated regional log bucket in the project's configured cloud region. Local development continues to display colored terminal logs.
- **Clean Separation Between Shared Context, Compliance Audit, and Observability:** Request correlation lives in a neutral core module (`src/core/context.py`) shared across the backend. Compliance events continue flowing to the long-term `interactiverag-audit` stream, while performance and AI metrics flow to a separate `interactiverag-telemetry` stream.
- **Cloud Monitoring Metrics, Dashboards & Alerts:** Google Cloud's Log Router automatically extracts latency distributions, token counts, and grounding rates from `interactiverag-telemetry` events into **Google Cloud Monitoring** dashboards and alert policies with zero extra network calls per request.
- **End-to-End Tracing & AI Telemetry on Every Turn:** Every stage of ingestion, retrieval, safety screening, and LangGraph agent execution is timed as a child span. Token consumption, stage-by-stage latencies, vector similarity scores, and grounding outcomes are recorded on every turn in `interactiverag-telemetry`.

---

## 2. Scope Boundaries (What We Implement vs. Exclude)

To keep the observability layer lightweight, privacy-safe, and compatible with our zero-internet VPC architecture, the table below summarizes what is implemented in this architecture and what is intentionally left out of scope.

| Observability Pillar | In Scope (Implemented) | Out of Scope (Intentionally Excluded) |
| :--- | :--- | :--- |
| **1. Structured Logging** | • Single-line JSON operational logs on Cloud Run enriched with request ID, trace ID, active span ID, user ID, service name, and source file/line<br>• Neutral shared request context (`src/core/context.py`) decoupling compliance auditing from operational logging<br>• Automatic query scrubbing so raw user prompts never appear in operational logs<br>• Dedicated regional Cloud Logging bucket (`var.region`) storing operational and telemetry logs<br>• Colored console logs preserved for local development | • Exporting logs to external third-party SaaS platforms (such as Datadog, Splunk, or New Relic) that require outbound public internet access |
| **2. Metrics** | • **Google Cloud Monitoring** log-based metrics (`google_logging_metric`) extracting latency distributions, token usage, ungrounded response counts, and safety blocks from structured telemetry logs<br>• Unified Cloud Monitoring observability dashboard and latency/error alert policies provisioned via Terraform | • Running standalone Prometheus or Grafana servers inside the VPC<br>• Exposing a dedicated custom REST metrics endpoint (`GET /observability/metrics`) on the backend containers |
| **3. Distributed Tracing** | • Hierarchical span tracking across Cloud Storage sync, PDF parsing/chunking, Vertex AI embeddings, Model Armor screening, and each LangGraph node (`input_guard`, `model_call`, `tools`, `output_guard`)<br>• Per-stage latency rollups recorded on every request<br>• Optional background span export to **Google Cloud Trace** over Private Google Access | • Browser-side OpenTelemetry JavaScript SDK instrumentation in the React frontend<br>• Surfacing internal trace spans or latency breakdowns as badges in the chat UI |
| **4. AI-Specific Monitoring** | • **Latency:** End-to-end turn duration (`total_latency_ms`), streaming Time-to-First-Token (`ttft_ms`), and per-stage millisecond breakdown (`gcs_sync_ms`, `input_guard_ms`, `llm_ms`, `retrieval_ms`, `output_guard_ms`)<br>• **Token Usage:** Prompt, completion, and total token counts accumulated across all Gemini tool-calling steps in a turn (`AIMessage.usage_metadata`)<br>• **Quality:** ChromaDB vector similarity scores (`top_similarity_score`, `mean_similarity_score`), retriever tool step counts, retrieved passage counts, inline citation counts, and grounding status (`grounded`) | • Heuristic abstention text detection or secondary "LLM-as-a-Judge" model calls on live user traffic<br>• Displaying token or latency telemetry badges in the frontend chat UI |

---

## 3. Key Design Decisions (In Plain English)

### 3.1 Separating Shared Request Context, Compliance Audit Logs, and Observability Telemetry
- **One Neutral Request Context for the Whole Backend:** Every incoming HTTP request carries correlation identifiers (`X-Request-Id`, `X-Cloud-Trace-Context`, and the authenticated user's ID) that are needed by authentication, AI security guardrails, compliance audit logs, and observability tools alike. Moving this shared request state directly into a neutral core module (`src/core/context.py`)—and removing `src/core/compliance/context.py`—means neither the compliance package nor the observability package depends on the other.
- **Separate Log Streams for Separate Purposes:** Mixing high-frequency performance telemetry into regulatory audit logs makes both harder to manage. Instead, the platform separates logs into three clear channels:
  1. **Compliance Audit Stream (`interactiverag-audit`):** Records *who* did *what* and *when* (sign-in failures, document uploads, data erasures, security blocks) and routes them into the 365-day regional compliance archive.
  2. **AI & Performance Telemetry Stream (`interactiverag-telemetry`):** Emits one structured summary record per completed request containing stage latencies, span timings, token counts, and retrieval quality scores.
  3. **Structured Operational Logs (`stdout` / `stderr`):** Records day-to-day application events, warnings, and error stack traces as single-line JSON.
- **Single-Region Storage for All Observability Logs:** Because Google Cloud's built-in `_Default` log bucket is global, Terraform provisions a dedicated **regional observability log bucket** pinned to the project's single configured cloud region (`var.region`, with a 30-day operational retention) and routes both `interactiverag-telemetry` and Cloud Run container logs into it.

### 3.2 Why We Need Both `request_id` and `trace_id` (Plus `span_id`)
A common question is: *If we already generate an `X-Request-Id` on every call, why do we also need a `trace_id` and `span_id`?* They solve two different problems:
- **What `request_id` (`X-Request-Id`) Does — Application & Client Correlation:**
  - `X-Request-Id` is an application-level header (for example, `req-audit-demo-001`) that a caller or the frontend can pass in and receive back in the HTTP response headers and compliance metadata.
  - However, **only our Python code knows about `X-Request-Id`**. Managed Google Cloud infrastructure services outside our Python code—such as the **Global External HTTPS Load Balancer** and **Cloud Run's internal request proxy**—do not read `X-Request-Id` and do not include it in their logs.
- **What `trace_id` (`X-Cloud-Trace-Context`) Does — Google Cloud Infrastructure & Waterfall Correlation:**
  - When a user's request enters the **Google Cloud HTTPS Load Balancer**, Google Cloud itself generates a 32-character hexadecimal **`trace_id`** and attaches the header `X-Cloud-Trace-Context: <trace_id>/<span_id>;o=1` before forwarding the request to Cloud Run.
  - Because the Load Balancer, Cloud Run, Cloud Logging, and **Google Cloud Trace** all natively tag their records with this `trace_id` (`projects/<project>/traces/<trace_id>`), attaching `logging.googleapis.com/trace` to our Python logs links **Google's Load Balancer access log + Cloud Run's request log + our Python operational, audit, and telemetry logs + the visual Cloud Trace timeline** into a single unified view.
  - In addition, **Google Cloud Trace** requires a valid 32-character hex `trace_id` and 16-character hex `span_id`s to build visual step-by-step latency waterfalls (it cannot accept arbitrary strings like `req-audit-demo-001`).
- **What `span_id` Does — Timing Individual Steps Inside a Request:**
  - While every log and step in a request shares the same **`trace_id`**, each internal stage of our pipeline (`gcs_sync`, `input_guard`, `model_call #1`, `retriever_tool`, `model_call #2`, `output_guard`) gets its own **`span_id`** and duration (`duration_ms`). This shows operators the exact millisecond breakdown of every internal step inside the request.

### 3.3 Dual-Mode Logging: Structured JSON in the Cloud, Colored Logs Locally
- **Zero-Overhead Structured Logs on Cloud Run:** In Cloud Run, anything written to standard output (`stdout`) as a single-line JSON object is automatically parsed by Google Cloud's built-in log agent. By formatting cloud logs with Google Cloud's standard fields (`severity`, `message`, `logging.googleapis.com/trace`, `logging.googleapis.com/spanId`, and `logging.googleapis.com/sourceLocation`), every application log entry is automatically indexed and pinned to the active trace and step in **Logs Explorer** without extra network calls.
- **Automatic Context Attachment & Privacy Scrubbing:** A central logging filter automatically attaches the current `request_id`, `trace_id`, `span_id`, `service`, and `uid` from the shared request context to every log message across the codebase, and scrubs any accidental raw query text so personal data never leaks into operational logs.

### 3.4 How Telemetry Logs Power Cloud Monitoring Metrics Without Extra API Calls
- **One Telemetry Event per Request $\rightarrow$ Automatic Cloud Monitoring Metrics:** Instead of making a separate synchronous network call to the Cloud Monitoring API on every user request (which would add latency to user responses), the backend emits a single structured JSON record to `interactiverag-telemetry` when a request finishes.
- **Log Router Extraction:** Google Cloud's **Log Router** evaluates Terraform-configured log-based metrics (`google_logging_metric`) as those `interactiverag-telemetry` records arrive and automatically extracts numeric fields—such as total turn latency, stage latencies, token counts, and ungrounded answer counts—into **Google Cloud Monitoring** as time-series distributions and counters that power our dashboard and alert policies.

### 3.5 AI-Specific Monitoring: Stage Latency, Token Usage & Retrieval Quality
- **Stage-by-Stage Latency & Streaming TTFT:** Every turn records total wall-clock latency (`total_latency_ms`), streaming Time-to-First-Token (`ttft_ms` on `/agent/stream`), and the exact time spent in each pipeline stage (`gcs_sync_ms`, `input_guard_ms`, `llm_ms`, `retrieval_ms`, `output_guard_ms`).
- **Tracking Token Usage Across Multi-Step Tool Calls:** Every time the agent calls Vertex AI Gemini—first to plan a search query, and again to synthesize the final cited answer—the model returns token usage metadata (`input_tokens`, `output_tokens`, `total_tokens`). The backend accumulates these token counts across all steps in the turn so operators can monitor token consumption by model (`gemini-2.5-flash`, `gemini-2.5-pro`, `gemini-2.5-flash-lite`) and collection.
- **Deterministic Retrieval & Grounding Quality Signals:** Without making extra LLM judge calls, each turn records:
  - **Vector Similarity Scores (`top_similarity_score` & `mean_similarity_score`):** Measures how closely the retrieved ChromaDB passages matched the search query, making it easy to spot low-confidence retrieval or out-of-domain questions.
  - **Grounding & Citation Counts (`retrieved_passages`, `tool_calls_count`, `citation_count`, `grounded`):** Tracks how many search iterations the agent ran, how many passages were retrieved, and whether the final response included verified `[filename.pdf, p.X]` citations.

---

## 4. Step-by-Step Implementation Phases

### Phase 1: Core Context Refactoring & Package Separation (`src/core/context.py`, `src/core/compliance/state.py`, `src/core/observability/state.py`)
**Goal:** Replace `src/core/compliance/context.py` directly with a neutral top-level `src/core/context.py` module for generic request correlation, and give both `src/core/compliance/` and `src/core/observability/` their own dedicated `state.py` modules for per-request state accumulators.

- [x] **Move Generic Request Correlation to `src/core/context.py` (Delete `src/core/compliance/context.py`):**
  - Define `RequestContext` and `RequestCorrelationMiddleware` in `src/core/context.py` holding only generic request/trace correlation state (`request_id`, `trace_id`, `root_span_id`, `active_span_id`, `service_name`, `uid`, `method`, `path`, `request_start_perf`) and a thread-safe `data` store for domain modules.
  - Place compliance-specific guardrail and retrieval state helpers (`GUARDRAIL_FILTER_LABELS`, `mark_pii_filtered`, `mark_model_armor_active`, `record_guardrail_action`, `record_retrieved_chunks`) in `src/core/compliance/state.py`.
  - Place observability-specific accumulators (`get_elapsed_latency_ms`, `record_ttft`, `record_token_usage`, `record_retrieval_scores`, `record_tool_call`, `record_span`, `get_recorded_spans`, `get_stage_latency_ms`) in `src/core/observability/state.py`.
- [x] **Keep `src/core/compliance/audit.py` Dedicated to Compliance Audit Events:**
  - `emit_audit_event()` continues publishing regulatory and security audit records exclusively to `interactiverag-audit`.

---

### Phase 2: Structured JSON Application Logging & Trace Correlation (`src/core/observability/logging.py` & `src/core/logger.py`)
**Goal:** Emit single-line GCP-structured JSON operational logs on `stdout` in Cloud Run (or when `STRUCTURED_LOGGING="true"`), automatically enriched with `RequestContext` correlation fields, while preserving `coloredlogs` for local terminal development.

- [x] **Correlation Filter & Structured JSON Formatter (`src/core/observability/logging.py`):**
  - Implement `RequestContextLogFilter(logging.Filter)` to inject `request_id`, `trace_id`, `span_id`, `service`, and `uid` from `src/core/context.py` into every `LogRecord` and scrub any accidental raw query text.
  - Implement `GCPJsonFormatter(logging.Formatter)` producing single-line JSON records with `severity`, `message`, `timestamp`, `request_id`, `service`, `uid`, `logging.googleapis.com/trace`, `logging.googleapis.com/spanId`, and `logging.googleapis.com/sourceLocation`.
- [x] **Environment-Aware Logger Setup (`src/core/logger.py` & `src/core/config.py`):**
  - Add `is_structured_logging_enabled()` in `src/core/config.py` (enabled automatically on Cloud Run or when `STRUCTURED_LOGGING="true"`).
  - Update `setup_logging()` in `src/core/logger.py` to use `GCPJsonFormatter` + `RequestContextLogFilter` when structured logging is active, and `coloredlogs` otherwise.

---

### Phase 3: Distributed Tracing & Per-Stage Span Instrumentation (`src/core/observability/tracing.py` & `src/`)
**Goal:** Record hierarchical spans and millisecond timings across Cloud Storage synchronization, PDF ingestion, vector search, and every LangGraph agent node.

- [x] **Span Tracer (`src/core/observability/tracing.py`):**
  - Implement `start_span(name, stage=None, **attributes)` context manager that generates a 16-character hex `span_id`, links to the parent `active_span_id` on `RequestContext`, measures duration via `time.perf_counter()`, records the completed span and stage latency via `record_span()` in `src/core/observability/state.py`, and optionally exports spans to **Google Cloud Trace** when `CLOUD_TRACE_ENABLED="true"`.
- [x] **Pipeline Instrumentation (`src/core/storage/`, `src/ingestion/`, `src/agent/`):**
  - **`src/core/storage/gcs_sync.py`:** Instrument Cloud Storage download/upload sync (`stage="gcs_sync"`).
  - **`src/ingestion/splitter.py` & `src/ingestion/service.py`:** Instrument PDF splitting (`stage="chunking"`), batched Chroma indexing (`stage="embedding"`), and vector search (`stage="retrieval"`).
  - **`src/agent/graph.py` & `src/agent/tools.py`:** Instrument `input_guard` (`stage="input_guard"`), `model_call` (`stage="llm"`), retriever tool execution (`stage="retrieval"`), and `output_guard` (`stage="output_guard"`).

---

### Phase 4: AI-Specific Monitoring & `interactiverag-telemetry` Stream (`src/core/observability/telemetry.py`, `src/agent/`, `src/ingestion/`)
**Goal:** Capture per-turn latency breakdowns, streaming TTFT, Gemini token usage, and retrieval/grounding quality signals, and publish them as structured events to `interactiverag-telemetry`.

- [x] **AI Telemetry Helpers & `interactiverag-telemetry` Publisher (`src/core/observability/telemetry.py`):**
  - Implement `record_llm_usage(response)` in `src/agent/graph.py` (`model_call`) to extract `input_tokens`, `output_tokens`, and `total_tokens` from `AIMessage.usage_metadata` and accumulate them across multi-step tool calls via `record_token_usage()` in `src/core/observability/state.py`.
  - Implement `record_retrieval_metrics(docs_with_scores)` in `src/agent/tools.py` and `src/ingestion/service.py` to capture `top_similarity_score`, `mean_similarity_score`, retrieved passage counts, and tool call counts via `src/core/observability/state.py`.
  - Implement `emit_telemetry_event(event_type, **fields)` reading accumulated state from `src/core/observability/state.py` and publishing one structured AI/performance JSON record per completed request (`document.ingest`, `vectorstore.query`, `agent.query`) to `projects/<project>/logs/interactiverag-telemetry` (`TELEMETRY_LOG_NAME`), enriched with `total_latency_ms`, `ttft_ms`, `stage_latency_ms`, `span_count`, `input_tokens`, `output_tokens`, `total_tokens`, `llm_calls`, `tool_calls_count`, `retrieved_passages`, `top_similarity_score`, `mean_similarity_score`, `citation_count`, and `grounded`.

---

### Phase 5: Regional Observability Infrastructure, Offline Test Suite & Documentation (`infra/observability.tf`, `tests/observability.py`, `README.md`)
**Goal:** Provision the regional observability log bucket, Cloud Monitoring log-based metrics, and dashboard in Terraform, and verify all observability behavior offline with `unittest`.

- [x] **Terraform Observability Infrastructure (`infra/observability.tf`, `infra/apis.tf`, `infra/iam.tf`, `infra/cloud_run.tf`, `infra/outputs.tf`):**
  - **`infra/apis.tf` & `infra/iam.tf`:** Enable `monitoring.googleapis.com` and `cloudtrace.googleapis.com`; grant `roles/monitoring.metricWriter` and `roles/cloudtrace.agent` to `backend_sa`.
  - **`infra/cloud_run.tf`:** Set `TELEMETRY_LOG_NAME = "interactiverag-telemetry"`, `STRUCTURED_LOGGING = "true"`, and `CLOUD_TRACE_ENABLED = "true"` on `ingestion_api` and `agent_api`.
  - **`infra/observability.tf`:**
    - Provision a regional Cloud Logging bucket (`google_logging_project_bucket_config.observability_telemetry`) pinned to `var.region` (30-day default retention, with single-region precondition).
    - Provision a log sink (`google_logging_project_sink.observability_sink`) routing `interactiverag-telemetry` and Cloud Run `stdout`/`stderr` container logs into the regional `observability_telemetry` bucket.
    - Provision `google_logging_metric` resources over `interactiverag-telemetry` (`agent_turn_latency_ms`, `agent_ttft_ms`, `agent_llm_stage_latency_ms`, `agent_retrieval_stage_latency_ms`, `agent_input_tokens`, `agent_output_tokens`, `agent_total_tokens`, `retrieval_mean_similarity_score`, `agent_ungrounded_answers`, `ingestion_chunks_added`) and a unified 8-widget `google_monitoring_dashboard`.
- [x] **Automated Offline Test Suite (`tests/observability.py`) & Documentation (`README.md`):**
  - Verify `src/core/context.py` request isolation, structured JSON log formatting and query scrubbing, hierarchical span timings across LangGraph nodes, multi-turn token accumulation, similarity score tracking, SSE `ttft_ms`, and `interactiverag-telemetry` event emission.

---

## 5. Observability & AI Monitoring Verification Playbook

### 1. Run the Automated Offline Test Suite
Run the observability test suite (or all repository test suites together) completely offline without requiring live cloud credentials:

```bash
# Run the dedicated Observability & AI Monitoring test suite
.venv/bin/python -m unittest tests.observability -v

# Run all 6 repository test suites together
.venv/bin/python -m unittest tests.auth tests.storage tests.multi_tenant_api tests.ai_security tests.compliance tests.observability -v
```

### 2. Live API Verification Commands

```bash
# Send an Agentic RAG request with both X-Request-Id and X-Cloud-Trace-Context
curl -i -X POST "${AGENT_URL}/agent" \
  -H "Authorization: Bearer ${ID_TOKEN}" \
  -H "X-Request-Id: req-obs-demo-001" \
  -H "X-Cloud-Trace-Context: 105445aa7843bc8bf206b12000100000/1;o=1" \
  -H "Content-Type: application/json" \
  -d '{"query": "Summarize the key findings.", "collection_name": "financial_reports", "stream": false}'
# Expect:
# - HTTP response echoes `X-Request-Id: req-obs-demo-001`
# - A structured `agent.query` record is written to `interactiverag-telemetry` with:
#   {
#     "event_type": "agent.query", "model_name": "gemini-2.5-flash", "region": "us-central1",
#     "total_latency_ms": 1840.5,
#     "stage_latency_ms": {"input_guard_ms": 95.2, "llm_ms": 1310.4, "retrieval_ms": 340.1, "output_guard_ms": 94.8},
#     "input_tokens": 1420, "output_tokens": 215, "total_tokens": 1635, "llm_calls": 2,
#     "tool_calls_count": 1, "retrieved_passages": 5, "top_similarity_score": 0.87, "mean_similarity_score": 0.79,
#     "citation_count": 3, "grounded": true,
#     "request_id": "req-obs-demo-001", "trace_id": "105445aa7843bc8bf206b12000100000"
#   }
```

### 3. Inspect Logs, Traces & Metrics in Google Cloud Console
Open **Google Cloud Console → Logging → Logs Explorer** or **Cloud Trace** for your project and use these filters:

```text
# 1. View AI & Performance Telemetry Events (stage latencies, token usage, similarity scores, spans)
logName="projects/${PROJECT_ID}/logs/interactiverag-telemetry"

# 2. View Structured Cloud Run Operational Logs (stdout / stderr)
resource.type="cloud_run_revision" AND logName="projects/${PROJECT_ID}/logs/run.googleapis.com%2Fstdout"

# 3. View Regulatory Compliance & Security Audit Events (365-day regional compliance archive)
logName="projects/${PROJECT_ID}/logs/interactiverag-audit"

# 4. View the complete lifecycle of a single request across Load Balancer + Cloud Run + App Logs by Trace ID
trace="projects/${PROJECT_ID}/traces/105445aa7843bc8bf206b12000100000"

# 5. Find slow AI turns (> 3000ms) or high token turns (> 4000 tokens)
logName="projects/${PROJECT_ID}/logs/interactiverag-telemetry" AND (jsonPayload.total_latency_ms > 3000 OR jsonPayload.total_tokens > 4000)

# 6. Monitor ungrounded answers
logName="projects/${PROJECT_ID}/logs/interactiverag-telemetry" AND jsonPayload.grounded=false
```
