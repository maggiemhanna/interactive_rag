# Compliance & Governance Plan (Data Residency, Audit Logging, Regulatory Requirements & Policy Enforcement)

This document outlines the architecture and implementation plan for the fourth defense-in-depth layer of **InteractiveRAG**: **Compliance & Governance**.

While earlier security layers protect authentication, network boundaries, and AI prompts/outputs, this layer ensures that the platform handles user data responsibly, transparently, and within strict geographic and operational rules. It is organized around four pillars:

1. **Data Residency** — Ensures that all uploaded documents, vector embeddings, application logs, and AI model requests stay within approved single-region boundaries. The platform adheres to the Google Cloud Organization's location policy and pins every storage, compute, and AI service to a single configured region rather than global or multi-regional endpoints.
2. **Audit Logging** — Maintains a complete, privacy-safe record of system activity—capturing **who** performed **what** action and **when**—without ever logging raw user questions or personal data. Every request is tagged with a shared correlation ID so administrators can trace a single user action across the Load Balancer, backend services, security filters, and underlying cloud storage/AI calls in Google Cloud Logging.
3. **Regulatory Requirements (GDPR & EU AI Act)** — Gives users direct control over their personal data and clear visibility into how AI responses are produced:
   - **Right of Access (GDPR Art. 15):** Users can view a complete inventory of their stored collections, uploaded PDF files, storage footprint, and active chat sessions.
   - **Right to Erasure (GDPR Art. 17):** Users can permanently erase all of their data—including current and archived file versions in Cloud Storage, vector indexes, and conversation history—in a single action.
   - **AI Transparency (EU AI Act Art. 50):** Every assistant reply includes clear provenance metadata and visual badges indicating that the response is AI-generated, which model and region produced it, whether it is grounded in cited source documents, and which safety or privacy filters ran.
4. **Policy Enforcement** — Protects system reliability, fairness, and compliance through automated guardrails:
   - **Fail-Closed Security Screening:** In production, if safety or privacy screening services are unreachable, requests are safely rejected rather than allowing unscreened content through.
   - **Per-User Rate Limits & Quotas:** Sliding-window request rate limits and per-user collection caps prevent abuse and runaway cloud costs.
   - **Deployment & Infrastructure Guardrails:** Production services are protected against accidental deletion, and automated pre-deploy checks block any deployment that violates regional policies or security settings.

---

## 1. Target Architecture (Before vs. After)

### Before
- **Data Residency:** AI model calls and embeddings could route through global or multi-regional endpoints by default, and deployments did not verify whether the target region complied with the organization's allowed locations.
- **Audit Logging:** Application logs were unstructured and recorded raw user questions in plain text before privacy filtering ran. There was no shared request ID to connect logs across services, no data-access audit logging on cloud storage or AI services, and no dedicated long-term audit log archive.
- **Regulatory Requirements:** Deleting a document collection left behind archived PDF copies, older file versions in Cloud Storage, and in-memory chat history. Assistant replies provided no structured disclosure of AI generation, grounding status, or safety filtering.
- **Policy Enforcement:** Temporary outages in the safety screening service caused requests to fail open in production. Authenticated users had no per-minute request limits or caps on the number of collections they could create, and production services lacked deletion protection and automated pre-deploy compliance checks.

### After
- **Data Residency:** Every storage bucket, backend service, security filter, embedding model, and generative AI call is locked to a single concrete cloud region that adheres to the Google Cloud Organization's resource location policy. Multi-regional and global routing are rejected both at deployment time and at runtime.
- **Audit Logging:** All user actions, security blocks, and data-access events are written as structured JSON records to a dedicated audit stream in Google Cloud Logging and archived in a regional compliance log bucket. User questions are replaced with one-way cryptographic hashes so personal data never leaks into logs, and a shared request/trace ID links every layer of a request together.
- **Regulatory Requirements:** Users have a self-service **"My Data & Privacy"** interface and API to inspect their complete data footprint (GDPR Art. 15) or permanently wipe all live and historical storage objects, vector databases, and chat threads (GDPR Art. 17). Every AI reply displays transparency, source-grounding, and safety verification badges (EU AI Act Art. 50).
- **Policy Enforcement:** Production safety screening operates strictly fail-closed, per-user rate limits and collection quotas prevent resource abuse, production Cloud Run services are guarded against accidental deletion, and pre-deploy checks automatically verify regional and security compliance before any infrastructure change is applied.

