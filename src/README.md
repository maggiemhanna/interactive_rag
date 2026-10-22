# InteractiveRAG - Source Module Documentation (`/src`)

This directory contains the unified Python backend packages for **InteractiveRAG**, an Agentic Retrieval-Augmented Generation system powered by **LangGraph**, **Google Cloud Vertex AI** (`gemini-3.8-flash` & `gemini-embedding-001`), **ChromaDB**, and **FastAPI**.

---

## 📑 Table of Contents

- [1. `src/core/` - Shared Infrastructure & FastAPI Wiring](#1-srccore---shared-infrastructure--fastapi-wiring)
- [2. `src/ingestion/` - Document Ingestion & Vector Search Service](#2-srcingestion---document-ingestion--vector-search-service)
  - [Core Responsibilities](#core-responsibilities)
  - [Ingestion Pipeline](#ingestion-pipeline)
  - [API Endpoints Reference](#api-endpoints-reference-httplocalhost8000)
  - [Usage & Examples](#usage--examples)
- [3. `src/agent/` - LangGraph Agentic RAG Service](#3-srcagent---langgraph-agentic-rag-service)
  - [Core Responsibilities](#core-responsibilities-1)
  - [LangGraph Agent Workflow](#langgraph-agent-workflow)
  - [API Endpoints Reference](#api-endpoints-reference-httplocalhost8001)
  - [Streaming & Event Output](#streaming--event-output)
  - [Usage & Examples](#usage--examples-1)
- [Prerequisites & Environment Setup](#-prerequisites--environment-setup)
- [Directory & Module Reference](#-directory--module-reference)

---

## 1. `src/core/` - Shared Infrastructure & FastAPI Wiring

`src/core/` centralizes cross-cutting backend logic shared by both Cloud Run services:
- **`config.py`**: Loads `.env` configuration and defines default model names (`gemini-3.8-flash`, `gemini-embedding-001`) and local service ports.
- **`auth.py`**: Verifies Google Identity Platform / Firebase Bearer JWTs, enforces path-safe `uid` validation, and configures CORS origins.
- **`logger.py`**: Configures colored structured logging (`setup_logging`, `format_dict_for_logs`).
- **`storage.py`**: Manages per-user local `/tmp/chroma_db/{uid}` directories, GCS snapshot syncing, LRU `/tmp` cache eviction, and atomic `if_generation_match` distributed write locks.
- **`chroma.py`**: Centralizes `GoogleGenerativeAIEmbeddings`, `Chroma` vectorstore initialization, and collection existence/list/delete helpers.
- **`routes.py`**: Provides `create_app()` (FastAPI + CORS + `/health`) and the shared `collections_router` (`GET /collections`, `DELETE /collections/{collection_name}`).

---

## 2. `src/ingestion/` - Document Ingestion & Vector Search Service

### Core Responsibilities
`src/ingestion/` handles the end-to-end ingestion pipeline:
1. **Upload Staging & Sanitization (`loader.py`)**: Validates `.pdf` uploads inside isolated `/tmp/uploads/{uid}/{request_id}` directories, optionally archives originals to GCS, and cleans up `/tmp` in a `finally` block.
2. **Text Chunking (`loader.py`)**: Splits PDF pages into overlapping text chunks using `RecursiveCharacterTextSplitter`.
3. **Embedding & Vector Storage (`service.py`)**: Uses Vertex AI's `gemini-embedding-001` model to index embeddings into ChromaDB in batches of `100` chunks inside the user's distributed GCS write lock.
4. **Similarity Search & Collection Management (`app.py`, `routes.py`)**: Exposes `/ingest`, `/query`, `/collections`, and `/health`.

### Ingestion Pipeline
```python
split_pdfs(pdf_paths, chunk_size=1000, chunk_overlap=200)
    │
    ▼
index_documents(documents, persist_directory="chroma_db", collection_name="stock_market_vs")
```

- **Chunk Size**: `1000` characters
- **Chunk Overlap**: `200` characters
- **Batch Ingestion**: Chunks are written to ChromaDB in batches of `100` to optimize memory and API payload sizes.

---

### API Endpoints Reference (`http://localhost:8000`)

| Method | Path | Content-Type | Description |
| :--- | :--- | :--- | :--- |
| `GET` | `/health` | N/A | Unauthenticated readiness/liveness probe (`ingestion-api`). |
| `POST` | `/ingest` | `application/json` or `multipart/form-data` | Ingests PDF files from file paths (local dev) or direct binary file uploads. |
| `GET` | `/collections` | N/A | Lists all ChromaDB collections and their document chunk counts. |
| `DELETE` | `/collections/{collection_name}` | N/A | Deletes a ChromaDB vector collection. |
| `POST` | `/query` | `application/json` | Performs similarity search over a collection using top-$k$ retriever. |

---

### Usage & Examples

#### Starting the Service
```bash
# From the repository root
python -m src.ingestion
# Or using uvicorn directly
uvicorn src.ingestion.app:app --host 0.0.0.0 --port 8000 --reload
```

#### Example 1: Ingest Local PDF Files via JSON
```bash
curl -X POST "http://localhost:8000/ingest" \
  -H "Content-Type: application/json" \
  -d '{
    "pdf_paths": ["./data/Stock_Market_Performance_2024.pdf"],
    "collection_name": "stock_market_vs"
  }'
```

#### Example 2: Ingest via Multipart File Upload
```bash
curl -X POST "http://localhost:8000/ingest" \
  -F "files=@/path/to/document.pdf" \
  -F "collection_name=stock_market_vs"
```

#### Example 3: Querying the Vector Store
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
`src/agent/` implements an autonomous RAG agent using **LangGraph** and **Vertex AI**:
1. **Dynamic Tool Creation (`tools.py`)**: Connects to the active ChromaDB vector store collection and creates a Gemini-safe retriever tool.
2. **Tool-Calling Agent (`graph.py`)**: Compiles the LangGraph `StateGraph` (`gemini-3.8-flash` + `ToolNode`) with `InMemorySaver` checkpointing scoped per `{uid}:{thread_id}`.
3. **Real-Time Streaming (`stream.py`)**: Dispatches LangGraph message events in real time over Server-Sent Events (SSE).
4. **Rich Terminal Formatting (`display.py`)**: Uses `rich` to render System, Human, AI, and Tool messages cleanly in terminal output.

---

### LangGraph Agent Workflow

- **`AgentState` (`schemas.py`)**: A `TypedDict` containing `messages: Annotated[Sequence[BaseMessage], add_messages]`.
- **`model_call` Node (`graph.py`)**: Injects the expert system prompt, binds the retriever tool, and invokes `ChatGoogleGenerativeAI`.
- **`tools` Node (`graph.py`)**: An instance of LangGraph `ToolNode` that executes the retrieval query against ChromaDB.
- **`should_continue` Conditional Edge (`graph.py`)**: Evaluates if the LLM emitted tool calls; if so, routes to `tools`, otherwise routes to `END`.

---

### API Endpoints Reference (`http://localhost:8001`)

| Method | Path | Content-Type | Description |
| :--- | :--- | :--- | :--- |
| `GET` | `/health` | N/A | Unauthenticated readiness/liveness probe (`agent-api`). |
| `POST` | `/agent` | `application/json` | Runs agent query. Returns complete JSON answer or streams SSE when `"stream": true`. |
| `POST` | `/agent/stream` | `application/json` | Dedicated Server-Sent Events (SSE) streaming endpoint (`text/event-stream`). |
| `GET` | `/collections` | N/A | Lists ChromaDB collections and chunk counts. |
| `DELETE` | `/collections/{collection_name}` | N/A | Deletes a ChromaDB vector collection. |

---

### Streaming & Event Output

When streaming is enabled (`"stream": true` or using `/agent/stream`), the API yields SSE data chunks in JSON format:
```json
data: {"event": "message", "message": {"type": "ai", "content": "", "tool_calls": [{"name": "stock_market_vs_retriever", "args": {"query": "ARM Holdings"}}]}}

data: {"event": "message", "message": {"type": "tool", "name": "stock_market_vs_retriever", "content": "..."}}

data: {"event": "message", "message": {"type": "ai", "content": "ARM Holdings is a semiconductor and software design company..."}}

data: [DONE]
```

---

### Usage & Examples

#### Starting the Service
```bash
# From the repository root
python -m src.agent
# Or using uvicorn directly
uvicorn src.agent.app:app --host 0.0.0.0 --port 8001 --reload
```

#### Example 1: Standard Synchronous Request
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

#### Example 2: Streaming Request (SSE)
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

#### Example 3: Running in Interactive Python Mode
```python
from src.agent.stream import run_interactive

run_interactive(
    query="How did NVDA perform in 2024?",
    collection_name="stock_market_vs"
)
```

---

## 🔑 Prerequisites & Environment Setup

1. **Google Cloud Vertex AI Authentication**:
   Ensure Google Cloud Application Default Credentials (ADC) are configured:
   ```bash
   gcloud auth application-default login
   ```
   Or set the environment variable:
   ```bash
   export GOOGLE_APPLICATION_CREDENTIALS="/path/to/service-account-key.json"
   ```

2. **Environment Variables (`.env`)**:
   Configure `.env` in the root directory or `src/.env`:
   ```bash
   GOOGLE_CLOUD_PROJECT="interactive-rag"
   GOOGLE_CLOUD_LOCATION="global"  # Optional, defaults to global
   ```

3. **Dependencies**:
   Install all dependencies listed in `requirements.txt`:
   ```bash
   pip install -r requirements.txt
   ```

---

## 📂 Directory & Module Reference

```
src/
├── __init__.py
├── README.md
├── core/
│   ├── __init__.py
│   ├── config.py          # .env loading & model/port defaults
│   ├── auth.py            # Firebase JWT verification & CORS origins
│   ├── logger.py          # Colored logging formatter
│   ├── storage.py         # Per-user GCS sync, /tmp LRU cache & write locks
│   ├── chroma.py          # Vertex AI embeddings & ChromaDB helpers
│   └── routes.py          # Shared FastAPI factory + /health + /collections router
├── ingestion/
│   ├── __init__.py
│   ├── __main__.py        # Entrypoint (`python -m src.ingestion`)
│   ├── app.py             # FastAPI service (/ingest, /query)
│   ├── schemas.py         # IngestRequest, QueryRequest
│   ├── loader.py          # PDF splitting, sanitization & /tmp upload staging
│   └── service.py         # Vectorstore write & similarity search logic
└── agent/
    ├── __init__.py
    ├── __main__.py        # Entrypoint (`python -m src.agent`)
    ├── app.py             # FastAPI service (/agent, /agent/stream)
    ├── schemas.py         # AgentRequest, AgentState
    ├── tools.py           # Chroma retriever tool builder
    ├── graph.py           # LangGraph StateGraph + InMemorySaver
    ├── stream.py          # Event streaming & SSE formatting
    └── display.py         # Rich terminal formatting for LangGraph messages
```
