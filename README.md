# InteractiveRAG

**InteractiveRAG** is a production-hardened, multi-tenant **Agentic Retrieval-Augmented Generation (RAG)** platform built with **LangGraph**, **Google Cloud Vertex AI**, **ChromaDB**, **FastAPI**, and **React**.

Instead of combining unrelated documents into a single shared database, InteractiveRAG gives every signed-in user an isolated workspace where they can organize PDFs into domain-specific vector collections (for example, separating annual financial reports from clinical research). Through the web dashboard or REST APIs, users can:

- **Ingest PDF Documents (`POST /ingest`)**: Upload PDF documents that are parsed page-by-page, split into overlapping text chunks, converted into vector embeddings using Vertex AI, and indexed into a named vector collection.
- **Run Direct Semantic Search (`POST /query`)**: Perform top-$k$ similarity searches scoped strictly to a selected collection to inspect relevant document passages, similarity scores, and page numbers directly.
- **Chat with a Grounded AI Agent (`POST /agent`, `POST /agent/stream`)**: Ask questions in a multi-turn conversation with a stateful LangGraph agent that dynamically queries the user's chosen collection, grounds every factual statement in retrieved evidence with inline `[filename.pdf, p.X]` citations, and streams each workflow step in real time over Server-Sent Events (SSE).
- **Manage Document Collections (`GET /collections`, `DELETE /collections/{collection_name}`)**: List all vector collections in the user's workspace along with their indexed chunk counts, or delete a collection when it is no longer needed.
- **Exercise Self-Service Privacy Controls (`GET /compliance/my-data`, `DELETE /compliance/my-data`)**: Export a complete inventory of stored collections, uploaded PDFs, storage bytes, and active conversation threads, or permanently erase all personal data—including live and archived Cloud Storage object versions, local vector caches, and conversation memory—in a single action.

---

## 🧠 Core Agentic RAG Architecture & Retrieval Pipeline

InteractiveRAG organizes its backend into **4 modular Python packages** (`src/ingest`, `src/manage`, `src/query`, `src/agent`) deployed across **2 backend services** so heavy document indexing never blocks interactive queries or chat:
- **`ingest-api` (Document Ingestion Service)**: Calls **only** `src/ingest` (`POST /ingest`) to run PDF parsing, text chunking, and batch vector indexing on dedicated compute instances.
- **`agent-api` (Interactive Agent, Search & Management Service)**: Calls **`src/manage`** (`GET /collections`, `DELETE /collections/{collection_name}`, `GET/DELETE /compliance/my-data`), **`src/query`** (`POST /query`), and **`src/agent`** (`POST /agent`, `POST /agent/stream`) to serve collection management, direct similarity search, and multi-turn LangGraph agent conversations.

### 1. PDF Document Parsing, Chunking & Indexing Strategy
- **Multipart PDF Upload Staging:** Uploaded PDF files are staged in a temporary workspace where original filenames are normalized before extraction begins, and temporary files are cleaned up automatically once indexing finishes.  
  — *Implemented in [`src/ingest/upload_parser.py`](src/ingest/upload_parser.py)*  
  — *Called in [`src/ingest/routes.py`](src/ingest/routes.py)*
- **Page-Aware PDF Extraction (`PyPDFLoader`):** Staged PDFs are loaded page-by-page using `PyPDFLoader`, preserving the source document filename (`source`) and page index (`page`) on every extracted page.  
  — *Implemented in [`src/ingest/splitter.py`](src/ingest/splitter.py)*  
  — *Called in [`src/ingest/service.py`](src/ingest/service.py)*
- **Overlapping Recursive Text Chunking (`RecursiveCharacterTextSplitter`):** Extracted pages are split into `1,000`-character passages with a `200`-character sliding overlap along paragraph and sentence boundaries so key facts or tables at chunk boundaries are never cut in half.  
  — *Implemented in [`src/ingest/splitter.py`](src/ingest/splitter.py)*  
  — *Called in [`src/ingest/service.py`](src/ingest/service.py)*
- **Sequential Batched Vector Indexing:** Extracted text chunks are embedded and written into the target ChromaDB collection sequentially in batches of `100` chunks per operation so multi-PDF uploads are indexed reliably.  
  — *Implemented in [`src/ingest/service.py`](src/ingest/service.py)*  
  — *Called in [`src/ingest/routes.py`](src/ingest/routes.py)*

### 2. Embedding Models & Domain-Scoped Vector Databases
- **Vertex AI Dense Vector Embeddings:** Document chunks (at ingestion time) and search queries (at retrieval time) are converted into dense semantic vectors using **Google Cloud Vertex AI** embedding models (`gemini-embedding-001` by default, or `text-embedding-005`).  
  — *Implemented in [`src/core/storage/chroma.py`](src/core/storage/chroma.py)*  
  — *Called in [`src/ingest/service.py`](src/ingest/service.py) & [`src/query/service.py`](src/query/service.py)*
- **Domain-Scoped Named Collections:** Instead of pooling all uploaded files into a single flat vector index (which degrades retrieval accuracy when documents cover different topics), documents are organized into separate named **ChromaDB** collections (for example, separating financial reports from clinical studies) that can be listed or deleted independently.  
  — *Implemented in [`src/core/storage/chroma.py`](src/core/storage/chroma.py)*  
  — *Called in [`src/ingest/service.py`](src/ingest/service.py), [`src/query/service.py`](src/query/service.py) & [`src/manage/collections.py`](src/manage/collections.py)*
- **ChromaDB SQLite Metadata & HNSW Vector Index Storage:** Each collection persists its passage text and page metadata in a SQLite catalog paired with a Hierarchical Navigable Small World (HNSW) approximate nearest-neighbor vector index for fast similarity lookups.  
  — *Implemented in [`src/core/storage/chroma.py`](src/core/storage/chroma.py)*  
  — *Called in [`src/ingest/service.py`](src/ingest/service.py) & [`src/query/service.py`](src/query/service.py)*
- **Collection-Targeted Search & Agent Retrieval:** Every similarity search (`POST /query`) and agent conversation (`POST /agent`, `POST /agent/stream`) targets one explicitly chosen collection at a time to keep retrieval focused and free of cross-topic noise.  
  — *Implemented in [`src/query/service.py`](src/query/service.py) & [`src/agent/tools.py`](src/agent/tools.py)*  
  — *Called in [`src/query/routes.py`](src/query/routes.py), [`src/agent/graph.py`](src/agent/graph.py) & [`src/agent/routes.py`](src/agent/routes.py)*

### 3. Agentic RAG Architecture Pattern (Stateful LangGraph Pipeline)
- **Why Agentic RAG Instead of Fixed Single-Shot Retrieval:** Traditional RAG pipelines run a single similarity search using the user's raw prompt, which often struggles with conversational follow-up questions, multi-part questions, or queries that require rephrasing search terms. InteractiveRAG uses **LangGraph** to orchestrate a stateful, cyclic tool-calling workflow powered by **Vertex AI Gemini** (`gemini-2.5-flash`, `gemini-2.5-pro`, or `gemini-2.5-flash-lite` at `temperature=0` for deterministic, factual responses).  
  — *Implemented in [`src/agent/graph.py`](src/agent/graph.py)*  
  — *Called in [`src/agent/routes.py`](src/agent/routes.py) & [`src/agent/service.py`](src/agent/service.py)*
- **Dynamic Collection-Scoped Retriever Tool:** On each request, the backend builds a structured retriever tool (retrieving the top-$k=5$ most relevant passages per call) bound strictly to the user's selected collection.  
  — *Implemented in [`src/agent/tools.py`](src/agent/tools.py)*  
  — *Called in [`src/agent/graph.py`](src/agent/graph.py)*
- **Iterative Query Formulation & Multi-Step Search:** Gemini autonomously decides when to call the retriever tool, formulates targeted search queries based on the conversation history, inspects the returned passages, and can perform additional search steps with refined queries if the initial passages are insufficient before synthesizing its final answer.  
  — *Implemented in [`src/agent/graph.py`](src/agent/graph.py)*  
  — *Called in [`src/agent/routes.py`](src/agent/routes.py) & [`src/agent/service.py`](src/agent/service.py)*
- **Multi-Turn Conversation Memory (`InMemorySaver`):** Conversation history is persisted across turns using an in-memory LangGraph checkpointer keyed by conversation thread so users can ask natural follow-up questions without repeating earlier context.  
  — *Implemented in [`src/agent/memory.py`](src/agent/memory.py)*  
  — *Called in [`src/agent/graph.py`](src/agent/graph.py), [`src/agent/routes.py`](src/agent/routes.py) & [`src/agent/service.py`](src/agent/service.py)*
- **Real-Time Step-by-Step SSE Event Streaming:** During execution, the streaming engine deduplicates graph state transitions and streams each completed workflow step—retriever tool invocations, retrieved evidence passages, and the final AI answer—in real time over Server-Sent Events (SSE).  
  — *Implemented in [`src/agent/service.py`](src/agent/service.py)*  
  — *Called in [`src/agent/routes.py`](src/agent/routes.py)*

### 4. Document Grounding & Inline Page Citations
- **1-Indexed Page Number Normalization:** PDF parsers number pages starting at `0`, which causes off-by-one confusion when cited directly. Before returning passages to the language model, the retriever tool converts `0`-based page metadata into human-readable `1`-indexed page numbers (`p.1`, `p.2`, ...) and attaches the exact `source` filename and `page` number to each passage.  
  — *Implemented in [`src/agent/tools.py`](src/agent/tools.py)*  
  — *Called in [`src/agent/graph.py`](src/agent/graph.py)*