---

## 2. Scope Boundaries (What We Implement vs. Exclude)

To keep the governance layer practical, maintainable, and aligned with cloud security best practices, the table below summarizes what is implemented in this architecture and what is intentionally left out of scope.

| Governance Pillar | In Scope (Implemented) | Out of Scope (Intentionally Excluded) |
| :--- | :--- | :--- |
| **1. Data Residency** | • Adherence to the Google Cloud Organization resource location policy<br>• Automated pre-deploy verification confirming the target region is permitted by the live Organization Policy<br>• Single unified regional configuration across infrastructure and backend services, explicitly rejecting `"global"` and multi-regions (`"us"`, `"eu"`, `"asia"`)<br>• Explicit regional endpoint binding for all generative AI, embedding, storage, and safety screening calls | • Customer-Managed Encryption Keys (Cloud KMS / CMEK); standard Google-managed regional encryption is used |
| **2. Audit Logging** | • Structured JSON audit events published directly to Google Cloud Logging with end-to-end request and trace ID correlation<br>• Privacy-safe query logging using SHA-256 hashes and character counts instead of raw user prompts<br>• Comprehensive audit trail covering authentication failures, document uploads, searches, chat queries, collection management, privacy requests, quota blocks, and AI safety events<br>• Cloud Audit Logs (`ADMIN_READ`, `DATA_READ`, `DATA_WRITE`) for Storage, Vertex AI, DLP, and Model Armor, plus Load Balancer access logs<br>• Dedicated regional Cloud Logging audit bucket with 365-day retention | — |
| **3. Regulatory Requirements** | • **GDPR Art. 15 (Right of Access):** Self-service API and UI modal summarizing a user's collections, uploaded PDFs, archived versions, storage bytes, and active chat threads<br>• **GDPR Art. 17 (Right to Erasure):** Complete, irreversible deletion of all live and archived Cloud Storage object versions, local vector/upload caches, and conversation memory for the user<br>• **Automated Storage Hygiene:** Cloud Storage lifecycle rules that automatically prune archived object versions after 7 days (keeping at most 3 noncurrent versions)<br>• **EU AI Act Art. 50 (Transparency):** Structured compliance metadata on both standard and streaming responses, paired with UI badges for AI disclosure, citation grounding, and safety filter actions | • Automatic time-based expiration of active user collections (users manage and delete active collections on demand)<br>• Ingest-time PII redaction before embedding (PII screening is enforced at query, retrieval, and response time) |
| **4. Policy Enforcement** | • Fail-closed AI safety and privacy screening in production (returning `HTTP 503` if screening services are unreachable)<br>• Per-user sliding-window rate limits for document uploads and queries (`HTTP 429` with `Retry-After`)<br>• Per-request upload ceilings (file size, file count, and chunk count limits) and per-user collection count quotas (`HTTP 403` when creating new collections past the limit)<br>• Accidental deletion protection enabled on production Cloud Run services<br>• Automated pre-deploy policy gate (`make policy-check`) blocking non-compliant configurations | • Role-based access control (RBAC) custom claims or email-domain allowlists in the backend |

---

## 3. Key Design Decisions (In Plain English)

### 3.1 Data Residency via Organization Policy Adherence & Single-Region Pinning
- **Adhering to Organization-Level Location Rules:** Allowed geographic regions are governed at the Google Cloud Organization level (`constraints/gcp.resourceLocations`), outside of application code. Before any infrastructure plan or deployment runs, an automated read-only check queries the effective Organization Policy on the target project and aborts immediately if no location restriction policy is active or if the target deployment region is not on the approved list.
- **Strict Single-Region Enforcement:** Some cloud SDKs default to `"global"` or multi-regional endpoints (such as `"us"` or `"eu"`) when a region is not explicitly provided. To prevent data or AI requests from leaving the chosen region:
  1. Both Terraform and the Python backend validate that the configured location is a concrete single region (for example, `us-central1` or `europe-west1`) and reject `"global"` or multi-region values.
  2. Every Vertex AI client (both chat models and embedding models), Model Armor client, and Cloud Storage bucket is explicitly bound to that single region.

