# InteractiveRAG

**InteractiveRAG** is an interactive, full-stack Agentic Retrieval-Augmented Generation (RAG) platform. It pairs a **LangGraph**-driven reasoning agent powered by **Google Cloud Vertex AI** (`gemini-2.5-flash` & `gemini-embedding-001`) and **ChromaDB** with a modern **React** web dashboard.

---

## 📁 Directory Structure & Components

```
InteractiveRAG/
├── src/                        # Backend service endpoints and core logic
│   ├── add_documents.py        # Document ingestion, PDF splitting, embedding & ChromaDB API (Port 8000)
│   ├── agentic_rag.py          # LangGraph agent workflow & streaming API (Port 8001)
│   └── README.md               # Detailed technical documentation for /src
├── frontend/                   # React + Vite web user interface (Port 3000)
│   ├── src/                    # UI components (App.jsx, index.css)
│   └── package.json            # Frontend scripts and dependencies
├── tests/                      # Integration and endpoint test scripts
│   ├── add_documents.py        # Tests document ingestion and Chroma retrieval
│   └── agentic_rag.py          # Tests standard and streaming (SSE) agent endpoints
├── utils/                      # Helper modules
│   ├── logging.py              # Colored log formatting configuration
│   └── messages_pretty_print.py# Rich console rendering for agent events
├── data/                       # Local data storage
│   ├── uploads/                # Directory where uploaded PDFs are stored
│   └── *.pdf                   # Sample financial and market report PDFs
├── chroma_db/                  # Local persistent Chroma vector store database
├── requirements.txt            # Python dependencies
└── README.md                   # Project documentation
```

### Component Summary

- **`src/add_documents.py` (Port 8000)**: FastAPI service that loads PDFs, chunks text using `RecursiveCharacterTextSplitter`, computes embeddings with Vertex AI (`gemini-embedding-001`), and indexes them in `ChromaDB`. Also provides collection management (`GET /collections`, `DELETE /collections/{name}`) and direct similarity search (`POST /query`).
- **`src/agentic_rag.py` (Port 8001)**: FastAPI service running a LangGraph `StateGraph` agent with `ChatVertexAI` (`gemini-2.5-flash`). Dynamically binds a Chroma retriever tool, handles multi-turn conversation state with `InMemorySaver`, and streams responses over Server-Sent Events (SSE).
- **`frontend/` (Port 3000)**: React dashboard that allows users to upload PDF documents, manage vector store collections, perform semantic search, and chat interactively with the LangGraph agent in real time.
- **`tests/`**: Integration test scripts to validate backend endpoints and retrieval performance.
- **`utils/`**: Shared logging and rich terminal message visualizer.

---

## 🚀 Setup & Prerequisites

### 1. Google Cloud Authentication
Ensure Google Cloud credentials are configured for Vertex AI:
```bash
gcloud auth application-default login
```
*Or set the service account key path:*
```bash
export GOOGLE_APPLICATION_CREDENTIALS="/path/to/service-account-key.json"
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
python -m src.add_documents
```

### Step 2: Start Agentic RAG API (Port 8001)
```bash
python -m src.agentic_rag
```

### Step 3: Start the Frontend UI (Port 3000)
```bash
cd frontend
npm run dev
```
Open **http://localhost:3000** in your browser.

---

## 🧪 How to Test the Application

### 1. Automated Test Scripts

Ensure both backend services are running on ports 8000 and 8001 before executing the tests.

#### A. Test Document Ingestion & Vector Retrieval
Runs ingestion of sample PDFs into ChromaDB and verifies similarity search:
```bash
python -m tests.add_documents
```

#### B. Test Agentic RAG (Standard + SSE Streaming)
Tests the LangGraph agent endpoint in both standard JSON mode and real-time Server-Sent Events (SSE) streaming mode with rich console output:
```bash
python -m tests.agentic_rag
```

---

### 2. Manual API Testing via cURL

#### Ingest a PDF Document:
```bash
curl -X POST "http://localhost:8000/add_documents" \
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
curl -X POST "http://localhost:8001/agentic_rag" \
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