- **Mandatory Document Grounding:** The system prompt enforces a strict grounding contract requiring the agent to call the retriever tool before answering factual questions and base every factual claim *exclusively* on the retrieved document passages rather than pretraining memory or assumptions.  
  — *Implemented in [`src/agent/graph.py`](src/agent/graph.py)*  
  — *Called in [`src/agent/routes.py`](src/agent/routes.py) & [`src/agent/service.py`](src/agent/service.py)*
- **Explicit Abstention When Evidence Is Missing:** Whenever the retriever returns no matching chunks or the retrieved passages lack sufficient evidence to answer the question, the agent explicitly states that the answer cannot be found in the available documents rather than guessing.  
  — *Implemented in [`src/agent/tools.py`](src/agent/tools.py) & [`src/agent/graph.py`](src/agent/graph.py)*  
  — *Called in [`src/agent/routes.py`](src/agent/routes.py) & [`src/agent/service.py`](src/agent/service.py)*
- **Inline `[source, p.X]` Page Citations:** Every factual statement in the assistant's answer must include an inline citation matching the exact `source` filename and `1`-indexed `page` number of the supporting passage (for example, `[annual_report.pdf, p.4]`) so users can verify every claim against the source PDF.  
  — *Implemented in [`src/agent/tools.py`](src/agent/tools.py) & [`src/agent/graph.py`](src/agent/graph.py)*  
  — *Called in [`src/core/compliance/provenance.py`](src/core/compliance/provenance.py)*

---

## 🏗️ Cloud Architecture & Defense-in-Depth Blueprints

InteractiveRAG is engineered for reproducible deployment on **Google Cloud Platform (GCP)** across six architectural blueprints documented in [`docs/`](docs/):

| Layer | Blueprint Document | Focus Area |
| :--- | :--- | :--- |
| **1. Cloud Deployment & Persistence** | [`docs/GCP_DEPLOYMENT_PLAN.md`](docs/GCP_DEPLOYMENT_PLAN.md) | Serverless Cloud Run microservices, Identity Platform authentication, strict multi-tenant isolation, Cloud Storage vector synchronization, and startup runtime frontend configuration |
| **2. Network Security** | [`docs/NETWORK_SECURITY_PLAN.md`](docs/NETWORK_SECURITY_PLAN.md) | Global External HTTPS Load Balancer (`sslip.io` TLS), locked-down Cloud Run ingress, zero-internet custom VPC, Private Google Access, and Regional Private Service Connect |
| **3. AI-Specific Security** | [`docs/AI_SECURITY_PLAN.md`](docs/AI_SECURITY_PLAN.md) | Prompt injection mitigation (Model Armor & XML spotlighting), model allowlists, grounding & upload guardrails, Cloud DLP redaction, and dual-layer content safety |
| **4. Compliance, Privacy & Governance** | [`docs/COMPLIANCE_GOVERNANCE_PLAN.md`](docs/COMPLIANCE_GOVERNANCE_PLAN.md) | Single-region data residency, end-to-end audit & data-access logging, GDPR self-service rights, EU AI Act provenance transparency, fail-closed screening, and rate/quota enforcement |
| **5. Observability & AI Monitoring** | [`docs/OBSERVABILITY_PLAN.md`](docs/OBSERVABILITY_PLAN.md) | Structured GCP JSON operational logging, Cloud Monitoring log-based metrics & dashboards, distributed per-stage tracing, and AI-specific latency, token & retrieval quality telemetry |
| **6. Scalability & Elasticity** | [`docs/SCALABILITY_ELASTICITY_PLAN.md`](docs/SCALABILITY_ELASTICITY_PLAN.md) | Workload-isolated microservice packaging, horizontal & vertical scaling profiles, session affinity, bounded `/tmp` cache eviction, parallel privacy screening, incremental Cloud Storage delta sync, and scaling saturation alerts |

---

### 1. Cloud Deployment & Multi-Tenant Persistence ([`docs/GCP_DEPLOYMENT_PLAN.md`](docs/GCP_DEPLOYMENT_PLAN.md))

Multiple users can sign in and use the cloud platform simultaneously—each person gets their own isolated document collections and chat history, while serverless containers automatically scale down to zero when idle to save cost.

- **1.1 Serverless Microservice Architecture**
  - **Serverless Cloud Run Services:** The application runs on **Google Cloud Run** across the React web frontend (`frontend`), the Document Ingestion service (`ingest-api`), and the interactive AI Agent, Search & Management service (`agent-api`), scaling on demand and scaling down to zero when idle. Each service is configured with health checks, explicit cross-origin security rules, and HTTP streaming support.  
    — *Provisioned in [`infra/cloud_run.tf`](infra/cloud_run.tf)*  
    — *Implemented in [`src/core/app_factory.py`](src/core/app_factory.py)*  
    — *Called in [`src/ingest/app.py`](src/ingest/app.py) & [`src/agent/app.py`](src/agent/app.py)*
  - **Externalized Runtime & Infrastructure Configuration:** All runtime application settings (approved model catalogs, upload and retrieval guardrails, per-user rate limits, collection quotas, `/tmp` cache caps, and regional endpoints) and cloud infrastructure parameters (project ID, single-region location, VPC subnet CIDRs, per-service scaling/concurrency bounds, and log retention policies) are externalized into environment variables and Terraform input variables rather than hardcoded in business logic.  
    — *Provisioned in [`infra/variables.tf`](infra/variables.tf), [`infra/terraform.tfvars.example`](infra/terraform.tfvars.example) & [`infra/cloud_run.tf`](infra/cloud_run.tf)*  
    — *Implemented in [`src/core/config.py`](src/core/config.py)*
  - **Container Packaging & Cloud Build Pipeline:** A unified Python container image powers both backend services (selecting either the ingestion service or the interactive agent service at startup), while an Nginx container serves the React frontend. Both images are built and pushed to Artifact Registry using **Google Cloud Build**.  
    — *Implemented in [`docker/Dockerfile.backend`](docker/Dockerfile.backend) & [`docker/Dockerfile.frontend`](docker/Dockerfile.frontend)*  
    — *Called in [`cloudbuild.yaml`](cloudbuild.yaml)*
  - **Least-Privilege Service Accounts:** Terraform provisions dedicated Google Cloud service accounts following least-privilege IAM principles—binding backend containers to a runtime service account granted access only to the project's storage bucket, Vertex AI models, logs, traces, and security filters, while separating frontend runtime and CI/CD deployment permissions.  
    — *Provisioned in [`infra/iam.tf`](infra/iam.tf)*  
    — *Called in [`infra/cloud_run.tf`](infra/cloud_run.tf)*
  - **Infrastructure & Deployment Automation:** Terraform declaratively provisions all required Google Cloud APIs, Artifact Registry repositories, and cloud resources using remote GCS state, while the root `Makefile` automates bootstrapping, image builds, and deployments (supporting both local Terraform CLI and Cloud Build execution).  
    — *Provisioned in [`infra/apis.tf`](infra/apis.tf) & [`infra/versions.tf`](infra/versions.tf)*  
    — *Called in [`infra/cloudbuild-terraform.yaml`](infra/cloudbuild-terraform.yaml) & [`Makefile`](Makefile)*

- **1.2 Zero-Trust User Authentication**
  - **Managed Sign-In Providers:** **Google Cloud Identity Platform (Firebase Authentication)** is provisioned to let users sign in with either **Google Sign-In (OAuth)** or **Email & Password**.  
    — *Provisioned in [`infra/auth.tf`](infra/auth.tf)*  
    — *Implemented in [`frontend/src/firebase.js`](frontend/src/firebase.js)*  
    — *Called in [`frontend/src/AuthGate.jsx`](frontend/src/AuthGate.jsx)*
  - **Automatic Bearer Token Attachment & Refresh:** The web application attaches the signed-in user's Firebase ID token to every REST and SSE streaming request and refreshes expired tokens automatically.  
    — *Implemented in [`frontend/src/firebase.js`](frontend/src/firebase.js) & [`frontend/src/api.js`](frontend/src/api.js)*  
    — *Called in [`frontend/src/App.jsx`](frontend/src/App.jsx)*
  - **Client Session Cleanup on Sign-Out:** Signing out or switching accounts immediately clears all local collection lists, active conversation threads, and chat messages in the browser.  
    — *Implemented in [`frontend/src/App.jsx`](frontend/src/App.jsx)*  
    — *Called in [`frontend/src/AuthGate.jsx`](frontend/src/AuthGate.jsx)*
  - **Cryptographic Backend Verification:** Every backend endpoint verifies the incoming token signature against Google's public keys, validates that the extracted user ID contains only safe characters, and disables the local development authentication bypass whenever running in the cloud.  
    — *Implemented in [`src/core/auth.py`](src/core/auth.py)*  
    — *Called in [`src/ingest/routes.py`](src/ingest/routes.py), [`src/query/routes.py`](src/query/routes.py), [`src/agent/routes.py`](src/agent/routes.py), [`src/manage/collections.py`](src/manage/collections.py) & [`src/manage/compliance.py`](src/manage/compliance.py)*

