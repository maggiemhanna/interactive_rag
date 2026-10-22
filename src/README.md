# InteractiveRAG - Source Module Documentation (`/src`)

This directory contains the backend core services for **InteractiveRAG**, an Agentic Retrieval-Augmented Generation system powered by **LangGraph**, **Google Cloud Vertex AI** (`gemini-2.5-flash` & `gemini-embedding-001`), **ChromaDB**, and **FastAPI**.

---

## 📑 Table of Contents

- [1. `add_documents.py` - Document Ingestion & Storage](#1-add_documentspy---document-ingestion--storage)
  - [Core Responsibilities](#core-responsibilities)
  - [Ingestion Pipeline](#ingestion-pipeline)
  - [API Endpoints Reference](#api-endpoints-reference)
  - [Usage & Examples](#usage--examples)
- [2. `agentic_rag.py` - LangGraph Agentic RAG Service](#2-agentic_ragpy---langgraph-agentic-rag-service)
  - [Core Responsibilities](#core-responsibilities-1)
  - [LangGraph Agent Workflow](#langgraph-agent-workflow)
  - [API Endpoints Reference](#api-endpoints-reference-1)
  - [Streaming & Event Output](#streaming--event-output)
  - [Usage & Examples](#usage--examples-1)
- [Prerequisites & Environment Setup](#-prerequisites--environment-setup)
- [Directory & Data Flow Reference](#-directory--data-flow-reference)

---

## 1. `add_documents.py` - Document Ingestion & Storage

### Core Responsibilities
`add_documents.py` handles the end-to-end ingestion pipeline:
1. **Document Loading**: Reads PDF documents from local file paths or web uploads.
2. **Text Chunking**: Splits document pages into overlapping text chunks using `RecursiveCharacterTextSplitter`.
3. **Embedding Generation**: Uses Google Vertex AI's `gemini-embedding-001` model to create dense vector representations.
4. **Vector Storage**: Indexes and persists embeddings in a local `Chroma` database collection (with batched ingestion of 100 chunks).
5. **Collection Management & Testing**: Provides endpoints to list, inspect, delete collections, and test similarity retrieval.

### Ingestion Pipeline
```python
load_and_split_pdfs(pdf_paths, chunk_size=1000, chunk_overlap=200)
    │
    ▼
add_documents_to_vectorstore(documents, persist_directory="chroma_db", collection_name="stock_market_vs")
```

- **Chunk Size**: `1000` characters
- **Chunk Overlap**: `200` characters
- **Batch Ingestion**: Chunks are written to ChromaDB in batches of `100` to optimize memory and API payload sizes.
- **Upload Directory**: Uploaded files via form-data are saved to `data/uploads/`.

---

### API Endpoints Reference (`http://localhost:8000`)

| Method | Path | Content-Type | Description |
| :--- | :--- | :--- | :--- |
| `POST` | `/add_documents` | `application/json` or `multipart/form-data` | Ingests PDF files from file paths or direct binary file uploads. |
| `GET` | `/collections` | N/A | Lists all ChromaDB collections and their document chunk counts. |
| `DELETE` | `/collections/{collection_name}` | N/A | Deletes a ChromaDB vector collection. |
| `POST` | `/query` | `application/json` | Performs similarity search over a collection using top-$k$ retriever. |

---

### Usage & Examples

#### Starting the Service
```bash
# From the repository root
python -m src.add_documents
# Or using uvicorn directly
uvicorn src.add_documents:app --host 0.0.0.0 --port 8000 --reload
```

#### Example 1: Ingest Local PDF Files via JSON
```bash
curl -X POST "http://localhost:8000/add_documents" \
  -H "Content-Type: application/json" \
  -d '{
    "pdf_paths": ["./data/Stock_Market_Performance_2024.pdf"],
    "collection_name": "stock_market_vs"
  }'
```

#### Example 2: Ingest via Multipart File Upload
```bash
curl -X POST "http://localhost:8000/add_documents" \
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

## 2. `agentic_rag.py` - LangGraph Agentic RAG Service

### Core Responsibilities
`agentic_rag.py` implements an autonomous RAG agent using **LangGraph** and **Vertex AI**:
1. **Dynamic Tool Creation**: Connects to the active ChromaDB vector store collection and creates a retriever tool.
2. **Tool-Calling Agent**: The LLM (`gemini-2.5-flash`) inspects the user question, determines whether to retrieve context from the vector database, inspects the retrieved chunks, and synthesizes answers.
3. **Multi-Turn State & Checkpointing**: Maintains conversation history through `InMemorySaver` keyed by `thread_id`.
4. **Real-Time Streaming**: Dispatches message events in real-time over Server-Sent Events (SSE).
5. **Rich Terminal Formatting**: Uses `rich` and `termcolor` (via `utils/messages_pretty_print.py`) to format System, Human, AI, and Tool messages cleanly in CLI logs.

---

### LangGraph Agent Workflow

- **`AgentState`**: A `TypedDict` containing `messages: Annotated[Sequence[BaseMessage], add_messages]`.
- **`model_call` Node**: Injects the expert system prompt, binds the retriever tool, and invokes `ChatGoogleGenerativeAI`.
- **`tools` Node**: An instance of LangGraph `ToolNode` that executes the retrieval query against ChromaDB.
- **`should_continue` Conditional Edge**: Evaluates if the LLM emitted tool calls; if so, routes to `tools`, otherwise routes to `END`.

---

### API Endpoints Reference (`http://localhost:8001`)

| Method | Path | Content-Type | Description |
| :--- | :--- | :--- | :--- |
| `POST` | `/agentic_rag` or `/agent` | `application/json` | Runs agent query. Returns complete JSON answer or streams SSE when `"stream": true`. |
| `POST` | `/agentic_rag/stream` | `application/json` | Dedicated Server-Sent Events (SSE) streaming endpoint (`text/event-stream`). |
| `GET` | `/collections` | N/A | Lists ChromaDB collections and chunk counts. |
| `DELETE` | `/collections/{collection_name}` | N/A | Deletes a ChromaDB vector collection. |

---

### Streaming & Event Output

When streaming is enabled (`"stream": true` or using `/agentic_rag/stream`), the API yields SSE data chunks in JSON format:
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
python -m src.agentic_rag
# Or specifying a custom port
PORT=8001 python -m src.agentic_rag
```

#### Example 1: Standard Synchronous Request
```bash
curl -X POST "http://localhost:8001/agentic_rag" \
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
curl -X POST "http://localhost:8001/agentic_rag/stream" \
  -H "Content-Type: application/json" \
  -N \
  -d '{
    "query": "Summarize the 2024 stock market performance for tech companies.",
    "collection_name": "stock_market_vs",
    "thread_id": "session-123"
  }'
```

#### Example 3: Running in Interactive CLI Mode
You can also execute the script directly or invoke `run_agent_interactive` in Python:
```python
from src.agentic_rag import run_agent_interactive

run_agent_interactive(
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
   GOOGLE_CLOUD_LOCATION="us-central1"  # Optional, defaults to us-central1
   ```

3. **Dependencies**:
   Install all dependencies listed in `requirements.txt`:
   ```bash
   pip install -r requirements.txt
   ```

---

## 📂 Directory & Data Flow Reference

```
InteractiveRAG/
├── chroma_db/                     # Persistent Chroma vector store files
├── data/
│   ├── uploads/                   # Uploaded PDF files via API
│   └── *.pdf                      # Default dataset PDFs
├── src/
│   ├── __init__.py
│   ├── add_documents.py           # Document Ingestion API (Port 8000)
│   ├── agentic_rag.py             # LangGraph Agentic RAG API (Port 8001)
│   └── README.md                  # This documentation
├── utils/
│   ├── logging.py                 # Colored logging formatter
│   └── messages_pretty_print.py   # Rich console formatting for LangGraph messages
└── tests/
    ├── add_documents.py           # Integration tests for document ingestion
    └── agentic_rag.py             # Integration tests for Agentic RAG endpoints
```
