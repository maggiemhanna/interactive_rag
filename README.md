# InteractiveRAG

**InteractiveRAG** is designed for multi-domain document intelligence, allowing users to organize different sets of PDF documents into isolated, dedicated Chroma vector store collections rather than pooling everything into a single shared index. Through either the React dashboard or the API endpoint (`POST /ingest`), users can create a new collection simply by supplying a custom collection name along with their PDF files (e.g., separate collections for market analysis vs. annual reports). The backend parses the files,  computes dense embeddings using, and indexes the chunks directly into the designated collection. Users can also monitor all active collections and chunk counts or completely remove obsolete collections.

When it comes to querying, users can select a specific collection to keep retrieval strictly scoped to that exact document set, avoiding cross-domain noise. Users can perform direct top-$k$ semantic similarity searches on an indexed collection through (`POST /query`) to inspect raw matching text passages and metadata. Alternatively, users can interact with the LangGraph conversational assistant via (`POST /agent`), where we dynamically configure an autonomous retrieval tool bound exclusively to the target collection, enabling the Gemini agent to search, inspect relevant context chunks, and synthesize grounded answers in real time.

**InteractiveRAG** is an interactive, full-stack Agentic Retrieval-Augmented Generation (RAG) platform. It pairs a **LangGraph**-driven reasoning agent powered by **Google Cloud Vertex AI** (`gemini-3.8-flash` & `gemini-embedding-001`) and **ChromaDB** with a modern **React** web dashboard.

---

## 📁 Directory Structure & Components

```
InteractiveRAG/
├── src/                        # Unified Python backend packages
│   ├── core/                   # Shared auth, config, logger, GCS storage, Chroma & routes
│   ├── ingestion/              # Document Ingestion & Vector Search API (Port 8000)
│   ├── agent/                  # LangGraph Agentic RAG & SSE Streaming API (Port 8001)
│   └── README.md               # Detailed technical documentation for /src
├── frontend/                   # React + Vite web user interface (Port 3000)
│   ├── src/                    # UI components (App.jsx, index.css)
│   └── package.json            # Frontend scripts and dependencies
├── infra/                      # Terraform GCP Cloud Run, VPC, Load Balancer & IAM configs
├── docker/                     # Backend & Frontend container definitions
├── tests/                      # Unit, multi-tenant, and endpoint test suites
│   ├── auth.py                 # Firebase JWT & local auth bypass unit tests
│   ├── storage.py              # Per-user GCS sync & distributed lock unit tests
│   ├── multi_tenant_api.py     # End-to-end multi-tenant API isolation tests
│   ├── ingestion.py            # Live ingestion & Chroma retrieval smoke test
│   └── agent.py                # Live standard & SSE agent endpoint smoke test
├── data/                       # Local data storage & sample PDFs
├── chroma_db/                  # Local persistent Chroma vector store database
├── requirements.txt            # Python dependencies
└── README.md                   # Project documentation
```

### Component Summary

- **`src/ingestion/` (Port 8000)**: FastAPI service (`app.py`, `loader.py`, `service.py`, `schemas.py`) that loads PDFs, chunks text using `RecursiveCharacterTextSplitter`, computes embeddings with `GoogleGenerativeAIEmbeddings` (`gemini-embedding-001`), and indexes them in `ChromaDB` into specified collections. Also provides collection management (`GET /collections`, `DELETE /collections/{name}`) and direct similarity search (`POST /query`).
- **`src/agent/` (Port 8001)**: FastAPI service (`app.py`, `graph.py`, `tools.py`, `stream.py`, `display.py`, `schemas.py`) running a LangGraph `StateGraph` agent with `ChatGoogleGenerativeAI` (`gemini-3.8-flash`). Dynamically binds a Chroma retriever tool, handles multi-turn conversation state with `InMemorySaver`, and streams responses over Server-Sent Events (SSE).
- **`src/core/`**: Shared backend modules (`config.py`, `auth.py`, `logger.py`, `storage.py`, `chroma.py`, `routes.py`) providing Firebase JWT verification, per-user GCS ↔ ChromaDB synchronization, Vertex AI embeddings, and deduplicated FastAPI setup.
- **`frontend/` (Port 3000)**: React dashboard that allows users to upload PDF documents, manage vector store collections, perform semantic search, and chat interactively with the LangGraph agent in real time.
- **`tests/`**: Offline `unittest` suites (`auth.py`, `storage.py`, `multi_tenant_api.py`) and live endpoint smoke scripts (`ingestion.py`, `agent.py`).