- **1.3 Multi-Tenant Data & Conversation Isolation**
  - **Identity-Bound Tenant Context (`uid`):** A user's tenant scope is derived exclusively from the verified Firebase JWT `uid` (validated against a strict alphanumeric pattern to prevent path traversal)—never from client-supplied request parameters or headers.  
    — *Implemented in [`src/core/auth.py`](src/core/auth.py)*  
    — *Called in [`src/core/storage/paths.py`](src/core/storage/paths.py), [`src/ingest/routes.py`](src/ingest/routes.py), [`src/query/routes.py`](src/query/routes.py), [`src/agent/routes.py`](src/agent/routes.py), [`src/manage/collections.py`](src/manage/collections.py) & [`src/manage/compliance.py`](src/manage/compliance.py)*
  - **Per-User Vector Storage & Collection Isolation:** Each user's ChromaDB files are isolated under a dedicated Cloud Storage prefix (`gs://<bucket>/users/{uid}/chroma_db/`) and an isolated local container directory (`/tmp/chroma_db/{uid}`). Two users can create, query, or delete collections with the exact same name without ever seeing or overwriting each other's documents.  
    — *Implemented in [`src/core/storage/paths.py`](src/core/storage/paths.py) & [`src/core/storage/chroma.py`](src/core/storage/chroma.py)*  
    — *Called in [`src/ingest/service.py`](src/ingest/service.py), [`src/query/service.py`](src/query/service.py) & [`src/manage/collections.py`](src/manage/collections.py)*
  - **Isolated Per-Request PDF Upload Staging:** In cloud deployments, the ingestion service accepts only multipart file uploads (blocking requests that reference local server file paths). Uploaded PDFs are staged in an isolated per-request folder (`/tmp/uploads/{uid}/{request_id}`), archived to the user's private Cloud Storage prefix (`gs://<bucket>/users/{uid}/uploads/`), and deleted from local disk as soon as indexing finishes.  
    — *Implemented in [`src/ingest/upload_parser.py`](src/ingest/upload_parser.py) & [`src/core/storage/gcs_sync.py`](src/core/storage/gcs_sync.py)*  
    — *Called in [`src/ingest/routes.py`](src/ingest/routes.py)*
  - **Tenant-Namespaced Conversation Memory:** Every multi-turn chat thread is namespaced as `{uid}:{thread_id}` inside the in-memory LangGraph checkpointer so one user can never read, resume, or clear another user's conversation history.  
    — *Implemented in [`src/agent/memory.py`](src/agent/memory.py)*  
    — *Called in [`src/agent/routes.py`](src/agent/routes.py), [`src/agent/service.py`](src/agent/service.py) & [`src/manage/compliance.py`](src/manage/compliance.py)*

- **1.4 Durable Cloud Storage $\leftrightarrow$ Container Memory Synchronization**
  - **Why We Synchronize Instead of Mounting Cloud Storage Directly:** ChromaDB relies on SQLite and memory-mapped index files, whereas Google Cloud Storage does not support the low-level file locks SQLite requires. Sharing a direct storage mount across containers would risk database lock errors and corrupted indexes, while storing data only in container memory would lose all collections whenever serverless instances scale down.
  - **Private Versioned Persistence Bucket:** Each user's authoritative vector database and uploaded PDFs are stored durably in a private, versioned **Google Cloud Storage** bucket with public access strictly blocked.  
    — *Provisioned in [`infra/storage.tf`](infra/storage.tf)*  
    — *Called in [`infra/cloud_run.tf`](infra/cloud_run.tf)*
  - **On-Demand Snapshot Synchronization & Version Tracking:** A lightweight `version.json` marker in Cloud Storage tracks whenever a user's vector database changes. Read requests download updated files into fast local `/tmp/chroma_db/{uid}` storage only when a newer version exists in Cloud Storage, while write requests (`POST /ingest` and `DELETE /collections/{name}`) push updated database files back to Cloud Storage and advance `version.json`.  
    — *Implemented in [`src/core/storage/gcs_sync.py`](src/core/storage/gcs_sync.py)*  
    — *Called in [`src/core/storage/chroma.py`](src/core/storage/chroma.py), [`src/ingest/routes.py`](src/ingest/routes.py) & [`src/manage/collections.py`](src/manage/collections.py)*
  - **Distributed Per-User Write Locks in Cloud Storage:** Before modifying a user's vector database, the backend acquires an atomic per-user lock object in Cloud Storage (with automatic TTL recovery if a previous container crashed) to prevent concurrent write conflicts across instances.  
    — *Implemented in [`src/core/storage/locks.py`](src/core/storage/locks.py)*  
    — *Called in [`src/core/storage/gcs_sync.py`](src/core/storage/gcs_sync.py)*
  - **Automatic Local `/tmp` LRU Cache Eviction:** An automatic Least-Recently-Used (LRU) cache evictor removes the oldest inactive user caches when local `/tmp` storage fills up while never evicting a directory currently locked by an active request.  
    — *Implemented in [`src/core/storage/locks.py`](src/core/storage/locks.py)*  
    — *Called in [`src/core/storage/gcs_sync.py`](src/core/storage/gcs_sync.py)*

- **1.5 Build-Once Frontend with Startup Runtime Configuration**
  - **Decoupling Image Builds from Cloud Provisioning:** Single-page web apps normally bake API URLs and Firebase keys into static JavaScript files during `docker build`, which happens *before* Terraform has provisioned the Load Balancer or generated the project's Firebase Web API key.
  - **Dynamic Startup Configuration (`/env-config.js`):** When the frontend container starts on Cloud Run, its startup script reads the environment variables injected by Terraform and writes `/env-config.js`, which the browser loads before starting the React app—allowing a single immutable frontend image to run in any environment without rebuilding.  
    — *Provisioned in [`infra/cloud_run.tf`](infra/cloud_run.tf)*  
    — *Implemented in [`docker/entrypoint-frontend.sh`](docker/entrypoint-frontend.sh)*  
    — *Called in [`frontend/index.html`](frontend/index.html) & [`frontend/src/firebase.js`](frontend/src/firebase.js)*

---

### 2. Zero-Internet VPC & Network Security ([`docs/NETWORK_SECURITY_PLAN.md`](docs/NETWORK_SECURITY_PLAN.md))

All external user traffic enters through a single HTTPS Load Balancer, while backend containers run inside a segmented private network with zero outbound access to the public internet.

- **2.1 Single Public HTTPS Entry Point (`Global External Application Load Balancer`)**
  - **Automatic Free HTTPS Certificate:** Terraform reserves a global static IP address and constructs a free `<IP-with-dashes>.sslip.io` domain name so Google Cloud can automatically provision and renew a trusted SSL/TLS certificate (and Identity Platform can authorize the domain) without requiring you to buy a custom domain. Plain HTTP requests on port `80` are automatically redirected to HTTPS on port `443`.  
    — *Provisioned in [`infra/load_balancer.tf`](infra/load_balancer.tf)*  
    — *Called in [`infra/auth.tf`](infra/auth.tf) & [`infra/outputs.tf`](infra/outputs.tf)*
  - **Single-Origin URL Path Routing:** The Load Balancer exposes the web interface and both backend APIs under the same HTTPS domain—routing `/api/ingest/*` to `ingest-api`, `/api/agent/*` to `agent-api`, and all other paths (`/*`) to the Frontend service.  
    — *Provisioned in [`infra/load_balancer.tf`](infra/load_balancer.tf)*
  - **Disabled Edge CDN Caching for Private Traffic:** Cloud CDN caching is explicitly disabled across all Load Balancer backends so private multi-tenant API responses and live chat streams are never cached at the network edge.  
    — *Provisioned in [`infra/load_balancer.tf`](infra/load_balancer.tf)*

- **2.2 Locked-Down Cloud Run Ingress**
  - **Blocking Direct Container Access:** All three Cloud Run services are configured to accept traffic only from the internal Load Balancer. Anyone attempting to visit an underlying `*.run.app` service URL directly from the internet is automatically blocked at Google's network edge before the request ever reaches a container.  
    — *Provisioned in [`infra/cloud_run.tf`](infra/cloud_run.tf)*

- **2.3 Segmented Private VPC with Zero Outbound Internet**
  - **Custom VPC Without an Internet Exit:** Backend services run inside a custom Virtual Private Cloud (VPC) that has its default route to the public internet removed. Even if a malicious file or prompt attempted to trick a container into sending data to an outside server, the network drops the connection immediately.  
    — *Provisioned in [`infra/network.tf`](infra/network.tf)*
  - **Workload-Segmented Private Subnets:** The Ingestion service (`ingest-subnet`) and the Agent service (`agent-subnet`) are placed in separate private subnets to isolate document parsing traffic from agent execution at the network layer.  
    — *Provisioned in [`infra/network.tf`](infra/network.tf)*  
    — *Called in [`infra/cloud_run.tf`](infra/cloud_run.tf)*
  - **Serverless Direct VPC Egress:** Both backend services route 100% of their outbound traffic into the private VPC using serverless **Cloud Run Direct VPC Egress** (routing traffic strictly to private internal IP ranges without requiring always-on NAT or proxy VMs).  
    — *Provisioned in [`infra/cloud_run.tf`](infra/cloud_run.tf)*

- **2.4 Private Internal Tunnels to Google Cloud APIs**
  - **Private Google Access for Standard Google APIs:** A private Cloud DNS zone for `googleapis.com` and a restricted internal VPC route (`199.36.153.8/30`) direct all backend calls to Cloud Storage, Vertex AI, Identity Platform, Cloud Logging, Cloud Trace, and Cloud DLP over Google's private internal backbone rather than the public internet.  
    — *Provisioned in [`infra/network.tf`](infra/network.tf)*
  - **Regional Private Service Connect (PSC) for Model Armor:** Because regional Model Armor endpoints use a dedicated regional domain (`modelarmor.<region>.rep.googleapis.com`), the VPC provisions a dedicated Private Service Connect endpoint and private DNS zone so AI safety screening traffic stays entirely on private internal IP addresses.  
    — *Provisioned in [`infra/network.tf`](infra/network.tf)*