### 3.2 Privacy-Safe Audit Logging & End-to-End Traceability
- **Immediate Visibility in Google Cloud Logging:** The backend writes structured JSON audit records directly to a dedicated `interactiverag-audit` log stream in Google Cloud Logging (with an automatic console fallback for local offline testing). Every event is immediately searchable in **Logs Explorer**.
- **Connecting Every Step of a Request:** A correlation middleware assigns or forwards a unique request ID (`X-Request-Id`) and links it with Google Cloud's trace context (`X-Cloud-Trace-Context`). Filtering by a single request ID or trace in Logs Explorer shows the entire lifecycle of a request: the Load Balancer entry, application events, AI safety checks, and underlying Google Cloud `DATA_READ` / `DATA_WRITE` audit logs.
- **Protecting User Privacy in Logs:** User prompts can accidentally contain sensitive personal information. Instead of writing raw query text to logs, the application logs a deterministic SHA-256 hash (`query_sha256`) and character length (`query_chars`). Administrators can still correlate repeated queries and investigate incidents without exposing raw user content in log storage.
- **Long-Term Regional Audit Archive:** Terraform enables data-access audit logs for Cloud Storage, Vertex AI, Cloud DLP, and Model Armor, enables HTTP access logging on the Load Balancer, and routes copies of all audit and access logs into a dedicated regional Cloud Logging bucket with configurable long-term retention (365 days by default).

### 3.3 Regulatory Compliance (GDPR Self-Service Rights & EU AI Act Transparency)
- **True Right to Erasure Across Versioned Storage:** Because Cloud Storage object versioning is enabled to protect against accidental corruption, simply deleting a collection leaves behind historical file versions and archived PDF uploads. When a user triggers **Right to Erasure** (`DELETE /compliance/my-data`), the platform enumerates and permanently deletes **every live and archived object version** under the user's storage prefix, wipes their local vector database and temporary upload directories, and clears all of their conversation threads from agent memory—without affecting any other user's data.
- **Clear AI Provenance & Safety Disclosure:** To satisfy **EU AI Act Article 50** transparency obligations, every agent response (both standard JSON and streaming SSE) includes a structured `compliance` metadata summary. The web interface uses this metadata to display clear badges on every assistant message showing:
  - Whether the reply was generated by AI or stopped by a safety guardrail, along with the exact model and region used.
  - Whether the answer is grounded in cited document passages.
  - Which Cloud Model Armor and Cloud DLP protections ran (for example, verifying the response is clean, or noting that sensitive PII was redacted or an unsafe prompt was blocked).
  - A **"My Data & Privacy"** modal where users can inspect their stored data inventory or erase all of their data at any time.

### 3.4 Fail-Closed Security, Abuse Prevention & Deployment Guardrails
- **Fail-Closed Screening in Production:** In local development, the app can run without cloud security templates for convenience. In production, fail-closed mode is strictly enforced: if Cloud Model Armor or Cloud DLP is misconfigured, unreachable, or times out during prompt screening, chunk retrieval masking, or response filtering, the backend immediately stops processing, logs a high-severity audit event, and returns `HTTP 503 Service Unavailable` so unscreened content is never exposed.
- **Fair-Use Rate Limits & Quotas:** Each authenticated user is protected by independent, in-memory sliding-window rate limiters for document uploads (default `10/minute`) and queries (default `30/minute`), returning `HTTP 429 Too Many Requests` with a `Retry-After` header when exceeded. Users are also capped at a maximum number of collections (default `20`), returning `HTTP 403 Forbidden` if they attempt to create a new collection above the cap while still allowing uploads into their existing collections.
- **Production Deletion Protection & Pre-Deploy Gate:** Terraform enables deletion protection on Cloud Run services only when deploying to the `prod` environment, preventing accidental service destruction while keeping development environments easy to tear down. Before any `make plan` or `make deploy` command executes, `make policy-check` automatically verifies live Organization Policy compliance, static single-region and fail-closed settings, and Terraform validity.

---

## 4. Step-by-Step Implementation Phases

### Prerequisite: Organization Location Policy
**Goal:** Establish a cloud-level boundary so resources can only be created in approved single regions.
- [x] Configure the **Google Cloud Resource Location Restriction** policy (`constraints/gcp.resourceLocations`) in Google Cloud Console on the target project (or parent folder/organization) to allow only your approved single-region location groups.

---

### Phase 1: Data Residency & Regional Pinning
**Goal:** Ensure that every infrastructure resource and AI model call is locked to a single concrete region and verified against the Organization Policy before deployment.

