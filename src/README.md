# InteractiveRAG - Source Module Documentation (`/src`)

This directory contains the Python backend packages for **InteractiveRAG**, a multi-tenant, compliance-aware Agentic Retrieval-Augmented Generation platform powered by **LangGraph**, **Google Cloud Vertex AI** (`gemini-2.5-flash`, `gemini-2.5-pro`, `gemini-2.5-flash-lite`, `gemini-embedding-001`, `text-embedding-005`), **Google Cloud Model Armor + Cloud DLP**, **ChromaDB**, **Google Cloud Storage**, and **FastAPI**.

---

## Table of Contents

- [1. `src/core/` - Shared Platform, Storage, AI Security & Compliance](#1-srccore---shared-platform-storage-ai-security--compliance)
  - [Core Infrastructure](#core-infrastructure)
  - [`src/core/storage/` - Multi-Tenant Persistence & Distributed Locking](#srccorestorage---multi-tenant-persistence--distributed-locking)
  - [`src/core/ai_security/` - Model Armor, Content Safety & Guardrails](#srccoreai_security---model-armor-content-safety--guardrails)
  - [`src/core/compliance/` - Data Residency, Audit Logging, Provenance & DSAR](#srccorecompliance---data-residency-audit-logging-provenance--dsar)
- [2. `src/ingestion/` - Document Ingestion & Vector Search Service](#2-srcingestion---document-ingestion--vector-search-service)
  - [Core Responsibilities](#core-responsibilities)
  - [API Endpoints Reference (`http://localhost:8000`)](#api-endpoints-reference-httplocalhost8000)
  - [Usage & Examples](#usage--examples)
- [3. `src/agent/` - LangGraph Agentic RAG Service](#3-srcagent---langgraph-agentic-rag-service)
  - [Core Responsibilities](#core-responsibilities-1)
  - [LangGraph Agent Workflow](#langgraph-agent-workflow)
  - [API Endpoints Reference (`http://localhost:8001`)](#api-endpoints-reference-httplocalhost8001)
  - [Streaming & Provenance Events](#streaming--provenance-events)
  - [Usage & Examples](#usage--examples-1)
- [Prerequisites & Environment Setup](#prerequisites--environment-setup)
- [Directory & Module Reference](#directory--module-reference)

---

## 1. `src/core/` - Shared Platform, Storage, AI Security & Compliance

`src/core/` centralizes cross-cutting platform infrastructure and domain packages shared by both Cloud Run services (`ingestion-api` and `agent-api`).

### Core Infrastructure
- **`config.py`**: Loads `.env` settings and exposes runtime environment helpers (`is_cloud_run`, `get_allowed_origins`, `get_google_cloud_location`, `get_model_armor_location`, `get_allowed_llm_models`, `get_allowed_embedding_models`) plus local service ports.
- **`context.py`**: Manages per-request `contextvars` correlation state (`RequestContext`, `RequestCorrelationMiddleware`) tracking sanitized `X-Request-Id`, Cloud Trace ID, active span ID, authenticated `uid`, and a thread-safe `data` store for domain modules.
- **`auth.py`**: Verifies Google Identity Platform / Firebase Bearer JWTs (`verify_bearer_token`, `get_current_user`, `get_current_user_info`), enforces path-safe `uid` validation (`validate_uid`), and blocks `AUTH_DISABLED` bypasses when running on Cloud Run.
- **`logger.py`**: Configures structured ANSI-colored console logging (`setup_logging`, `format_dict_for_logs`).
- **`app_factory.py`**: Provides `create_app()` to wire CORS middleware, `RequestCorrelationMiddleware`, `/health` readiness probes, `/collections` routes, and `/compliance/my-data` self-service DSAR routes.

### `src/core/storage/` - Multi-Tenant Persistence & Distributed Locking
- **`paths.py`**: Isolates per-user local directories (`/tmp/chroma_db/{uid}`, `/tmp/uploads/{uid}`), constructs GCS object prefixes, checkpoints SQLite WAL files, and evicts cached Chroma `SharedSystemClient` registries (`release_client_cache`).
- **`gcs_sync.py`**: Pulls and pushes per-user ChromaDB snapshots between `/tmp` and `gs://<bucket>/users/{uid}/chroma_db/` with `version.json` invalidation, and archives raw uploaded PDFs (`archive_upload`).
- **`locks.py`**: Coordinates per-user `threading.RLock` instances, distributed `if_generation_match` GCS write locks with TTL stale-lock recovery (`user_write_lock`, `user_read_lock`), and LRU `/tmp` cache eviction (`evict_tmp_cache`).
- **`chroma.py`**: Instantiates regional Vertex AI `GoogleGenerativeAIEmbeddings` and manages per-user Chroma collections (`get_vectorstore`, `require_collection`, `list_collections`, `remove_collection`).
- **`routes.py`**: Shared FastAPI `collections_router` (`GET /collections`, `DELETE /collections/{collection_name}`).

### `src/core/ai_security/` - Model Armor, Content Safety & Guardrails
- **`armor.py`**: Regional Google Cloud Model Armor + Cloud Data Loss Prevention (SDP) screening for user prompts (`screen_user_prompt`), retrieved document chunks (`sanitize_chunk_text`, `sanitize_retrieved_documents`), and final LLM responses (`sanitize_model_output`) with fail-closed behavior in Cloud Run.
- **`safety.py`**: Vertex AI Gemini `HarmBlockThreshold` safety configuration (`get_gemini_safety_settings`), `finish_reason="SAFETY"` refusal detection (`is_safety_blocked`), and standardized user-safe refusal/error messages.
- **`guardrails.py`**: Server-enforced LLM and embedding model allowlists (`validate_llm_model`, `validate_embedding_model`), collection identifier sanitization (`validate_collection_name`), query/upload/chunk ceilings (`validate_query_text`, `validate_file_count`, `validate_chunk_count`), and bounded LangGraph tool-loop recursion limits (`get_bounded_recursion_limit`).

### `src/core/compliance/` - Data Residency, Audit Logging, Provenance & DSAR
- **`residency.py`**: Enforces single-region GCP locations (`validate_gcp_region`), rejecting `global` and multi-region specifiers (`us`, `eu`, `asia`, `nam4`, `eur4`) before any Vertex AI or Model Armor client call.
- **`state.py`**: Manages per-request compliance guardrail and retrieval state (`GUARDRAIL_FILTER_LABELS`, `mark_pii_filtered`, `mark_model_armor_active`, `record_guardrail_action`, `record_retrieved_chunks`) stored on `RequestContext`.
- **`audit.py`**: Emits PII-free structured audit records (`emit_audit_event`) to stdout and Google Cloud Logging (`interactiverag-audit`) and hashes queries via SHA-256 (`hash_query`).
- **`policy.py`**: Enforces per-user sliding-window rate limiters (`enforce_ingest_rate_limit`, `enforce_query_rate_limit`) and collection count quotas (`enforce_collection_quota`).
- **`provenance.py`**: Builds EU AI Act Article 50 transparency, citation grounding, and Model Armor provenance metadata (`ComplianceMetadata`, `build_compliance_metadata`).
- **`dsar.py`**: Executes GDPR Article 15 data inventory (`get_user_data_inventory`) and Article 17 hard erasure (`purge_all_user_data`) across live + noncurrent versioned GCS blobs, local `/tmp` caches, and LangGraph memory checkpoints.
- **`routes.py`**: Builds the self-service `/compliance/my-data` FastAPI router (`build_compliance_router`).

### `src/core/observability/` - Observability & AI Monitoring
- **`logging.py`**: Enriches `LogRecord` instances from `RequestContext`, scrubs raw queries into `query_sha256` / `query_chars` (`RequestContextLogFilter`), and formats single-line Cloud Logging JSON (`GCPJsonFormatter`).
- **`state.py`**: Manages per-request observability state accumulators (`get_elapsed_latency_ms`, `record_ttft`, `record_token_usage`, `record_retrieval_scores`, `record_tool_call`, `record_span`, `get_stage_latency_ms`) stored on `RequestContext`.
- **`tracing.py`**: Provides `start_span(name, stage=None, **attributes)` context manager for hierarchical per-stage span timing and optional Google Cloud Trace export.

---

## 2. `src/ingestion/` - Document Ingestion & Vector Search Service

### Core Responsibilities
`src/ingestion/` handles PDF validation, chunking, embedding indexing, and direct similarity search:
1. **Upload Staging & Validation (`upload_parser.py`)**: Parses JSON or multipart `/ingest` payloads, enforces `%PDF-` magic-byte checks, `MAX_UPLOAD_MB` (413), `MAX_FILES_PER_REQUEST` (400), and `MAX_CHUNKS_PER_INGEST` (400), stages uploads inside `/tmp/uploads/{uid}/{request_id}`, and guarantees cleanup in `app.py`.
2. **PDF Loading & Chunking (`splitter.py`)**: Loads PDFs via `PyPDFLoader` and splits pages into overlapping chunks (`chunk_size=1000`, `chunk_overlap=200`) using `RecursiveCharacterTextSplitter`.
3. **Vector Indexing & DLP-Sanitized Search (`service.py`)**: Indexes chunks in batches of `100` inside `user_write_lock(uid)` (`ingest_documents`) and runs top-$k$ similarity search inside `user_read_lock(uid)` with Cloud DLP `[INFO_TYPE]` chunk masking (`search_collection`).
4. **FastAPI Service (`app.py`, `schemas.py`)**: Exposes `/ingest`, `/query`, `/collections`, `/compliance/my-data`, and `/health`.

### API Endpoints Reference (`http://localhost:8000`)

| Method | Path | Content-Type | Description |
| :--- | :--- | :--- | :--- |
| `GET` | `/health` | N/A | Unauthenticated readiness/liveness probe (`ingestion-api`). |
| `POST` | `/ingest` | `application/json` or `multipart/form-data` | Ingests PDF files from local paths (dev only) or multipart binary uploads. |
| `POST` | `/query` | `application/json` | Performs top-$k$ similarity search with DLP chunk sanitization. |
| `GET` | `/collections` | N/A | Lists the authenticated user's ChromaDB collections and passage counts. |
| `DELETE` | `/collections/{collection_name}` | N/A | Deletes a ChromaDB collection from local cache and GCS. |
| `GET` | `/compliance/my-data` | N/A | Returns GDPR Art. 15 personal data inventory for the caller. |
| `DELETE` | `/compliance/my-data` | N/A | Executes GDPR Art. 17 complete versioned erasure for the caller. |

### Usage & Examples

#### Starting the Service
```bash
python -m src.ingestion
# Or via uvicorn directly:
uvicorn src.ingestion.app:app --host 0.0.0.0 --port 8000 --reload
```

#### Multipart PDF Upload
```bash
curl -X POST "http://localhost:8000/ingest" \
  -F "files=@./data/Stock_Market_Performance_2024.pdf" \
  -F "collection_name=stock_market_vs"
```

#### Vector Store Similarity Query
```bash
curl -X POST "http://localhost:8000/query" \
  -H "Content-Type: application/json" \
  -d '{
    "query": "What are the key financial performance metrics?",
    "collection_name": "stock_market_vs",
    "k": 5
  }'
```

---

## 3. `src/agent/` - LangGraph Agentic RAG Service

### Core Responsibilities
`src/agent/` runs the stateful, safety-screened LangGraph RAG workflow:
1. **XML-Spotlighted Retriever Tool (`tools.py`)**: Queries the user's Chroma collection, applies Cloud DLP `[INFO_TYPE]` chunk redaction, and wraps retrieved passages in `<retrieved_chunk source="..." page="...">` tags (`format_retrieved_chunks`, `get_retriever_tool`).
2. **Tenant-Isolated Memory (`memory.py`)**: Namespaces `InMemorySaver` checkpoint threads under `{uid}:{thread_id}` and supports GDPR Art. 17 thread purging (`purge_user_memory_threads`).
3. **Safety-Screened LangGraph StateGraph (`graph.py`)**: Compiles `input_guard` $\rightarrow$ `llm` $\rightarrow$ `tools` $\rightarrow$ `output_guard` with regional `ChatGoogleGenerativeAI`, Gemini safety thresholds, and `[source, p.X]` citation rules.
4. **SSE & Interactive Streaming (`stream.py`, `display.py`)**: Streams sanitized message events, tool traces, and EU AI Act Art. 50 `compliance` frames over SSE (`format_sse_events`) or rich terminal panels (`run_interactive`).
5. **FastAPI Service (`app.py`, `schemas.py`)**: Exposes `/agent`, `/agent/stream`, `/collections`, `/compliance/my-data`, and `/health`.

### LangGraph Agent Workflow
- **`input_guard` (`graph.py`)**: Screens the latest user turn via Model Armor (`screen_user_prompt`) before any LLM call.
- **`llm` (`graph.py`)**: Prepends `SYSTEM_PROMPT`, binds the user's retriever tool, and invokes regional Vertex AI Gemini.
- **`tools` (`graph.py`)**: Executes `ToolNode` retrieval against the user's synced ChromaDB snapshot.
- **`output_guard` (`graph.py`)**: Screens final `AIMessage` text via Model Armor + Cloud DLP (`sanitize_model_output`).

### API Endpoints Reference (`http://localhost:8001`)

| Method | Path | Content-Type | Description |
| :--- | :--- | :--- | :--- |
| `GET` | `/health` | N/A | Unauthenticated readiness/liveness probe (`agent-api`). |
| `POST` | `/agent` | `application/json` | Runs agent query (`stream: false` JSON with `compliance`, or `stream: true` SSE). |
| `POST` | `/agent/stream` | `application/json` | Dedicated Server-Sent Events (`text/event-stream`) endpoint. |
| `GET` | `/collections` | N/A | Lists the authenticated user's ChromaDB collections and passage counts. |
| `DELETE` | `/collections/{collection_name}` | N/A | Deletes a ChromaDB collection from local cache and GCS. |
| `GET` | `/compliance/my-data` | N/A | Returns GDPR Art. 15 personal data inventory (including active memory threads). |
| `DELETE` | `/compliance/my-data` | N/A | Executes GDPR Art. 17 hard erasure across GCS, `/tmp`, and memory threads. |

### Streaming & Provenance Events

When streaming via `/agent/stream` (or `/agent` with `"stream": true`), the server emits sanitized message frames followed by a `compliance` frame and `[DONE]`:
```text
data: {"event": "message", "message": {"type": "ai", "content": "", "tool_calls": [{"name": "stock_market_vs_retriever", "args": {"query": "ARM Holdings"}}]}}

data: {"event": "message", "message": {"type": "tool", "name": "stock_market_vs_retriever", "content": "<retrieved_chunk source=\"report.pdf\" page=\"1\">...</retrieved_chunk>"}}

data: {"event": "message", "message": {"type": "ai", "content": "ARM Holdings is a semiconductor design company [report.pdf, p.1]."}}

data: {"event": "compliance", "message": {"type": "compliance", "content": ""}, "compliance": {"ai_generated": true, "model": "gemini-2.5-flash", "region": "us-central1", "grounding_enforced": true, "grounded": true, "citation_count": 1, "retrieved_passages": 1, "pii_filtered": false, "model_armor_active": true, "filters_checked": ["pi_and_jailbreak", "malicious_uri", "rai", "sdp"], "guardrail_actions": [], "request_id": "req-..."}}

data: [DONE]
```

### Usage & Examples

#### Starting the Service
```bash
python -m src.agent
# Or via uvicorn directly:
uvicorn src.agent.app:app --host 0.0.0.0 --port 8001 --reload
```

#### Synchronous JSON Request
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

#### Streaming Request (SSE)
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
│   ├── config.py              # .env loading, runtime environment & allowlist getters
│   ├── context.py             # Shared RequestContext & RequestCorrelationMiddleware
│   ├── auth.py                # Firebase JWT verification & path-safe uid validation
│   ├── logger.py              # ANSI-colored structured logging setup
│   ├── app_factory.py         # Shared FastAPI factory (CORS, RequestCorrelationMiddleware, routers)
│   ├── storage/
│   │   ├── __init__.py
│   │   ├── paths.py           # Per-user /tmp & GCS paths, SQLite WAL & Chroma cache helpers
│   │   ├── gcs_sync.py        # GCS snapshot pull/push & PDF upload archiving
│   │   ├── locks.py           # Per-user thread RLocks, distributed GCS locks & /tmp LRU eviction
│   │   ├── chroma.py          # Regional Vertex AI embeddings & ChromaDB collection helpers
│   │   └── routes.py          # Shared /collections FastAPI router
│   ├── ai_security/
│   │   ├── __init__.py
│   │   ├── armor.py           # Regional Model Armor + Cloud DLP prompt/chunk/output screening
│   │   ├── safety.py          # Gemini HarmBlockThreshold settings & refusal messages
│   │   └── guardrails.py      # Model allowlists, query/upload/chunk ceilings & recursion bounds
│   ├── compliance/
│   │   ├── __init__.py
│   │   ├── residency.py       # Single-region GCP location enforcement
│   │   ├── state.py           # Per-request compliance guardrail & retrieval state helpers
│   │   ├── audit.py           # PII-free Cloud Logging publisher & SHA-256 query hashing
│   │   ├── policy.py          # Per-user sliding-window rate limiters & collection quotas
│   │   ├── provenance.py      # EU AI Act Art. 50 transparency & citation grounding metadata
│   │   ├── dsar.py            # GDPR Art. 15 inventory & Art. 17 versioned hard erasure
│   │   └── routes.py          # Self-service GET/DELETE /compliance/my-data router
│   └── observability/
│       ├── __init__.py
│       ├── logging.py         # Structured GCP JSON formatter & request correlation filter
│       ├── state.py           # Per-request latency, TTFT, token, score & span state accumulators
│       └── tracing.py         # Hierarchical per-stage span tracer & Cloud Trace exporter
├── ingestion/
│   ├── __init__.py
│   ├── __main__.py            # Entrypoint (`python -m src.ingestion`)
│   ├── splitter.py            # PyPDFLoader + RecursiveCharacterTextSplitter chunking
│   ├── upload_parser.py       # Multipart/JSON upload staging, %PDF- & ceiling validation
│   ├── schemas.py             # IngestRequest & QueryRequest Pydantic models
│   ├── service.py             # Batch Chroma indexing & DLP-sanitized similarity search
│   └── app.py                 # Ingestion FastAPI service (/ingest, /query)
└── agent/
    ├── __init__.py
    ├── __main__.py            # Entrypoint (`python -m src.agent`)
    ├── schemas.py             # AgentRequest & AgentState models
    ├── memory.py              # Tenant-namespaced InMemorySaver checkpoint management
    ├── tools.py               # XML-spotlighted, DLP-masked Chroma retriever tool
    ├── graph.py               # Safety-screened LangGraph StateGraph builder
    ├── stream.py              # Agent event runner, SSE formatter & CLI interactive runner
    ├── display.py             # Rich terminal message rendering
    └── app.py                 # Agent FastAPI service (/agent, /agent/stream)
```