---

### 3. AI-Specific Security — 5 Pillars ([`docs/AI_SECURITY_PLAN.md`](docs/AI_SECURITY_PLAN.md))

Every user question, retrieved document passage, and AI reply flows through a multi-stage security pipeline (prompt screening, iterative retrieval and reasoning, and output screening) organized around five pillars:

- **3.1 Pillar 1 — Prompt Injection Mitigation**
  - **Front-Door Prompt Screening:** Before calling Gemini, the agent's input screening stage sends the user's question to **Google Cloud Model Armor** to detect prompt-injection attempts, jailbreaks, and malicious links. Unsafe prompts are blocked immediately with a polite refusal without spending LLM tokens.  
    — *Provisioned in [`infra/ai_security.tf`](infra/ai_security.tf)*  
    — *Implemented in [`src/core/ai_security/armor.py`](src/core/ai_security/armor.py)*  
    — *Called in [`src/agent/graph.py`](src/agent/graph.py)*
  - **XML Spotlighting for Retrieved PDF Passages:** Because malicious instructions hidden inside a PDF could try to impersonate system commands, every retrieved document chunk is wrapped inside `<retrieved_chunk source="..." page="...">` tags with any internal `<` or `>` characters escaped first so a document can never break out of its quotation box, and the system prompt instructs Gemini to treat all chunk content strictly as untrusted reference data.  
    — *Implemented in [`src/agent/tools.py`](src/agent/tools.py)*  
    — *Called in [`src/agent/graph.py`](src/agent/graph.py)*
  - **Safe Collection Naming:** Collection names are validated against a strict alphanumeric pattern across all endpoints so attackers cannot smuggle prompt instructions into retriever tool descriptions via collection names.  
    — *Implemented in [`src/core/ai_security/guardrails.py`](src/core/ai_security/guardrails.py)*  
    — *Called in [`src/ingest/schemas.py`](src/ingest/schemas.py), [`src/ingest/upload_parser.py`](src/ingest/upload_parser.py), [`src/query/schemas.py`](src/query/schemas.py), [`src/agent/schemas.py`](src/agent/schemas.py) & [`src/manage/collections.py`](src/manage/collections.py)*

- **3.2 Pillar 2 — Model Access Controls**
  - **Server-Approved Model Catalogs:** Instead of allowing callers to request arbitrary Vertex AI models, the backend maintains curated allowlists of approved chat models (`gemini-2.5-flash`, `gemini-2.5-pro`, `gemini-2.5-flash-lite`) and embedding models (`gemini-embedding-001`, `text-embedding-005`).  
    — *Provisioned in [`infra/cloud_run.tf`](infra/cloud_run.tf)*  
    — *Implemented in [`src/core/ai_security/guardrails.py`](src/core/ai_security/guardrails.py)*
  - **Automatic Endpoint Validation:** Every ingestion, search, and chat request requires an explicit collection name, defaults to the primary approved model when omitted, and immediately rejects unapproved model names with `HTTP 422`.  
    — *Implemented in [`src/core/ai_security/guardrails.py`](src/core/ai_security/guardrails.py)*  
    — *Called in [`src/ingest/schemas.py`](src/ingest/schemas.py), [`src/ingest/upload_parser.py`](src/ingest/upload_parser.py), [`src/query/schemas.py`](src/query/schemas.py) & [`src/agent/schemas.py`](src/agent/schemas.py)*

- **3.3 Pillar 3 — AI & Ingestion Guardrails**
  - **Strict Document Grounding Guardrails:** The system prompt and retriever tool enforce anti-hallucination guardrails requiring the model to rely solely on `<retrieved_chunk>` evidence with 1-indexed `[filename.pdf, p.X]` citations and abstain whenever retrieved context is empty or insufficient.  
    — *Implemented in [`src/agent/tools.py`](src/agent/tools.py) & [`src/agent/graph.py`](src/agent/graph.py)*  
    — *Called in [`src/core/compliance/provenance.py`](src/core/compliance/provenance.py)*
  - **Query Length & Top-$k$ Search Bounds:** Request validators enforce maximum query text length (`4,000` characters) and similarity search result bounds (`k` between `1` and `50`).  
    — *Implemented in [`src/core/ai_security/guardrails.py`](src/core/ai_security/guardrails.py)*  
    — *Called in [`src/query/schemas.py`](src/query/schemas.py) & [`src/agent/schemas.py`](src/agent/schemas.py)*
  - **PDF File-Signature Verification & Upload Ceilings:** Ingestion validators enforce `%PDF-` magic-byte header verification, path-traversal filename sanitization, maximum file size (`20 MB` per PDF), maximum files per upload (`10`), and maximum extracted chunks per ingestion (`1,000`).  
    — *Implemented in [`src/core/ai_security/guardrails.py`](src/core/ai_security/guardrails.py)*  
    — *Called in [`src/ingest/upload_parser.py`](src/ingest/upload_parser.py) & [`src/ingest/splitter.py`](src/ingest/splitter.py)*
  - **Bounded Agent Retrieval Loops:** The agent's tool-calling loop is capped (`6` steps by default) so ambiguous questions cannot trigger endless retrieval cycles, returning a clean fallback message if the step limit is reached.  
    — *Implemented in [`src/core/ai_security/guardrails.py`](src/core/ai_security/guardrails.py)*  
    — *Called in [`src/agent/routes.py`](src/agent/routes.py) & [`src/agent/service.py`](src/agent/service.py)*

- **3.4 Pillar 4 — Output Filtering & Sanitized Errors**
  - **Automated PII & Secret Redaction Templates:** Terraform provisions **Google Cloud Sensitive Data Protection (Cloud DLP)** inspection and de-identification templates wired into **Model Armor** to automatically mask emails, phone numbers, credit card numbers, US Social Security numbers, IBAN codes, API keys, and auth tokens with `[INFO_TYPE]` placeholders (such as `[EMAIL_ADDRESS]`).  
    — *Provisioned in [`infra/ai_security.tf`](infra/ai_security.tf)*  
    — *Called in [`infra/cloud_run.tf`](infra/cloud_run.tf)*
  - **Two-Stage Redaction Across Search, JSON & Live Streaming:** Sensitive data is masked both when document passages are retrieved from ChromaDB (protecting direct `/query` searches and retriever tool traces) and again on the final AI answer in the output screening stage before responses are returned or streamed over SSE.  
    — *Provisioned in [`infra/ai_security.tf`](infra/ai_security.tf)*  
    — *Implemented in [`src/core/ai_security/armor.py`](src/core/ai_security/armor.py)*  
    — *Called in [`src/query/service.py`](src/query/service.py), [`src/agent/tools.py`](src/agent/tools.py), [`src/agent/graph.py`](src/agent/graph.py) & [`src/agent/service.py`](src/agent/service.py)*
  - **Sanitized Server Error Messages:** Unexpected exceptions across all REST and streaming endpoints log full diagnostic stack traces privately on the server while returning a generic, safe error message to the client so internal details never leak.  
    — *Implemented in [`src/core/ai_security/safety.py`](src/core/ai_security/safety.py)*  
    — *Called in [`src/ingest/routes.py`](src/ingest/routes.py), [`src/query/routes.py`](src/query/routes.py), [`src/agent/routes.py`](src/agent/routes.py) & [`src/agent/service.py`](src/agent/service.py)*

- **3.5 Pillar 5 — Content Safety**
  - **Dual-Layer Harm Filtering (Model Armor RAI + Vertex AI Gemini):**
    1. **Google Cloud Model Armor Responsible AI (RAI) Filters** screen both incoming user prompts and outgoing model responses across Hate Speech, Harassment, Sexually Explicit, and Dangerous Content categories.  
       — *Provisioned in [`infra/ai_security.tf`](infra/ai_security.tf)*  
       — *Implemented in [`src/core/ai_security/armor.py`](src/core/ai_security/armor.py)*  
       — *Called in [`src/agent/graph.py`](src/agent/graph.py)*
    2. **Vertex AI Gemini Safety Thresholds** explicitly configure every Gemini model instance with pinned harm-blocking thresholds across all four safety categories.  
       — *Implemented in [`src/core/ai_security/safety.py`](src/core/ai_security/safety.py)*  
       — *Called in [`src/agent/graph.py`](src/agent/graph.py)*
  - **Polite Refusal Fallbacks:** Whenever Model Armor or Gemini blocks a request or halts generation due to content safety rules, the backend replaces the blocked or empty output with a clear, polite refusal explanation so users never see a blank message bubble.  
    — *Implemented in [`src/core/ai_security/safety.py`](src/core/ai_security/safety.py)*  
    — *Called in [`src/agent/graph.py`](src/agent/graph.py)*

---

### 4. Compliance, Data Privacy & Governance ([`docs/COMPLIANCE_GOVERNANCE_PLAN.md`](docs/COMPLIANCE_GOVERNANCE_PLAN.md))

Enterprise governance controls ensure that all data and AI calls stay within approved single-region boundaries, every layer of a request is auditable without exposing personal data, users have self-service privacy controls and AI transparency badges, and operational policies are enforced automatically:

