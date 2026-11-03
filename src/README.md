# InteractiveRAG - Source Module Documentation (`/src`)

This directory contains the Python backend packages for **InteractiveRAG**, a multi-tenant, compliance-aware Agentic Retrieval-Augmented Generation platform powered by **LangGraph**, **Google Cloud Vertex AI** (`gemini-2.5-flash`, `gemini-2.5-pro`, `gemini-2.5-flash-lite`, `gemini-embedding-001`, `text-embedding-005`), **Google Cloud Model Armor + Cloud DLP**, **ChromaDB**, **Google Cloud Storage**, and **FastAPI**.

To prevent CPU- and memory-intensive PDF ingestion from blocking interactive queries or agent chat, the backend is organized into **4 modular domain packages** (`src/ingest`, `src/manage`, `src/query`, `src/agent`) plus shared platform infrastructure (`src/core`), deployed across **2 specialized backend Cloud Run services**:
1. **`ingest-api` (`src.ingest.app:app`, Port 8000)**: Mounts **only** `src/ingest` (`POST /ingest` + `GET /health`) with low concurrency (`4`) and heavy vertical resources (`2 vCPU / 4Gi RAM`).
2. **`agent-api` (`src.agent.app:app`, Port 8001)**: Mounts `src/query` (`POST /query`), `src/manage` (`GET /collections`, `DELETE /collections/{collection_name}`, `GET/DELETE /compliance/my-data`), and `src/agent` (`POST /agent`, `POST /agent/stream`, `GET /health`) with high concurrency (`32`), sharing a single warm `/tmp/chroma_db/{uid}` cache and unified per-UID rate limiter.

---

## Table of Contents