- [x] **Infrastructure Regional Validation (`infra/`):**
  - **`infra/variables.tf`:** Add a validation rule on `var.region` requiring concrete single-region syntax (e.g., `us-central1`, `europe-west1`) so `"global"` and multi-regions (`"us"`, `"eu"`, `"asia"`) are rejected during `terraform plan`.
  - **`infra/cloud_run.tf`:** Pin both `ingestion-api` and `agent-api` container environments to `var.region`.
  - **`infra/storage.tf`, `infra/ai_security.tf`, `infra/network.tf`:** Add Terraform `precondition` checks confirming all storage buckets, Model Armor templates, and Private Service Connect endpoints use a concrete single region.
- [x] **Automated Organization Policy Gate (`Makefile` & `scripts/`):**
  - **`scripts/check_region_policy.py` & `Makefile` (`check-region-policy`):** Query the project's effective `gcp.resourceLocations` Organization Policy via `gcloud` before `plan` and `deploy` to confirm that an active allowlist policy exists and that the configured `REGION` is permitted.
- [x] **Backend Regional Binding (`src/`):**
  - **`src/core/config.py` & `src/core/compliance/residency.py`:** Centralize regional configuration with `validate_gcp_region()`, rejecting `"global"` or multi-regional strings at runtime.
  - **`src/core/storage/chroma.py` & `src/agent/graph.py`:** Explicitly pass the validated region into `GoogleGenerativeAIEmbeddings` and `ChatGoogleGenerativeAI` so all Vertex AI embedding and chat calls route strictly to the regional endpoint.
  - **`src/core/ai_security/armor.py`:** Validate that Model Armor template targets and regional endpoints never resolve to `"global"`.

---

### Phase 2: Privacy-Safe Audit Logging & Trace Correlation
**Goal:** Create an end-to-end, PII-free audit trail across the Load Balancer, backend microservices, and Google Cloud services that is immediately searchable in Cloud Logging and archived regionally.

- [x] **Request Correlation & Structured Audit Logger (`src/core/context.py`, `src/core/compliance/`):**
  - **`src/core/context.py`:** Manage generic per-request correlation context (`RequestContext`: `request_id`, `trace_id`, `active_span_id`, `service_name`, `uid`, `method`, `path`) using Python `contextvars` and wire `RequestCorrelationMiddleware` into both services via `src/core/app_factory.py` to read or generate `X-Request-Id`, extract Cloud Run's `X-Cloud-Trace-Context`, and echo `X-Request-Id` on every HTTP response.
  - **`src/core/compliance/state.py`:** Store per-request compliance guardrail and retrieval state (`GUARDRAIL_FILTER_LABELS`, `mark_pii_filtered`, `mark_model_armor_active`, `record_guardrail_action`, `record_retrieved_chunks`) on the active `RequestContext`.
  - **`src/core/compliance/audit.py`:**
    - Implement `emit_audit_event()` to publish structured JSON audit records to the `interactiverag-audit` stream in Google Cloud Logging (with stdout JSON fallback when running offline).
    - Re-export `hash_query()` from `src/core/context.py` to automatically replace any raw user query text with `query_sha256` and `query_chars`.
- [x] **Application-Wide Audit Coverage & PII Scrubbing (`src/`):**
  - Remove plaintext user query logging from `src/agent/app.py`, `src/agent/stream.py`, and `src/ingestion/service.py`.
  - Emit structured audit events for authentication failures (`src/core/auth.py`), collection listing and deletion (`src/core/storage/routes.py`), document ingestion and vector search (`src/ingestion/app.py`), agent chat and streaming (`src/agent/app.py`, `src/agent/stream.py`), and Model Armor / DLP blocks or redactions (`src/core/ai_security/armor.py`, `src/agent/graph.py`).
- [x] **Cloud Audit Logs, Load Balancer Logs & Regional Audit Bucket (`infra/`):**
  - **`infra/apis.tf`, `infra/iam.tf`, `Makefile`:** Enable `logging.googleapis.com`, grant `roles/logging.configWriter` to the Terraform deployer service account, and grant `roles/logging.logWriter` to the backend runtime service account.
  - **`infra/compliance.tf`:**
    - Enable `ADMIN_READ`, `DATA_READ`, and `DATA_WRITE` Cloud Audit Logs for Cloud Storage, Vertex AI, Cloud DLP, and Model Armor.
    - Provision a regional Cloud Logging bucket (`google_logging_project_bucket_config.compliance_audit`) pinned to `var.region` with configurable retention (`var.audit_log_retention_days`, default 365 days).
    - Provision a project log sink (`google_logging_project_sink.compliance_audit_sink`) that copies `interactiverag-audit` events, Cloud Audit Logs, and Load Balancer HTTP logs into the regional compliance bucket while keeping them visible in Logs Explorer.
  - **`infra/load_balancer.tf`:** Enable HTTP access logging on both `ingestion_backend` and `agent_backend`.