- **4.1 Pillar 1 — Data Residency & Single-Region Pinning**
  - **Live Organization Policy Verification:** Allowed geographic regions are governed by the project's **Google Cloud Resource Location Restriction** Organization Policy. Before any deployment runs, an automated check queries the live Organization Policy on the project and aborts if no location allowlist is active or if the target region is not permitted.  
    — *Implemented in [`scripts/check_region_policy.py`](scripts/check_region_policy.py)*  
    — *Called in [`Makefile`](Makefile)*
  - **Infrastructure Single-Region Validation:** Terraform validates that the configured deployment location is a concrete single region (such as `us-central1` or `europe-west1`)—rejecting `"global"` and multi-regions (`"us"`, `"eu"`, `"asia"`)—and enforces regional preconditions on storage buckets, security templates, and private endpoints.  
    — *Provisioned in [`infra/storage.tf`](infra/storage.tf), [`infra/ai_security.tf`](infra/ai_security.tf), [`infra/network.tf`](infra/network.tf) & [`infra/cloud_run.tf`](infra/cloud_run.tf)*
  - **Backend Regional Endpoint Binding:** At runtime, the Python backend validates that the configured location is a single region and explicitly binds all Vertex AI chat models, Vertex AI embedding models, and Model Armor clients to that regional endpoint.  
    — *Implemented in [`src/core/compliance/residency.py`](src/core/compliance/residency.py)*  
    — *Called in [`src/core/storage/chroma.py`](src/core/storage/chroma.py), [`src/agent/graph.py`](src/agent/graph.py) & [`src/core/ai_security/armor.py`](src/core/ai_security/armor.py)*

- **4.2 Pillar 2 — Privacy-Safe Audit Logging, GCP Data Access Logs & Trace Correlation**
  - **End-to-End Request & Trace Correlation:** A correlation middleware assigns or forwards a unique request ID (`X-Request-Id`) on every call and links it with Google Cloud's trace context so administrators can filter by a single ID in **Logs Explorer** to trace a request across the Load Balancer, backend microservices, AI safety checks, and cloud storage operations.  
    — *Implemented in [`src/core/context.py`](src/core/context.py) & [`src/core/compliance/audit.py`](src/core/compliance/audit.py)*  
    — *Called in [`src/core/app_factory.py`](src/core/app_factory.py)*
  - **PII-Free Application Audit Stream:** Backend services publish structured JSON audit records to a dedicated audit stream in **Google Cloud Logging** for authentication failures, document uploads, searches, agent chats, collection changes, privacy requests, rate-limit blocks, and safety filter actions—replacing raw user questions with one-way SHA-256 hashes and character counts so personal data never leaks into logs.  
    — *Implemented in [`src/core/compliance/audit.py`](src/core/compliance/audit.py)*  
    — *Called in [`src/core/auth.py`](src/core/auth.py), [`src/ingest/routes.py`](src/ingest/routes.py), [`src/query/routes.py`](src/query/routes.py), [`src/agent/routes.py`](src/agent/routes.py), [`src/agent/service.py`](src/agent/service.py), [`src/manage/collections.py`](src/manage/collections.py) & [`src/manage/compliance.py`](src/manage/compliance.py)*
  - **GCP Data Access Audit Logs & Load Balancer Access Logs:** Terraform enables Google Cloud Data Access Audit Logs (covering administrative reads, data reads, and data writes) for Cloud Storage, Vertex AI, Cloud DLP, and Model Armor alongside HTTP access logging on the HTTPS Load Balancer backends.  
    — *Provisioned in [`infra/compliance.tf`](infra/compliance.tf) & [`infra/load_balancer.tf`](infra/load_balancer.tf)*
  - **365-Day Regional Audit Log Archive:** A dedicated Cloud Logging sink routes application audit events, GCP Data Access Audit Logs, and Load Balancer HTTP access logs into a regional Cloud Logging bucket configured with 365-day retention.  
    — *Provisioned in [`infra/compliance.tf`](infra/compliance.tf)*

- **4.3 Pillar 3 — Regulatory Compliance (GDPR Self-Service Rights & EU AI Act Art. 50 Provenance)**
  - **GDPR Art. 15 Right of Access (`GET /compliance/my-data`):** Authenticated users can view a complete inventory of their stored collections, passage counts, uploaded PDF filenames, live and archived Cloud Storage object counts, total storage bytes, and active chat threads via the API or the **"My Data & Privacy"** modal in the web interface.  
    — *Implemented in [`src/core/compliance/dsar.py`](src/core/compliance/dsar.py)*  
    — *Called in [`src/manage/compliance.py`](src/manage/compliance.py) & [`frontend/src/App.jsx`](frontend/src/App.jsx)*
  - **GDPR Art. 17 Right to Erasure (`DELETE /compliance/my-data`) & Storage Hygiene:** Users can permanently erase all of their data in one action—deleting every live and archived object version in Cloud Storage, wiping local vector and upload caches, and clearing all conversation history from agent memory—complemented by Cloud Storage lifecycle rules that automatically prune archived object versions after 7 days.  
    — *Provisioned in [`infra/storage.tf`](infra/storage.tf)*  
    — *Implemented in [`src/core/compliance/dsar.py`](src/core/compliance/dsar.py) & [`src/agent/memory.py`](src/agent/memory.py)*  
    — *Called in [`src/manage/compliance.py`](src/manage/compliance.py) & [`frontend/src/App.jsx`](frontend/src/App.jsx)*
  - **EU AI Act Art. 50 AI Provenance & Transparency Badges:** Every `/agent` JSON response and `/agent/stream` SSE completion event includes structured provenance metadata and renders visual verification badges in the chat UI allowing the user to see:
    - **AI Generation, Model & Region:** Whether the reply was AI-generated or stopped by a guardrail, which exact **Gemini model** and **cloud region** produced it, and the correlating request ID.
    - **Citation Grounding:** Whether document grounding was enforced, whether the answer is backed by inline `[filename.pdf, p.X]` citations, and how many passages were retrieved.
    - **PII Redaction & Content Safety Activation:** Whether sensitive personal data (PII) was detected and redacted, whether Model Armor screening was active, which security filters ran, and whether any content safety or prompt-protection guardrails were triggered.  
    — *Implemented in [`src/core/compliance/provenance.py`](src/core/compliance/provenance.py) & [`src/core/compliance/state.py`](src/core/compliance/state.py)*  
    — *Called in [`src/agent/schemas.py`](src/agent/schemas.py), [`src/agent/routes.py`](src/agent/routes.py), [`src/agent/service.py`](src/agent/service.py) & [`frontend/src/App.jsx`](frontend/src/App.jsx)*

- **4.4 Pillar 4 — Policy Enforcement (Fail-Closed Screening, Rate Limits, Quotas & Pre-Deploy Gates)**
  - **Fail-Closed Security Screening in Production:** In production deployments, if Google Cloud Model Armor or Cloud DLP is unreachable, misconfigured, or times out during prompt screening, chunk redaction, or output filtering, the backend immediately halts processing, logs a high-severity audit event, and returns `HTTP 503 Service Unavailable` so unscreened content is never exposed.  
    — *Provisioned in [`infra/cloud_run.tf`](infra/cloud_run.tf)*  
    — *Implemented in [`src/core/ai_security/armor.py`](src/core/ai_security/armor.py)*  
    — *Called in [`src/ingest/routes.py`](src/ingest/routes.py), [`src/query/routes.py`](src/query/routes.py), [`src/agent/routes.py`](src/agent/routes.py) & [`src/agent/service.py`](src/agent/service.py)*
  - **Per-User Sliding-Window Rate Limiters:** Sliding-window rate limiters protect each user account on document uploads (`10/minute` default) and queries (`30/minute` default), returning `HTTP 429 Too Many Requests` with a `Retry-After` header when exceeded.  
    — *Provisioned in [`infra/cloud_run.tf`](infra/cloud_run.tf)*  
    — *Implemented in [`src/core/compliance/policy.py`](src/core/compliance/policy.py)*  
    — *Called in [`src/ingest/upload_parser.py`](src/ingest/upload_parser.py), [`src/query/routes.py`](src/query/routes.py) & [`src/agent/routes.py`](src/agent/routes.py)*
  - **Per-User Collection Count Quotas:** Each user is capped at a maximum number of collections (`20` default), returning `HTTP 403 Forbidden` when attempting to create a new collection past the limit while still allowing uploads into existing collections.  
    — *Provisioned in [`infra/cloud_run.tf`](infra/cloud_run.tf)*  
    — *Implemented in [`src/core/compliance/policy.py`](src/core/compliance/policy.py)*  
    — *Called in [`src/ingest/upload_parser.py`](src/ingest/upload_parser.py)*
  - **Cloud Run Production Deletion Protection:** Terraform enables accidental deletion protection on Cloud Run services in production deployments.  
    — *Provisioned in [`infra/cloud_run.tf`](infra/cloud_run.tf)*
  - **Automated Pre-Deploy Policy Gate (`make policy-check`):** Automated policy checks verify live Organization Policy compliance, single-region settings, fail-closed screening, and Terraform validity before any `make plan` or `make deploy` runs.  
    — *Implemented in [`scripts/check_static_policies.py`](scripts/check_static_policies.py) & [`scripts/check_region_policy.py`](scripts/check_region_policy.py)*  
    — *Called in [`Makefile`](Makefile)*

---