- [1. `src/core/` - Shared Platform, Storage, AI Security, Compliance & Observability](#1-srccore---shared-platform-storage-ai-security-compliance--observability)
  - [Core Infrastructure](#core-infrastructure)
  - [`src/core/storage/` - Multi-Tenant Persistence, Incremental Delta Sync & Distributed Locking](#srccorestorage---multi-tenant-persistence-incremental-delta-sync--distributed-locking)
  - [`src/core/ai_security/` - Model Armor, Content Safety & Guardrails](#srccoreai_security---model-armor-content-safety--guardrails)
  - [`src/core/compliance/` - Data Residency, Audit Logging, Provenance & DSAR](#srccorecompliance---data-residency-audit-logging-provenance--dsar)
  - [`src/core/observability/` - Observability & AI Monitoring](#srccoreobservability---observability--ai-monitoring)
- [2. `src/ingest/` - Dedicated Document Ingestion Service (`ingest-api`, Port 8000)](#2-srcingest---dedicated-document-ingestion-service-ingest-api-port-8000)
  - [Core Responsibilities](#core-responsibilities)
  - [API Endpoints Reference (`http://localhost:8000`)](#api-endpoints-reference-httplocalhost8000)
  - [Usage & Examples](#usage--examples)
- [3. `src/manage/` - Collection Management & GDPR Self-Service Package](#3-srcmanage---collection-management--gdpr-self-service-package)
- [4. `src/query/` - Direct Vector Similarity Search Package](#4-srcquery---direct-vector-similarity-search-package)
- [5. `src/agent/` - Unified Interactive Agent, Query & Management Service (`agent-api`, Port 8001)](#5-srcagent---unified-interactive-agent-query--management-service-agent-api-port-8001)
  - [Core Responsibilities](#core-responsibilities-1)
  - [LangGraph Agent Workflow](#langgraph-agent-workflow)
  - [API Endpoints Reference (`http://localhost:8001`)](#api-endpoints-reference-httplocalhost8001)
  - [Streaming & Provenance Events](#streaming--provenance-events)
  - [Usage & Examples](#usage--examples-1)
- [Prerequisites & Environment Setup](#prerequisites--environment-setup)
- [Directory & Module Reference](#directory--module-reference)

---

## 1. `src/core/` - Shared Platform, Storage, AI Security, Compliance & Observability

`src/core/` centralizes cross-cutting platform infrastructure and domain packages shared by both Cloud Run backend services (`ingest-api` and `agent-api`).

### Core Infrastructure
- **`config.py`**: Single canonical source of truth for loading `.env` settings and parsing all environment variables across `src/` via typed runtime getters (`is_cloud_run`, `get_google_cloud_project`, `get_google_cloud_location`, `get_model_armor_location`, `get_allowed_origins`, `get_allowed_llm_models`, `get_allowed_embedding_models`, `get_bucket_name`, `get_chroma_tmp_max_bytes`, `get_chroma_tmp_min_free_bytes`, etc.) plus local service ports.
- **`context.py`**: Manages per-request `contextvars` correlation state (`RequestContext`, `RequestCorrelationMiddleware`, `hash_query`, `scrub_query_fields`) tracking sanitized `X-Request-Id`, Cloud Trace ID, active span ID, authenticated `uid`, and a thread-safe `data` store for domain modules.
- **`auth.py`**: Verifies Google Identity Platform / Firebase Bearer JWTs (`verify_bearer_token`, `get_current_user`, `get_current_user_info`), enforces path-safe `uid` validation (`validate_uid`), and blocks `AUTH_DISABLED` bypasses when running on Cloud Run.
- **`logger.py`**: Configures structured ANSI-colored console and GCP JSON logging (`setup_logging`, `format_dict_for_logs`).
- **`app_factory.py`**: Provides `create_app()` to wire CORS middleware, `RequestCorrelationMiddleware`, `/health` readiness probes, and an explicit `routers` sequence so `ingest-api` mounts only `[ingest_router]` while `agent-api` mounts `[agent_router, query_router, manage_router]`.

### `src/core/storage/` - Multi-Tenant Persistence, Incremental Delta Sync & Distributed Locking
- **`paths.py`**: Isolates per-user local directories (`/tmp/chroma_db/{uid}`, `/tmp/uploads/{uid}`), constructs GCS object prefixes, checkpoints SQLite WAL files, and evicts cached Chroma `SharedSystemClient` registries (`release_client_cache`).
- **`gcs_sync.py`**: Pulls and pushes per-user ChromaDB snapshots between `/tmp` and `gs://<bucket>/users/{uid}/chroma_db/` with incremental MD5 delta sync (`_compute_file_md5_base64`, uploading only new or changed files and deleting stale remote blobs), `version.json` invalidation, and raw PDF upload archiving (`archive_upload`).
- **`locks.py`**: Coordinates per-user `threading.RLock` instances, distributed `if_generation_match` GCS write locks with TTL stale-lock recovery (`user_write_lock`, `user_read_lock`), and bounded `/tmp` `tmpfs` LRU cache eviction (`evict_tmp_cache` enforcing `CHROMA_TMP_MAX_BYTES=512 MiB` and `CHROMA_TMP_MIN_FREE_BYTES=256 MiB`).
- **`chroma.py`**: Instantiates regional Vertex AI `GoogleGenerativeAIEmbeddings`, executes normalized similarity search (`similarity_search_with_scores`), and manages per-user Chroma collections (`get_vectorstore`, `require_collection`, `list_collections`, `remove_collection`).

### `src/core/ai_security/` - Model Armor, Content Safety & Guardrails
- **`armor.py`**: Regional Google Cloud Model Armor + Cloud Data Loss Prevention (SDP) screening for user prompts (`screen_user_prompt`), retrieved document chunks (`sanitize_chunk_text`, `sanitize_retrieved_documents`), and final LLM responses (`sanitize_model_output`) with fail-closed behavior in Cloud Run.
- **`safety.py`**: Vertex AI Gemini `HarmBlockThreshold` safety configuration (`get_gemini_safety_settings`), `finish_reason="SAFETY"` refusal detection (`is_safety_blocked`), and standardized user-safe refusal/error messages.
- **`guardrails.py`**: Server-enforced LLM and embedding model allowlists (`validate_llm_model`, `validate_embedding_model`), collection and tool identifier sanitization (`validate_collection_name`, `safe_tool_name`), query/upload/chunk ceilings (`validate_query_text`, `validate_file_count`, `validate_chunk_count`), PDF file-signature and upload path guardrails (`sanitize_filename`, `validate_pdf_bytes`, `validate_server_pdf_paths`), and XML spotlighting for retrieved chunks (`EMPTY_RETRIEVAL_MARKER`, `format_retrieved_chunks`).

### `src/core/compliance/` - Data Residency, Audit Logging, Provenance & DSAR
- **`residency.py`**: Enforces single-region GCP locations (`validate_gcp_region`), rejecting `global` and multi-region specifiers (`us`, `eu`, `asia`, `nam4`, `eur4`) before any Vertex AI or Model Armor client call.
- **`state.py`**: Manages per-request compliance guardrail and retrieval state (`GUARDRAIL_FILTER_LABELS`, `mark_pii_filtered`, `mark_model_armor_active`, `record_guardrail_action`, `record_retrieved_chunks`) stored on `RequestContext`.
- **`audit.py`**: Emits PII-free structured audit records (`emit_audit_event`) to stdout and Google Cloud Logging (`interactiverag-audit`) and hashes queries via SHA-256 (`hash_query`).
- **`policy.py`**: Enforces per-user sliding-window rate limiters (`enforce_ingest_rate_limit`, `enforce_query_rate_limit`) and collection count quotas (`enforce_collection_quota`).
- **`provenance.py`**: Extracts inline `[source, p.X]` citations (`extract_citations`) and builds EU AI Act Article 50 transparency, citation grounding, and Model Armor provenance metadata (`ComplianceMetadata`, `build_compliance_metadata`).
- **`dsar.py`**: Executes GDPR Article 15 data inventory (`get_user_data_inventory`), Article 17 hard erasure (`purge_all_user_data`) across live + noncurrent versioned GCS blobs and local `/tmp` caches, and dependency-injected conversation memory thread inspection and erasure (`list_user_memory_threads`, `clear_user_memory`).

### `src/core/observability/` - Observability & AI Monitoring
- **`logging.py`**: Enriches `LogRecord` instances from `RequestContext`, scrubs raw queries into `query_sha256` / `query_chars` (`RequestContextLogFilter`), and formats single-line Cloud Logging JSON (`GCPJsonFormatter`).
- **`state.py`**: Manages per-request observability state accumulators (`get_elapsed_latency_ms`, `record_ttft`, `record_token_usage`, `record_retrieval_scores`, `record_tool_call`, `record_span`, `get_stage_latency_ms`) stored on `RequestContext`.
- **`tracing.py`**: Provides `start_span(name, stage=None, **attributes)` context manager for hierarchical per-stage span timing and optional Google Cloud Trace export.
- **`telemetry.py`**: Captures LLM token usage (`record_llm_usage`), retrieval similarity scores (`record_retrieval_metrics`), and publishes per-request AI telemetry records (`emit_telemetry_event`) to `interactiverag-telemetry`.

---

## 2. `src/ingest/` - Dedicated Document Ingestion Service (`ingest-api`, Port 8000)

### Core Responsibilities
`src/ingest/` isolates CPU- and memory-heavy PDF parsing, text splitting, batch embedding, and ChromaDB HNSW indexing into a dedicated Cloud Run service (`ingest-api`) so uploads never block interactive queries or agent chat:
1. **Upload Staging & Validation (`upload_parser.py`)**: Parses JSON or multipart `/ingest` payloads, enforces per-user ingestion rate limits and collection quotas, delegates `%PDF-` magic-byte checks, filename sanitization, server path validation, `MAX_UPLOAD_MB` (413), and `MAX_FILES_PER_REQUEST` (400) to `src/core/ai_security/guardrails.py`, stages uploads inside `/tmp/uploads/{uid}/{request_id}`, and guarantees cleanup.
2. **PDF Loading & Chunking (`splitter.py`)**: Loads PDFs via `PyPDFLoader` and splits pages into overlapping chunks (`chunk_size=1000`, `chunk_overlap=200`) using `RecursiveCharacterTextSplitter`.
3. **Batched Vector Indexing (`service.py`)**: Indexes chunks in batches of `100` inside `user_write_lock(uid)` (`ingest_documents`) and pushes incremental MD5 deltas to GCS.
4. **Dedicated FastAPI Service (`routes.py`, `app.py`, `schemas.py`)**: Exposes **only** `POST /ingest` and `GET /health`.

### API Endpoints Reference (`http://localhost:8000`)

| Method | Path | Content-Type | Description |
| :--- | :--- | :--- | :--- |
| `GET` | `/health` | N/A | Unauthenticated readiness/liveness probe (`ingest-api`). |
| `POST` | `/ingest` | `application/json` or `multipart/form-data` | Ingests PDF files from local paths (dev only) or multipart binary uploads. |

### Usage & Examples

#### Starting the Service
```bash
python -m src.ingest
# Or via uvicorn directly:
uvicorn src.ingest.app:app --host 0.0.0.0 --port 8000 --reload
```

#### Multipart PDF Upload
```bash
curl -X POST "http://localhost:8000/ingest" \
  -F "files=@./data/Stock_Market_Performance_2024.pdf" \
  -F "collection_name=stock_market_vs"
```

---

## 3. `src/manage/` - Collection Management & GDPR Self-Service Package

`src/manage/` encapsulates tenant collection lifecycle management and GDPR Articles 15 & 17 self-service rights, mounted onto `agent-api`:
- **`collections.py`**: Defines `collections_router` (`GET /collections` and `DELETE /collections/{collection_name}`).
- **`compliance.py`**: Defines `compliance_router` (`GET /compliance/my-data` and `DELETE /compliance/my-data`), passing `MEMORY_SAVER` into `get_user_data_inventory` and `purge_all_user_data` in `src/core/compliance/dsar.py`.
- **`routes.py`**: Defines `manage_router`, combining `collections_router` and `compliance_router` so `agent-api` can inspect and erase both ChromaDB collections and in-memory LangGraph sessions in a single atomic request.

---

## 4. `src/query/` - Direct Vector Similarity Search Package

`src/query/` encapsulates direct top-$k$ semantic similarity search against a user's ChromaDB collection, mounted onto `agent-api`:
- **`schemas.py`**: Defines `QueryRequest` (validating collection name, query text, `k`, and embedding model allowlist).
- **`service.py`**: Implements `search_collection(uid, query, collection_name, k, embedding_model)` inside `user_read_lock(uid)` with Cloud DLP `[INFO_TYPE]` chunk masking and retrieval similarity score telemetry.
- **`routes.py`**: Exposes `POST /query` (`query_router`), sharing `agent-api`'s warm `/tmp/chroma_db/{uid}` cache and `_QUERY_RATE_LIMITER`.

---

## 5. `src/agent/` - Unified Interactive Agent, Query & Management Service (`agent-api`, Port 8001)

### Core Responsibilities
`src/agent/` runs the stateful, safety-screened LangGraph RAG workflow and mounts `src/query` + `src/manage` + `src/agent` onto `agent-api`:
1. **XML-Spotlighted Retriever Tool (`tools.py`)**: Queries the user's Chroma collection, applies Cloud DLP `[INFO_TYPE]` chunk redaction, and formats retrieved chunks into `<retrieved_chunk source="..." page="...">` tags via `format_retrieved_chunks` and `safe_tool_name` from `src/core/ai_security/guardrails.py` (`get_retriever_tool`).
2. **Tenant-Isolated Memory (`memory.py`)**: Owns the `MEMORY_SAVER` (`InMemorySaver`) checkpointer and namespaces checkpoint threads under `{uid}:{thread_id}` (`make_user_thread_id`).
3. **Safety-Screened LangGraph StateGraph (`graph.py`)**: Compiles `input_guard` $\rightarrow$ `model_call` $\leftrightarrow$ `tools` $\rightarrow$ `output_guard` with regional `ChatGoogleGenerativeAI`, Gemini safety thresholds, and `[source, p.X]` citation rules.
4. **SSE & Interactive Streaming (`service.py`, `display.py`)**: Streams sanitized message events, tool traces, and EU AI Act Art. 50 `compliance` frames over SSE (`stream_sse`), emits canonical `agent.query` audit/telemetry events (`emit_agent_query_events`), or renders rich terminal panels (`run_interactive`).
5. **Unified Interactive FastAPI Service (`routes.py`, `app.py`, `schemas.py`)**: Defines `agent_router` (`POST /agent`, `POST /agent/stream`) in `routes.py` and mounts `agent_router`, `query_router` (`POST /query`), and `manage_router` (`GET /collections`, `DELETE /collections/{collection_name}`, `GET/DELETE /compliance/my-data`) plus `GET /health` in `app.py`.

### LangGraph Agent Workflow
- **`input_guard` (`graph.py`)**: Screens the latest user turn via Model Armor (`screen_user_prompt`) before any LLM call.
- **`model_call` (`graph.py`)**: Prepends `SYSTEM_PROMPT`, binds the user's retriever tool, and invokes regional Vertex AI Gemini.
- **`tools` (`graph.py`)**: Executes `ToolNode` retrieval against the user's synced ChromaDB snapshot.
- **`output_guard` (`graph.py`)**: Screens final `AIMessage` text via Model Armor + Cloud DLP (`sanitize_model_output`).

### API Endpoints Reference (`http://localhost:8001`)

| Method | Path | Content-Type | Description |
| :--- | :--- | :--- | :--- |
| `GET` | `/health` | N/A | Unauthenticated readiness/liveness probe (`agent-api`). |
| `POST` | `/query` | `application/json` | Performs top-$k$ similarity search with DLP chunk sanitization (`src/query`). |
| `GET` | `/collections` | N/A | Lists the authenticated user's ChromaDB collections and chunk counts (`src/manage`). |
| `DELETE` | `/collections/{collection_name}` | N/A | Deletes a ChromaDB collection from local cache and GCS (`src/manage`). |
| `GET` | `/compliance/my-data` | N/A | Returns GDPR Art. 15 personal data inventory across GCS, Chroma, and memory threads (`src/manage`). |
| `DELETE` | `/compliance/my-data` | N/A | Executes GDPR Art. 17 hard erasure across GCS, `/tmp`, and memory threads (`src/manage`). |
| `POST` | `/agent` | `application/json` | Runs agent query (`stream: false` JSON with `compliance`, or `stream: true` SSE). |
| `POST` | `/agent/stream` | `application/json` | Dedicated Server-Sent Events (`text/event-stream`) endpoint. |

### Streaming & Provenance Events

When streaming via `/agent/stream` (or `/agent` with `"stream": true`), the server emits sanitized message frames followed by a `compliance` frame and `[DONE]`:
```text
data: {"event": "message", "message": {"type": "ai", "content": "", "tool_calls": [{"name": "stock_market_vs_retriever", "args": {"query": "ARM Holdings"}}]}}

data: {"event": "message", "message": {"type": "tool", "name": "stock_market_vs_retriever", "content": "<retrieved_chunk source=\"report.pdf\" page=\"1\">...</retrieved_chunk>"}}

data: {"event": "message", "message": {"type": "ai", "content": "ARM Holdings is a semiconductor design company [report.pdf, p.1]."}}

data: {"event": "compliance", "message": {"type": "compliance", "content": ""}, "compliance": {"ai_generated": true, "model": "gemini-2.5-flash", "region": "us-central1", "grounding_enforced": true, "grounded": true, "citation_count": 1, "retrieved_chunks": 1, "pii_filtered": false, "model_armor_active": true, "filters_checked": ["pi_and_jailbreak", "malicious_uri", "rai", "sdp"], "guardrail_actions": [], "request_id": "req-..."}}

data: [DONE]
```

### Usage & Examples

#### Starting the Service
```bash
python -m src.agent
# Or via uvicorn directly:
uvicorn src.agent.app:app --host 0.0.0.0 --port 8001 --reload
```

#### Vector Store Similarity Query
```bash
curl -X POST "http://localhost:8001/query" \
  -H "Content-Type: application/json" \
  -d '{
    "query": "What are the key financial performance metrics?",
    "collection_name": "stock_market_vs",
    "k": 5
  }'
```

#### Synchronous JSON Agent Request
```bash
curl -X POST "http://localhost:8001/agent" \
  -H "Content-Type: application/json" \
  -d '{
    "query": "What is ARM Holdings?",
    "collection_name": "stock_market_vs",
    "thread_id": "session-123",
    "stream": false
  }'
```

#### Streaming Agent Request (SSE)
```bash
curl -X POST "http://localhost:8001/agent/stream" \
  -H "Content-Type: application/json" \
  -N \
  -d '{
    "query": "Summarize the 2024 stock market performance for tech companies.",
    "collection_name": "stock_market_vs",
    "thread_id": "session-123"
  }'
```

---

## Prerequisites & Environment Setup

1. **Google Cloud ADC**:
   ```bash
   gcloud auth application-default login
   ```
2. **Environment Variables (`.env`)**:
   ```bash
   GOOGLE_CLOUD_PROJECT="interactive-rag"
   GOOGLE_CLOUD_LOCATION="us-central1"  # Must be a concrete single GCP region (never "global")
   ALLOWED_LLM_MODELS="gemini-2.5-flash,gemini-2.5-pro,gemini-2.5-flash-lite"
   ALLOWED_EMBEDDING_MODELS="gemini-embedding-001,text-embedding-005"
   ```
3. **Dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

---

## Directory & Module Reference

```text
src/
├── __init__.py
├── README.md
├── core/
│   ├── __init__.py
│   ├── config.py              # Single source of truth for all env vars, runtime settings, /tmp caps & allowlists
│   ├── context.py             # Shared RequestContext & RequestCorrelationMiddleware
│   ├── auth.py                # Firebase JWT verification & path-safe uid validation
│   ├── logger.py              # ANSI-colored structured logging setup
│   ├── app_factory.py         # Shared FastAPI factory (CORS, RequestCorrelationMiddleware, routers)
│   ├── storage/
│   │   ├── __init__.py
│   │   ├── paths.py           # Per-user /tmp & GCS paths, SQLite WAL & Chroma cache helpers
│   │   ├── gcs_sync.py        # Incremental MD5 delta GCS snapshot pull/push & PDF archiving
│   │   ├── locks.py           # Per-user thread RLocks, distributed GCS locks & /tmp LRU eviction
│   │   └── chroma.py          # Regional Vertex AI embeddings & ChromaDB collection helpers
│   ├── ai_security/
│   │   ├── __init__.py
│   │   ├── armor.py           # Regional Model Armor + Cloud DLP prompt/chunk/output screening
│   │   ├── safety.py          # Gemini HarmBlockThreshold settings & refusal messages
│   │   └── guardrails.py      # Model allowlists, query/upload/chunk ceilings, XML spotlighting & tool-name sanitization
│   ├── compliance/
│   │   ├── __init__.py
│   │   ├── residency.py       # Single-region GCP location enforcement
│   │   ├── state.py           # Per-request compliance guardrail & retrieval state helpers
│   │   ├── audit.py           # PII-free Cloud Logging publisher & SHA-256 query hashing
│   │   ├── policy.py          # Per-user sliding-window rate limiters & collection quotas
│   │   ├── provenance.py      # EU AI Act Art. 50 transparency & citation grounding metadata
│   │   └── dsar.py            # GDPR Art. 15 inventory, Art. 17 versioned hard erasure & memory thread wiping
│   └── observability/
│       ├── __init__.py
│       ├── logging.py         # Structured GCP JSON formatter & request correlation filter
│       ├── state.py           # Per-request latency, TTFT, token, score & span state accumulators
│       ├── telemetry.py       # AI-specific monitoring & interactiverag-telemetry publisher
│       └── tracing.py         # Hierarchical per-stage span tracer & Cloud Trace exporter
├── ingest/                    # Package 1: Dedicated Document Ingestion Service (`ingest-api`)
│   ├── __init__.py
│   ├── __main__.py            # Entrypoint (`python -m src.ingest`)
│   ├── splitter.py            # PyPDFLoader + RecursiveCharacterTextSplitter chunking
│   ├── upload_parser.py       # Multipart/JSON upload staging, quota checks & guardrail delegation
│   ├── schemas.py             # IngestRequest Pydantic model
│   ├── service.py             # Batch Chroma indexing inside distributed GCS write lock
│   ├── routes.py              # FastAPI router (`POST /ingest`)
│   └── app.py                 # Ingestion FastAPI service (`POST /ingest`, `GET /health`)
├── manage/                    # Package 2: Collection Management & GDPR Self-Service (`agent-api`)
│   ├── __init__.py
│   ├── collections.py         # `GET /collections` & `DELETE /collections/{collection_name}`
│   ├── compliance.py          # `GET /compliance/my-data` & `DELETE /compliance/my-data`
│   └── routes.py              # Combined management router builder
├── query/                     # Package 3: Direct Vector Similarity Search (`agent-api`)
│   ├── __init__.py
│   ├── schemas.py             # Validated QueryRequest model
│   ├── service.py             # DLP-sanitized top-k Chroma similarity search
│   └── routes.py              # FastAPI router (`POST /query`)
└── agent/                     # Package 4: Unified Interactive Agent, Query & Management Service (`agent-api`)
    ├── __init__.py
    ├── __main__.py            # Entrypoint (`python -m src.agent`)
    ├── schemas.py             # AgentRequest & AgentState models
    ├── memory.py              # Tenant-namespaced InMemorySaver checkpointer (`MEMORY_SAVER`)
    ├── tools.py               # DLP-masked Chroma retriever tool delegating XML spotlighting to guardrails
    ├── graph.py               # Safety-screened LangGraph StateGraph builder
    ├── service.py             # Agent event runner, SSE formatter & CLI interactive runner
    ├── display.py             # Rich terminal message rendering
    ├── routes.py              # FastAPI router (`POST /agent`, `POST /agent/stream`)
    └── app.py                 # Agent FastAPI service (`agent_router`, `query_router`, `manage_router`)
```