---

### Phase 3: Regulatory Requirements (GDPR Self-Service & EU AI Act Transparency)
**Goal:** Empower users to inspect or permanently erase all of their stored personal data (GDPR Art. 15 & 17) and provide clear AI transparency and grounding indicators on every response (EU AI Act Art. 50).

- [x] **GDPR Data Inventory & Complete Versioned Erasure (`src/`):**
  - **`src/core/compliance/dsar.py`:**
    - Implement `get_user_data_inventory(uid)` to return a user's collections, passage counts, uploaded PDF filenames, live and archived Cloud Storage object counts, and total storage bytes.
    - Implement `purge_all_user_data(uid)` to permanently delete **all live and noncurrent Cloud Storage object versions** under `users/{uid}/` (`versions=True`), plus local `/tmp/chroma_db/{uid}` and `/tmp/uploads/{uid}` directories.
  - **`src/agent/memory.py`:** Implement `list_user_memory_threads(uid)` and `purge_user_memory_threads(uid)` to count and wipe all `{uid}:*` conversation checkpoints from in-memory chat storage.
  - **`src/core/compliance/routes.py` & `src/core/app_factory.py`:** Expose `GET /compliance/my-data` (Art. 15 inventory) and `DELETE /compliance/my-data` (Art. 17 complete erasure) on both backend services, emitting `dsar.export_inventory` and `dsar.erase_user_data` audit events.
- [x] **EU AI Act Provenance Metadata & Frontend Privacy Controls (`src/` & `frontend/`):**
  - **`src/core/compliance/provenance.py`, `src/core/compliance/state.py`, `src/agent/schemas.py`, `src/agent/app.py`, `src/agent/stream.py`:** Build and attach a structured `compliance` metadata object (`ai_generated`, `model`, `region`, `grounding_enforced`, `grounded`, `citation_count`, `retrieved_passages`, `pii_filtered`, `model_armor_active`, `filters_checked`, `guardrail_actions`, `request_id`) to all `/agent` JSON responses and `/agent/stream` SSE completion events.
  - **`frontend/src/api.js` & `frontend/src/App.jsx`:**
    - Render transparency badges on every assistant message showing AI generation status, model and region, citation grounding status, and any Model Armor / DLP safety actions taken.
    - Add the **"My Data & Privacy"** self-service modal allowing authenticated users to inspect their stored data footprint and trigger complete account data erasure.

---

### Phase 4: Policy Enforcement (Fail-Closed Screening, Quotas & Deployment Guardrails)
**Goal:** Enforce fail-closed behavior when safety services are unavailable in production, protect multi-tenant resources with per-user rate limits and quotas, and prevent misconfigured deployments.

- [x] **Fail-Closed Security Screening (`src/core/ai_security/` & `src/`):**
  - **`src/core/config.py` & `src/core/ai_security/armor.py`:** Add `MODEL_ARMOR_FAIL_CLOSED` (enabled in Cloud Run deployments). When active, any missing template or upstream error calling Model Armor or Cloud DLP raises `SecurityScreeningUnavailableError` and emits an audit event.
  - **`src/agent/app.py`, `src/agent/stream.py`, `src/ingestion/app.py`:** Catch `SecurityScreeningUnavailableError` and return `HTTP 503 Service Unavailable` (or an SSE error frame with `http_status: 503`) so unscreened prompts, chunks, or responses never pass through.