### 5. Observability & AI Monitoring ([`docs/OBSERVABILITY_PLAN.md`](docs/OBSERVABILITY_PLAN.md))

While compliance audit logging records *who* performed *what* action and *when* for regulatory accountability, the observability layer gives operators real-time visibility into *how fast*, *at what token cost*, and *with what retrieval and grounding quality* every request executed:

- **5.1 Pillar 1 — Structured Logging & Privacy-Safe Correlation**
  - **Dual-Mode Application Logging:** When running in the cloud, backend services emit single-line JSON log records containing severity levels, timestamps, source file locations, and trace identifiers so **Google Cloud Logging** can index and filter them automatically—while preserving readable color-coded terminal output during local development.  
    — *Provisioned in [`infra/cloud_run.tf`](infra/cloud_run.tf)*  
    — *Implemented in [`src/core/observability/logging.py`](src/core/observability/logging.py)*  
    — *Called in [`src/core/logger.py`](src/core/logger.py)*
  - **Shared Request Correlation Context:** Every operational log entry is automatically enriched with the active request ID, trace ID, span ID, service name, and authenticated user ID so logs from different components can be correlated without coupling operational logging to compliance audit modules.  
    — *Implemented in [`src/core/context.py`](src/core/context.py)*  
    — *Called in [`src/core/observability/logging.py`](src/core/observability/logging.py) & [`src/core/compliance/audit.py`](src/core/compliance/audit.py)*
  - **Automatic Query Scrubbing Filter:** A built-in log filter automatically intercepts any accidental raw query text before it is written to standard output and replaces it with a one-way SHA-256 hash and character count so user questions never leak into operational logs.  
    — *Implemented in [`src/core/observability/logging.py`](src/core/observability/logging.py)*  
    — *Called in [`src/core/logger.py`](src/core/logger.py)*
  - **Dedicated Regional Observability Log Bucket:** Operational container logs and AI performance telemetry are routed into a dedicated regional **Google Cloud Logging** bucket with a 30-day retention policy, keeping high-volume operational diagnostics separate from the 365-day compliance audit archive.  
    — *Provisioned in [`infra/observability.tf`](infra/observability.tf)*

- **5.2 Pillar 2 — Cloud Monitoring Metrics & AI Observability Dashboard**
  - **Zero-Overhead Log-Based Metrics:** Instead of making extra network calls to a monitoring API during user requests, **Google Cloud's Log Router** automatically extracts 10 custom performance and quality metrics directly from structured telemetry logs—tracking end-to-end turn latency, time-to-first-token, LLM vs. retrieval stage latency, input/output/total token consumption, retrieval similarity scores, ungrounded answer counts, and ingested chunk throughput with zero request-path overhead.  
    — *Provisioned in [`infra/observability.tf`](infra/observability.tf)*  
    — *Called in [`src/core/observability/telemetry.py`](src/core/observability/telemetry.py)*
  - **AI Quality, Latency & Token Dashboard:** Terraform provisions a **Google Cloud Monitoring** dashboard that gives operators a single view of median and tail (95th-percentile) response latency, time-to-first-token, Gemini reasoning vs. vector search stage durations, token consumption by model, retrieval similarity score distributions, ungrounded or blocked response rates, document ingestion throughput, and HTTP traffic volume.  
    — *Provisioned in [`infra/observability.tf`](infra/observability.tf)*

- **5.3 Pillar 3 — Distributed Tracing & Stage-Level Latency Breakdown**
  - **Hierarchical Per-Stage Execution Spans:** Every major step of a request—including Cloud Storage synchronization, PDF page splitting, Vertex AI embedding generation, Model Armor input screening, each Gemini reasoning step, ChromaDB similarity search, and output safety screening—is timed as an individual child span linked to the parent request trace so operators can pinpoint exactly which stage dominates latency.  
    — *Implemented in [`src/core/observability/tracing.py`](src/core/observability/tracing.py)*  
    — *Called in [`src/core/storage/gcs_sync.py`](src/core/storage/gcs_sync.py), [`src/ingest/splitter.py`](src/ingest/splitter.py), [`src/ingest/service.py`](src/ingest/service.py), [`src/query/service.py`](src/query/service.py), [`src/agent/graph.py`](src/agent/graph.py) & [`src/agent/tools.py`](src/agent/tools.py)*
  - **Cloud Trace Waterfall Export:** In cloud deployments, completed execution spans are exported to **Google Cloud Trace** so operators can inspect end-to-end request waterfall timelines alongside correlated application logs.  
    — *Provisioned in [`infra/cloud_run.tf`](infra/cloud_run.tf) & [`infra/iam.tf`](infra/iam.tf)*  
    — *Implemented in [`src/core/observability/tracing.py`](src/core/observability/tracing.py)*  
    — *Called in [`src/core/app_factory.py`](src/core/app_factory.py)*

- **5.4 Pillar 4 — AI-Specific Monitoring (Latency, Token Usage & Retrieval Quality)**
  - **Turn Latency, Stage Breakdown & Time-to-First-Token:** Every completed ingestion, similarity search, and agent conversation turn emits a structured telemetry summary recording total wall-clock duration, a millisecond breakdown across internal pipeline stages, and—for live streaming chats—the Time-to-First-Token (how quickly the user sees the first streamed event).  
    — *Implemented in [`src/core/observability/telemetry.py`](src/core/observability/telemetry.py) & [`src/core/observability/state.py`](src/core/observability/state.py)*  
    — *Called in [`src/ingest/routes.py`](src/ingest/routes.py), [`src/query/routes.py`](src/query/routes.py), [`src/agent/routes.py`](src/agent/routes.py) & [`src/agent/service.py`](src/agent/service.py)*
  - **Multi-Step Gemini Token Accounting:** Because an agent turn may invoke Gemini multiple times across iterative tool-calling steps, the backend extracts prompt, completion, and total token counts from each model call and aggregates total token usage and LLM call counts across the entire conversation turn for accurate cost tracking.  
    — *Implemented in [`src/core/observability/telemetry.py`](src/core/observability/telemetry.py)*  
    — *Called in [`src/agent/graph.py`](src/agent/graph.py)*
  - **Deterministic Retrieval & Grounding Quality Signals:** Every search and agent turn records normalized vector similarity scores (top and average match quality), the number of retriever tool calls and passages fetched, the number of inline `[filename.pdf, p.X]` citations produced, and whether the final answer was properly grounded—providing continuous retrieval quality signals without paying for extra LLM-as-a-judge calls.  
    — *Implemented in [`src/core/observability/telemetry.py`](src/core/observability/telemetry.py)*  
    — *Called in [`src/agent/tools.py`](src/agent/tools.py), [`src/query/service.py`](src/query/service.py), [`src/agent/routes.py`](src/agent/routes.py) & [`src/agent/service.py`](src/agent/service.py)*

---

### 6. Scalability & Elasticity ([`docs/SCALABILITY_ELASTICITY_PLAN.md`](docs/SCALABILITY_ELASTICITY_PLAN.md))

InteractiveRAG combines workload-isolated microservice packaging with horizontal, vertical, and storage scaling optimizations so heavy PDF uploads never degrade interactive user latency:

- **6.1 Workload-Isolated Microservice Packaging (4 Packages Across 2 Backend Services)**
  - **Dedicated Heavy-Write Ingestion Service (`ingest-api`):** Runs only the document ingestion package (`POST /ingest`) on dedicated instances so CPU-intensive PDF parsing and vector index construction never compete with or slow down live user chats.  
    — *Provisioned in [`infra/cloud_run.tf`](infra/cloud_run.tf)*  
    — *Implemented in [`src/ingest/routes.py`](src/ingest/routes.py)*  
    — *Called in [`src/ingest/app.py`](src/ingest/app.py)*
  - **Unified Interactive Read, Management & Agent Service (`agent-api`):** Co-locates collection management, direct similarity search, and multi-turn LangGraph agent execution on a single interactive service so searches and follow-up chat turns share the same warm local vector cache, unified query rate limiter, and atomic GDPR data inventory and erasure workflow.  
    — *Provisioned in [`infra/cloud_run.tf`](infra/cloud_run.tf)*  
    — *Implemented in [`src/agent/routes.py`](src/agent/routes.py), [`src/query/routes.py`](src/query/routes.py) & [`src/manage/routes.py`](src/manage/routes.py)*  
    — *Called in [`src/agent/app.py`](src/agent/app.py)*