---

## 🚀 Setup & Prerequisites

### 1. Google Cloud Authentication & Configuration
Ensure Google Cloud credentials and environment variables are configured:
```bash
gcloud auth application-default login
```
*Or set the service account key path:*
```bash
export GOOGLE_APPLICATION_CREDENTIALS="/path/to/service-account-key.json"
```

Configure your `.env` file in the project root or `src/.env` and `tests/.env`:
```bash
GOOGLE_CLOUD_PROJECT="interactive-rag"
GOOGLE_CLOUD_LOCATION="global"  # Optional, defaults to global
```

### 2. Python Environment Setup
```bash
# Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### 3. Frontend Setup
```bash
cd frontend
npm install
cd ..
```

---

## 🏃 Running the Application

To run the full stack, start each service in a separate terminal:

### Step 1: Start Document Ingestion API (Port 8000)
```bash
python -m src.ingestion
```

### Step 2: Start Agentic RAG API (Port 8001)
```bash
python -m src.agent
```

### Step 3: Start the Frontend UI (Port 3000)
```bash
cd frontend
npm run dev
```
Open **http://localhost:3000** in your browser.

---

## 🧪 How to Test the Application

### 1. Offline Unit & Multi-Tenant Isolation Tests
```bash
python -m unittest tests.auth tests.storage tests.multi_tenant_api -v
```

### 2. Live Endpoint Test Scripts

Ensure both backend services are running on ports 8000 and 8001 before executing the live smoke tests.

#### A. Test Document Ingestion & Vector Retrieval
Runs ingestion of sample PDFs into ChromaDB and verifies similarity search:
```bash
python -m tests.ingestion
```

#### B. Test Agentic RAG (Standard + SSE Streaming)
Tests the LangGraph agent endpoint in both standard JSON mode and real-time Server-Sent Events (SSE) streaming mode with rich console output:
```bash
python -m tests.agent
```

---

### 2. Manual API Testing via cURL

#### Ingest a PDF Document:
```bash
curl -X POST "http://localhost:8000/ingest" \
  -H "Content-Type: application/json" \
  -d '{
    "pdf_paths": ["./data/Stock_Market_Performance_2024.pdf"],
    "collection_name": "stock_market_vs"
  }'
```

#### Test Similarity Query:
```bash
curl -X POST "http://localhost:8000/query" \
  -H "Content-Type: application/json" \
  -d '{
    "query": "What is ARM Holdings?",
    "collection_name": "stock_market_vs",
    "k": 3
  }'
```

#### Query the Agentic RAG Assistant:
```bash
curl -X POST "http://localhost:8001/agent" \
  -H "Content-Type: application/json" \
  -d '{
    "query": "What is ARM Holdings and what was its financial performance in 2024?",
    "collection_name": "stock_market_vs",
    "stream": false
  }'
```

---

### 3. Frontend Interactive Testing

1. Open **http://localhost:3000**.
2. **Upload / Ingest**: Go to the ingestion panel, upload a PDF or select existing files to ingest into a collection.
3. **Inspect Collections**: View indexed collections and chunk counts.
4. **Chat & Reasoning**: Query the agent in the chat interface to watch the real-time reasoning steps, tool calls, retrieved document context, and final generated response.