- [x] **Per-User Rate Limits & Collection Quotas (`src/core/compliance/policy.py`):**
  - Implement thread-safe per-user sliding-window rate limiters for ingestion (`RATE_LIMIT_INGEST_PER_MINUTE`, default `10`) and queries (`RATE_LIMIT_QUERY_PER_MINUTE`, default `30`), returning `HTTP 429` with a `Retry-After` header and emitting `policy.rate_limit_exceeded` audit events.
  - Implement `enforce_collection_quota(uid, collection_name)` to cap the number of collections a user can own (`MAX_COLLECTIONS_PER_USER`, default `20`), returning `HTTP 403` and emitting `policy.collection_quota_exceeded` when creating a new collection over the limit while allowing uploads into existing collections.
  - Enforce rate limits and quotas in `src/ingestion/upload_parser.py`, `src/ingestion/app.py`, and `src/agent/app.py` **before** archiving uploaded PDFs to Cloud Storage.
- [x] **Infrastructure Protection & Pre-Deploy Policy Gate (`infra/`, `scripts/`, `Makefile`):**
  - **`infra/cloud_run.tf`:** Set `deletion_protection = var.environment == "prod"` on `ingestion_api`, `agent_api`, and `frontend`.
  - **`scripts/check_static_policies.py` & `Makefile` (`policy-check`):** Automatically verify before `make plan` and `make deploy` that the live Organization Policy allows `REGION`, no Python or Terraform config uses `"global"` or multi-regions, fail-closed screening is enabled, production deletion protection is configured, and `terraform validate` succeeds.

---

### Phase 5: Automated Compliance Test Suite & Documentation
**Goal:** Continuously verify all compliance, residency, privacy, and policy guardrails offline before code reaches production.

- [x] **`tests/compliance.py`:** Comprehensive offline `unittest` suite covering:
  - Phase 1: Single-region validation, rejection of `"global"` and multi-regions, explicit regional binding on Vertex AI clients, Organization Policy evaluation, and single-source-of-truth model allowlists.
  - Phase 2: End-to-end `X-Request-Id` and `X-Cloud-Trace-Context` correlation across REST and SSE, Cloud Logging publication with graceful fallback, and zero plaintext query leakage across all loggers.
  - Phase 3: GDPR Art. 15 inventory and Art. 17 complete erasure across live and noncurrent Cloud Storage object versions, local caches, and conversation memory with strict multi-tenant isolation, plus EU AI Act Art. 50 provenance metadata across all guardrail states.
  - Phase 4: Fail-closed `HTTP 503` screening across prompt, chunk, and output stages (REST and SSE), per-user sliding-window rate limits (`HTTP 429` + `Retry-After`), per-user collection quotas (`HTTP 403` on new collections vs. `HTTP 200` on existing collection appends), and static repository policy checks.
- [x] **`README.md` & `docs/COMPLIANCE_GOVERNANCE_PLAN.md`:** Document the Compliance & Governance architecture, self-service DSAR endpoints, and verification workflows.

---

## 5. Compliance & Governance Verification Playbook

### 1. Pre-Deploy Organization Policy & Static Governance Checks
Verify that the target Google Cloud project has an active resource location policy, that your configured region is permitted, and that all Terraform and Python policy invariants pass:

```bash
# 1. Inspect the live effective GCP Organization Policy on your project
gcloud resource-manager org-policies describe gcp.resourceLocations \
  --project="${PROJECT_ID}" --effective

# 2. Run the live Organization Policy adherence check
make check-region-policy PROJECT_ID="${PROJECT_ID}" REGION="${REGION}"

# 3. Run offline static policy checks (single-region pinning, fail-closed Model Armor, prod deletion protection)
make check-static-policy

# 4. Run the full pre-deploy governance gate (runs automatically before `make plan` and `make deploy`)
make policy-check PROJECT_ID="${PROJECT_ID}" REGION="${REGION}"
```

### 2. Run the Automated Offline Test Suite
Run the compliance test suite (or the full repository test suite) completely offline without requiring live cloud credentials:

```bash
# Run the dedicated Compliance & Governance test suite
.venv/bin/python -m unittest tests.compliance -v

# Run all security, multi-tenancy, storage, and compliance test suites together
.venv/bin/python -m unittest tests.auth tests.storage tests.multi_tenant_api tests.ai_security tests.compliance -v
```

### 3. Live API Verification Commands