- **6.2 Horizontal, Vertical & Network Scaling Profiles**
  - **Workload-Tuned Request Concurrency Ceilings:** Each Cloud Run service enforces a per-instance request concurrency limit tailored to its workload—limiting the ingestion service to `4` concurrent uploads per container to prevent CPU contention, allowing `32` concurrent interactive requests per container on the agent service (since chat and search spend most of their time waiting on external network I/O), and `80` concurrent requests on the static web frontend.  
    — *Provisioned in [`infra/cloud_run.tf`](infra/cloud_run.tf)*
  - **Right-Sized Vertical CPU & Memory Allocations:** The ingestion service is provisioned with `2 vCPU` and `4 GiB` of RAM to provide ample memory headroom for concurrent PDF parsing, batch embedding generation, and in-memory `/tmp` storage, while the interactive agent service runs on `2 vCPU` and `2 GiB` of RAM.  
    — *Provisioned in [`infra/cloud_run.tf`](infra/cloud_run.tf)*
  - **Service-Specific Horizontal Scale-Out Caps:** Cloud Run automatically scales each service from zero idle instances up to service-specific maximums—capping the ingestion service at `5` instances (`20` concurrent PDF uploads) to protect downstream Vertex AI and Cloud DLP quotas, while scaling the interactive agent service up to `10` instances (`320` concurrent interactive requests) and the frontend up to `5` instances.  
    — *Provisioned in [`infra/cloud_run.tf`](infra/cloud_run.tf)*
  - **Session Affinity for Multi-Turn Conversations:** Cloud Run session affinity is enabled on the interactive agent service so consecutive turns from the same user prefer the same container instance, maximizing hits on the user's in-memory conversation checkpointer and warm local `/tmp` vector cache.  
    — *Provisioned in [`infra/cloud_run.tf`](infra/cloud_run.tf)*
  - **Expanded `/24` Private VPC Subnets for Scale-Out Headroom:** Both private backend subnets are sized as `/24` CIDR blocks (`251` usable IP addresses each), ensuring serverless Direct VPC Egress never runs out of private IP addresses during traffic spikes or revision rollouts.  
    — *Provisioned in [`infra/network.tf`](infra/network.tf)*  
    — *Called in [`infra/cloud_run.tf`](infra/cloud_run.tf)*
  - **Bounded In-Memory `/tmp` Vector Cache Quotas:** Because Cloud Run mounts `/tmp` directly inside container RAM, both backend services enforce a `512 MiB` cap on cached user vector databases and maintain at least `256 MiB` of free container memory—automatically evicting the oldest inactive user caches before memory pressure can trigger an out-of-memory crash.  
    — *Provisioned in [`infra/cloud_run.tf`](infra/cloud_run.tf)*  
    — *Implemented in [`src/core/storage/locks.py`](src/core/storage/locks.py)*  
    — *Called in [`src/core/storage/gcs_sync.py`](src/core/storage/gcs_sync.py)*

- **6.3 API Throughput, Incremental Storage Sync & Scaling Observability**
  - **Parallel Retrieval-Time Privacy Screening:** When a similarity search or agent tool call retrieves top-$k$ passages from ChromaDB, the backend screens all retrieved passages for sensitive personal data concurrently across a bounded pool of up to `8` worker threads (`1` passage per thread) instead of scanning them one after another—cutting retrieval-stage screening latency to the duration of a single passage scan.  
    — *Implemented in [`src/core/ai_security/armor.py`](src/core/ai_security/armor.py)*  
    — *Called in [`src/query/service.py`](src/query/service.py) & [`src/agent/tools.py`](src/agent/tools.py)*
  - **Incremental MD5 Cloud Storage Delta Synchronization:** When pushing updated ChromaDB files back to Cloud Storage after an upload or collection deletion, the backend compares local file MD5 checksums against remote Cloud Storage object metadata and uploads only new or modified files, avoiding redundant multi-megabyte uploads as user vector databases grow.  
    — *Implemented in [`src/core/storage/gcs_sync.py`](src/core/storage/gcs_sync.py)*  
    — *Called in [`src/ingest/routes.py`](src/ingest/routes.py) & [`src/manage/collections.py`](src/manage/collections.py)*
  - **Infrastructure Scaling & Utilization Dashboard Panels:** The Cloud Monitoring dashboard includes dedicated scaling panels charting active vs. idle container instance counts, 95th-percentile concurrent requests per instance, CPU utilization, and container memory utilization across all services.  
    — *Provisioned in [`infra/observability.tf`](infra/observability.tf)*
  - **Automated Latency, Memory Saturation & Error Alert Policies:** Cloud Monitoring alert policies automatically notify operators whenever 95th-percentile agent turn latency exceeds `15 seconds` over a sustained `5-minute` window, container memory utilization exceeds `85%` over `3 minutes`, or server `5xx` error rates exceed `0.05 requests/second` over `2 minutes`.  
    — *Provisioned in [`infra/observability.tf`](infra/observability.tf)*

---

## 📁 Repository Structure

```text
InteractiveRAG/
├── src/                                # Unified Python backend packages (4 domain packages + core)
│   ├── core/                           # Shared authentication, storage, AI security, compliance & observability
│   │   ├── ai_security/                # 5-pillar AI security package
│   │   │   ├── armor.py                # Model Armor & Cloud DLP screening/redaction client
│   │   │   ├── guardrails.py           # Collection/query validators & PDF upload guardrails
│   │   │   └── safety.py               # Gemini safety settings & refusal detection
│   │   ├── compliance/                 # 4-pillar compliance & governance package
│   │   │   ├── audit.py                # Structured JSON audit logger & SHA-256 query hashing
│   │   │   ├── dsar.py                 # GDPR Art. 15 inventory & Art. 17 versioned erasure
│   │   │   ├── policy.py               # Per-user sliding-window rate limiters & collection quotas
│   │   │   ├── provenance.py           # EU AI Act Art. 50 transparency & citation grounding metadata
│   │   │   ├── residency.py            # Single-region GCP location validation
│   │   │   └── state.py                # Per-request compliance guardrail & retrieval state helpers
│   │   ├── observability/              # Observability & AI monitoring package
│   │   │   ├── logging.py              # Structured GCP JSON formatter & request correlation filter
│   │   │   ├── state.py                # Per-request latency, TTFT, token, score & span state accumulators
│   │   │   ├── telemetry.py            # AI-specific monitoring & interactiverag-telemetry publisher
│   │   │   └── tracing.py              # Hierarchical per-stage span tracer & Cloud Trace exporter
│   │   ├── storage/                    # Multi-tenant ChromaDB & Cloud Storage persistence
│   │   │   ├── chroma.py               # Regional Vertex AI embeddings & ChromaDB client factory
│   │   │   ├── gcs_sync.py             # Incremental MD5 delta GCS <-> /tmp sync & PDF archive
│   │   │   ├── locks.py                # Atomic GCS distributed write locks & bounded /tmp LRU eviction
│   │   │   └── paths.py                # Tenant-isolated path helpers & client cache release
│   │   ├── app_factory.py              # Shared FastAPI app factory, CORS, middleware & /health
│   │   ├── auth.py                     # Identity Platform / Firebase Bearer JWT verification
│   │   ├── config.py                   # Approved model catalogs, /tmp caps, guardrail limits & settings
│   │   ├── context.py                  # Shared RequestContext & RequestCorrelationMiddleware
│   │   └── logger.py                   # Application logging setup
│   ├── ingest/                         # Package 1: Dedicated Document Ingestion Service (Port 8000, `ingest-api`)
│   │   ├── app.py                      # FastAPI service mounting ONLY ingest_router & GET /health
│   │   ├── routes.py                   # ingest_router (`POST /ingest`)
│   │   ├── schemas.py                  # Validated IngestRequest model
│   │   ├── service.py                  # Batched ChromaDB indexing inside distributed GCS write lock
│   │   ├── splitter.py                 # PDF page loading & overlapping text chunking
│   │   └── upload_parser.py            # Multipart PDF upload staging, quota & magic-byte checks
│   ├── manage/                         # Package 2: Collection Management & GDPR Self-Service (`agent-api`)
│   │   ├── collections.py              # collections_router (`GET /collections` & `DELETE /collections/{collection_name}`)
│   │   ├── compliance.py               # compliance_router (`GET /compliance/my-data` & `DELETE /compliance/my-data`)
│   │   └── routes.py                   # manage_router combining collections_router & compliance_router
│   ├── query/                          # Package 3: Direct Vector Similarity Search (`agent-api`)
│   │   ├── routes.py                   # query_router (`POST /query`)
│   │   ├── schemas.py                  # Validated QueryRequest model
│   │   └── service.py                  # DLP-sanitized top-k Chroma similarity search
│   ├── agent/                          # Package 4: Unified Interactive Agent, Query & Manage API (Port 8001, `agent-api`)
│   │   ├── app.py                      # FastAPI service mounting agent_router, query_router & manage_router
│   │   ├── display.py                  # Terminal message formatting helpers
│   │   ├── graph.py                    # Hardened StateGraph (input_guard -> model_call <-> tools -> output_guard)
│   │   ├── memory.py                   # Tenant-isolated conversation checkpoint memory
│   │   ├── routes.py                   # agent_router (`POST /agent` & `POST /agent/stream`)
│   │   ├── schemas.py                  # Validated AgentRequest, AgentResponse & AgentState schemas
│   │   ├── service.py                  # Bounded recursion runner & live SSE event generator
│   │   └── tools.py                    # XML-spotlighted retriever tool with 1-indexed citations
│   └── README.md                       # Backend package reference
├── frontend/                           # React + Vite web dashboard (Port 3000)
│   ├── index.html                      # Loads runtime /env-config.js before the React bundle
│   └── src/                            # App.jsx, AuthGate.jsx (Sign-In UI), api.js, firebase.js
├── infra/                              # Reproducible Terraform Infrastructure as Code
│   ├── versions.tf                     # Terraform & Google provider versions + GCS state backend
│   ├── apis.tf                         # Required GCP service APIs & Artifact Registry repository
│   ├── iam.tf                          # Dedicated backend service account & least-privilege roles
│   ├── storage.tf                      # Private versioned user data bucket & lifecycle rules
│   ├── auth.tf                         # Identity Platform configuration & OAuth/Email sign-in
│   ├── network.tf                      # Zero-internet VPC, /24 subnets, Private Google Access & PSC
│   ├── load_balancer.tf                # Global HTTPS Load Balancer, NEGs & managed sslip.io cert
│   ├── ai_security.tf                  # Cloud DLP inspect/de-identify templates & Model Armor
│   ├── cloud_run.tf                    # Frontend, Ingestion API & Agent API Cloud Run services
│   ├── compliance.tf                   # Cloud Audit Logs, 365-day regional audit bucket & log sink
│   ├── observability.tf                # Regional telemetry log bucket, sink, log metrics, dashboard & alerts
│   ├── variables.tf                    # Parameterized infrastructure, scaling, security & compliance settings
│   ├── outputs.tf                      # Deployed URLs, IPs, bucket names & template identifiers
│   ├── terraform.tfvars.example        # Template for environment-specific variable values
│   └── cloudbuild-terraform.yaml       # Cloud Build pipeline for remote Terraform execution
├── scripts/                            # Automated pre-deployment compliance & policy scripts
│   ├── check_region_policy.py          # Verifies target region against live GCP Org Policy
│   └── check_static_policies.py        # Offline verification of Terraform & runtime security/scaling policies
├── docs/                               # Architecture, security, observability & scaling blueprints
│   ├── GCP_DEPLOYMENT_PLAN.md          # Layer 1: Cloud Deployment & Multi-Tenant Persistence
│   ├── NETWORK_SECURITY_PLAN.md        # Layer 2: Zero-Internet VPC & Network Security
│   ├── AI_SECURITY_PLAN.md             # Layer 3: AI-Specific Security (5 Pillars)
│   ├── COMPLIANCE_GOVERNANCE_PLAN.md   # Layer 4: Compliance, Data Privacy & Governance
│   ├── OBSERVABILITY_PLAN.md           # Layer 5: Observability & AI Monitoring (4 Pillars)
│   └── SCALABILITY_ELASTICITY_PLAN.md  # Layer 6: Scalability & Elasticity (6 Pillars)
├── tests/                              # Automated unit suites & live smoke scripts
│   ├── auth.py                         # Token verification, tenant UID validation & bypass tests
│   ├── storage.py                      # Per-user GCS sync, distributed lock & LRU cache tests
│   ├── multi_tenant_api.py             # Cross-tenant isolation tests across REST & SSE APIs
│   ├── ai_security.py                  # Unit tests for all 5 AI security pillars
│   ├── compliance.py                   # Unit tests for residency, audit logs, DSAR, provenance & quotas
│   ├── observability.py                # Unit tests for structured logs, spans, tokens, TTFT & telemetry
│   ├── scalability_elasticity.py       # Unit tests for 4-package split, concurrency, /tmp caps, MD5 delta sync & alerts
│   ├── ingestion.py                    # Live local PDF ingestion & similarity search smoke test
│   └── agent.py                        # Live local JSON & SSE agent streaming smoke test
├── docker/                             # Backend & frontend Dockerfiles + Nginx startup scripts
├── data/                               # Sample PDF documents for local testing
├── Makefile                            # Automated bootstrap, policy checks, builds & deployment
├── cloudbuild.yaml                     # Multi-image Cloud Build configuration
└── requirements.txt                    # Python dependencies
```