```bash
# 1. End-to-End Trace Correlation & EU AI Act Art. 50 Provenance Metadata (JSON)
curl -i -X POST "${AGENT_URL}/agent" \
  -H "Authorization: Bearer ${ID_TOKEN}" \
  -H "X-Request-Id: req-audit-demo-001" \
  -H "Content-Type: application/json" \
  -d '{"query": "Summarize key findings.", "collection_name": "financial_reports", "stream": false}'
# Expect:
# - HTTP response header: X-Request-Id: req-audit-demo-001
# - JSON body includes "compliance": {
#     "ai_generated": true, "model": "gemini-2.5-flash", "region": "us-central1",
#     "grounding_enforced": true, "grounded": true, "citation_count": ...,
#     "pii_filtered": false, "model_armor_active": true,
#     "filters_checked": ["pi_and_jailbreak", "malicious_uri", "rai", "sdp"],
#     "guardrail_actions": [], "request_id": "req-audit-demo-001"
#   }

# 2. Streaming SSE Provenance Metadata
curl -N -X POST "${AGENT_URL}/agent/stream" \
  -H "Authorization: Bearer ${ID_TOKEN}" \
  -H "X-Request-Id: req-audit-stream-001" \
  -H "Content-Type: application/json" \
  -d '{"query": "Summarize key findings.", "collection_name": "financial_reports"}'
# Expect: Final SSE frame before [DONE] with {"event": "compliance", "compliance": {...}}

# 3. GDPR Art. 15 — Self-Service Data Inventory (Right of Access)
curl -s "${AGENT_URL}/compliance/my-data" \
  -H "Authorization: Bearer ${ID_TOKEN}" \
  -H "X-Request-Id: req-dsar-inventory-001" | jq .
# Expect: JSON inventory of collections, total_passages, uploaded_pdfs,
#         gcs_live_object_count, gcs_noncurrent_version_count, total_bytes, and memory_thread_count.

# 4. GDPR Art. 17 — Complete Right to Erasure
curl -s -X DELETE "${AGENT_URL}/compliance/my-data" \
  -H "Authorization: Bearer ${ID_TOKEN}" \
  -H "X-Request-Id: req-dsar-erase-001" | jq .
# Expect: {"status": "success", "deleted_gcs_live_objects": ..., "deleted_gcs_noncurrent_versions": ..., "deleted_memory_threads": ...}
# Re-running GET /compliance/my-data afterwards returns 0 collections, 0 files, 0 bytes, and 0 memory threads.

# 5. Policy Enforcement — Per-User Rate Limiting (HTTP 429 + Retry-After)
# Sending more than RATE_LIMIT_QUERY_PER_MINUTE (default 30) queries within 60 seconds returns:
# HTTP/2 429 Too Many Requests with header `Retry-After: <seconds>`

# 6. Policy Enforcement — Per-User Collection Quota (HTTP 403)
# Attempting to create a new collection once MAX_COLLECTIONS_PER_USER (default 20) is reached returns:
# HTTP/2 403 Forbidden (while uploading additional PDFs into an existing collection continues to succeed with HTTP 200).
```

### 4. Inspect Audit Records in Google Cloud Logging (Logs Explorer & CLI)
Open **Google Cloud Console → Logging → Logs Explorer** for your project and use these filters:

```text
# 1. View all structured InteractiveRAG application audit events
#    (ingest, query, agent chat, collection delete, DSAR inventory/erasure, rate/quota blocks, Model Armor/DLP events)
logName="projects/${PROJECT_ID}/logs/interactiverag-audit"

# 2. Trace a single request end-to-end across all services by X-Request-Id
#    (verifies user questions are hashed as query_sha256 with zero plaintext PII)
jsonPayload.request_id="req-audit-demo-001"

# 3. View only security blocks, PII redactions, or policy quota rejections
logName="projects/${PROJECT_ID}/logs/interactiverag-audit" AND (jsonPayload.event_type:"security." OR jsonPayload.event_type:"policy." OR jsonPayload.event_type="auth.failure")

# 4. View GDPR Art. 15 & Art. 17 self-service DSAR audit events
logName="projects/${PROJECT_ID}/logs/interactiverag-audit" AND jsonPayload.event_type:"dsar."

# 5. View Google Cloud DATA_READ / DATA_WRITE Audit Logs (Cloud Storage, Vertex AI, Cloud DLP, Model Armor)
logName="projects/${PROJECT_ID}/logs/cloudaudit.googleapis.com%2Fdata_access"

# 6. View Global External HTTPS Load Balancer access logs
resource.type="http_load_balancer"
```

Or query the audit stream directly from your terminal:

```bash
gcloud logging read "logName=\"projects/${PROJECT_ID}/logs/interactiverag-audit\"" \
  --project="${PROJECT_ID}" --limit=10 --format=json
```