---

## 🚀 Local Setup & Prerequisites

### 1. Authenticate with Google Cloud & Configure `.env`
Authenticate your local Application Default Credentials (ADC) so the backend services can call Vertex AI:
```bash
gcloud auth application-default login
```

Create a `.env` file in the project root:
```bash
GOOGLE_CLOUD_PROJECT="your-gcp-project-id"
GOOGLE_CLOUD_LOCATION="us-central1"

# Local development bypass (automatically ignored on Cloud Run when K_SERVICE is set):
AUTH_DISABLED="true"
DEV_USER_ID="local-dev"

# Optional AI security, rate-limit, quota & /tmp cache overrides (defaults shown):
# ALLOWED_LLM_MODELS="gemini-2.5-flash,gemini-2.5-pro,gemini-2.5-flash-lite"
# ALLOWED_EMBEDDING_MODELS="gemini-embedding-001,text-embedding-005"
# GEMINI_SAFETY_THRESHOLD="BLOCK_LOW_AND_ABOVE"
# MODEL_ARMOR_TEMPLATE_ID=""
# RATE_LIMIT_INGEST_PER_MINUTE="10"
# RATE_LIMIT_QUERY_PER_MINUTE="30"
# MAX_COLLECTIONS_PER_USER="20"
# CHROMA_TMP_MAX_BYTES="536870912"
# CHROMA_TMP_MIN_FREE_BYTES="268435456"
```

### 2. Install Python Dependencies
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Install Frontend Dependencies
```bash
cd frontend
npm install
cd ..
```

---

## 🏃 Running Locally

Start each service in its own terminal from the repository root:

1. **Dedicated Document Ingestion API (`ingest-api`, Port 8000)**
   ```bash
   python -m src.ingest
   ```
2. **Unified Interactive Agent, Query & Management API (`agent-api`, Port 8001)**
   ```bash
   python -m src.agent
   ```
3. **React Web Dashboard (Port 3000)**
   ```bash
   cd frontend && npm run dev
   ```

Open **http://localhost:3000** in your browser.

---

## ☁️ Deploying to Google Cloud (Terraform + Cloud Build)

The root [`Makefile`](Makefile) automates the complete deployment lifecycle. It automatically uses your local `terraform` binary if installed (`TF_MODE=local`) or falls back to running Terraform inside Google Cloud Build (`TF_MODE=cloudbuild`) using a dedicated least-privilege deployer service account.

1. **Configure Terraform Variables**
   ```bash
   cp infra/terraform.tfvars.example infra/terraform.tfvars
   # Edit infra/terraform.tfvars to set your project_id, region, and optional settings
   ```

2. **Bootstrap Foundational APIs, State Bucket & Artifact Registry**
   ```bash
   make bootstrap
   ```

3. **Build & Push Container Images (`linux/amd64`)**
   ```bash
   make build-push
   ```

4. **Run Pre-Deployment Policy Gates, Preview & Apply Infrastructure**
   ```bash
   make policy-check
   make plan
   make deploy
   ```

5. **Inspect Provisioned URLs & Outputs**
   ```bash
   make outputs
   ```
   > **Note:** On initial deployment, allow ~10–15 minutes for the Google-managed SSL certificate on `https://<LB-IP-with-dashes>.sslip.io` to reach `ACTIVE` status. If Google Sign-In OAuth is enabled, add the `sslip.io` URL to your OAuth 2.0 Web Client's **Authorized JavaScript origins** in the Google Cloud Console.

---

## 🧪 Testing & Verification

### 1. Offline Unit, Multi-Tenant, AI Security, Compliance, Observability & Scalability Test Suites
Run the full offline test suite and static policy verifier (no live cloud resources required):
```bash
make check-static-policy
python -m unittest tests.auth tests.storage tests.multi_tenant_api tests.ai_security tests.compliance tests.observability tests.scalability_elasticity -v
```

### 2. Live Local Smoke Scripts
With `src.ingest` (port `8000`) and `src.agent` (port `8001`) running locally:
```bash
# Ingest sample PDFs into ChromaDB and verify similarity search
python -m tests.ingestion

# Test the Agentic RAG pipeline in both standard JSON and live SSE streaming modes
python -m tests.agent
```

### 3. Manual API Testing via `curl`
For local requests with `AUTH_DISABLED=true`, call `http://localhost:8000` (`ingest-api`) for `/ingest` and `http://localhost:8001` (`agent-api`) for `/query`, `/collections`, `/compliance/my-data`, and `/agent*`. When calling the deployed Load Balancer (`https://<LB-HOST>`), prefix paths with `/api/ingest` or `/api/agent` and include `-H "Authorization: Bearer <ID_TOKEN>"`:

```bash
# 1. Upload & index a PDF into a collection (ingest-api on port 8000)
curl -X POST "http://localhost:8000/ingest" \
  -F "files=@./data/stock-market-performance/Stock_Market_Performance_2024.pdf" \
  -F "collection_name=stock_market_vs" \
  -F "embedding_model=gemini-embedding-001"

# 2. Run direct top-k semantic similarity search (agent-api on port 8001)
curl -X POST "http://localhost:8001/query" \
  -H "Content-Type: application/json" \
  -d '{
    "query": "What is ARM Holdings?",
    "collection_name": "stock_market_vs",
    "k": 3
  }'

# 3. Ask the Agentic RAG assistant over a live SSE stream (agent-api on port 8001)
curl -N -X POST "http://localhost:8001/agent/stream" \
  -H "Content-Type: application/json" \
  -d '{
    "query": "What was ARM Holdings financial performance in 2024?",
    "collection_name": "stock_market_vs",
    "model_name": "gemini-2.5-flash"
  }'

# 4. Inspect your personal data inventory (GDPR Art. 15 Right of Access on agent-api port 8001)
curl -s "http://localhost:8001/compliance/my-data" | python3 -m json.tool
```

### 4. Interactive Web Dashboard Workflow
1. Open **http://localhost:3000** (or your deployed `https://<LB-IP-with-dashes>.sslip.io` URL and sign in via **Identity Platform**).
2. **Ingest PDFs**: Upload one or more PDF files into a named collection using an approved embedding model.
3. **Manage Collections & Privacy**: View your isolated collections and passage counts, delete collections you no longer need, or open **"My Data & Privacy"** to inspect your full data footprint or erase all account data.
4. **Chat & Verify Provenance Badges**: Select an approved Gemini model and ask questions in the chat panel to watch real-time retriever tool calls, grounded `[filename.pdf, p.X]` citations, DLP-filtered responses, and EU AI Act transparency badges.
