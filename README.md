# InteractiveRAG

**InteractiveRAG** is a production-hardened, multi-tenant **Agentic Retrieval-Augmented Generation (RAG)** platform built with **LangGraph**, **Google Cloud Vertex AI**, **ChromaDB**, **FastAPI**, and **React**.

Instead of combining unrelated documents into a single shared database, InteractiveRAG gives every signed-in user an isolated workspace where they can organize PDFs into domain-specific vector collections (for example, separating annual financial reports from clinical research). Through the web dashboard or REST APIs, users can:

- **Ingest PDF Documents (`POST /ingest`)**: Upload PDF documents that are parsed page-by-page, split into overlapping text chunks, converted into vector embeddings using Vertex AI, and indexed into a named vector collection.
- **Manage Document Collections (`GET /collections`, `DELETE /collections/{collection_name}`)**: List all vector collections in the user's workspace along with their indexed chunk counts, or delete a collection when it is no longer needed.
- **Run Direct Semantic Search (`POST /query`)**: Perform top-$k$ similarity searches scoped strictly to a selected collection to inspect relevant document passages, similarity scores, and page numbers directly.
- **Chat with a Grounded AI Agent (`POST /agent`, `POST /agent/stream`)**: Ask questions in a multi-turn conversation with a stateful LangGraph agent that dynamically queries the user's chosen collection, grounds every factual statement in retrieved evidence with inline `[filename.pdf, p.X]` citations, and streams each workflow step in real time over Server-Sent Events (SSE).
- **Exercise Self-Service Privacy Controls (`GET /compliance/my-data`, `DELETE /compliance/my-data`)**: Export a complete inventory of stored collections, uploaded PDFs, storage bytes, and active conversation threads, or permanently erase all personal data—including live and archived Cloud Storage object versions, local vector caches, and conversation memory—in a single action.

---

## 🧠 Core Agentic RAG Architecture & Retrieval Pipeline

InteractiveRAG organizes its backend into **4 modular Python packages** (`src/ingest`, `src/manage`, `src/query`, `src/agent`) deployed across **2 backend services** so heavy document indexing never blocks interactive queries or chat:
- **`ingest-api` (Document Ingestion Service)**: Calls **only** `src/ingest` (`POST /ingest`) to run PDF parsing, text chunking, and batch vector indexing on dedicated compute instances.
- **`agent-api` (Interactive Agent, Search & Management Service)**: Calls **`src/manage`** (`GET /collections`, `DELETE /collections/{collection_name}`, `GET/DELETE /compliance/my-data`), **`src/query`** (`POST /query`), and **`src/agent`** (`POST /agent`, `POST /agent/stream`) to serve collection management, direct similarity search, and multi-turn LangGraph agent conversations.

### 1. PDF Document Parsing, Chunking & Indexing Strategy
- **Multipart PDF Upload Staging:** Uploaded PDF files are staged in a temporary workspace where original filenames are normalized before page extraction begins, and temporary files are cleaned up automatically once indexing finishes. — *See [`src/ingest/upload_parser.py`](src/ingest/upload_parser.py) & [`src/ingest/splitter.py`](src/ingest/splitter.py)*
- **Page-Aware PDF Parsing & Overlapping Text Chunking:** Uploaded PDFs are loaded page-by-page using `PyPDFLoader`—preserving the source document filename and page number on every page—and split with `RecursiveCharacterTextSplitter` into `1,000`-character passages with a `200`-character sliding overlap. Splitting recursively along paragraph and sentence boundaries with a `200`-character overlap prevents key facts or tables at chunk boundaries from being cut in half. — *See [`src/ingest/splitter.py`](src/ingest/splitter.py)*
- **Batched Vector Indexing:** Extracted text chunks are embedded and written into the target ChromaDB collection sequentially in batches of `100` chunks per operation so multi-PDF uploads are indexed reliably. — *See [`src/ingest/service.py`](src/ingest/service.py)*

### 2. Embedding Models & Domain-Scoped Vector Databases
- **Vertex AI Dense Vector Embeddings:** Document chunks (at ingestion time) and search queries (at retrieval time) are converted into dense semantic vectors using **Google Cloud Vertex AI** embedding models (`gemini-embedding-001` by default, or `text-embedding-005`). — *See [`src/core/storage/chroma.py`](src/core/storage/chroma.py) & [`src/core/config.py`](src/core/config.py)*
- **Domain-Scoped ChromaDB Vector Collections:** Instead of pooling all files into a single flat vector index (which degrades retrieval accuracy when documents cover different topics), documents are organized into separate named **ChromaDB** collections backed by SQLite metadata storage and HNSW approximate nearest-neighbor vector indexes. Searches (`POST /query`) and agent conversations (`POST /agent`, `POST /agent/stream`) target one chosen collection at a time to keep retrieval focused and free of cross-topic noise. — *See [`src/core/storage/chroma.py`](src/core/storage/chroma.py), [`src/manage/collections.py`](src/manage/collections.py), [`src/query/service.py`](src/query/service.py) & [`src/ingest/service.py`](src/ingest/service.py)*

### 3. Agentic RAG Architecture Pattern (Stateful LangGraph Pipeline)
- **Why Agentic RAG Instead of Fixed Single-Shot Retrieval:** Traditional RAG pipelines run a single similarity search using the user's raw prompt, which often struggles with conversational follow-up questions, multi-part questions, or queries that require rephrasing search terms. InteractiveRAG uses **LangGraph** to orchestrate a stateful, cyclic tool-calling workflow (`model_call <-> tools`) powered by **Vertex AI Gemini** (`gemini-2.5-flash`, `gemini-2.5-pro`, or `gemini-2.5-flash-lite` at `temperature=0` for deterministic, factual responses). — *See [`src/agent/graph.py`](src/agent/graph.py)*
- **Dynamic Collection-Scoped Retriever Tool & Iterative Search:** On each request, the backend builds a structured retriever tool (`<collection>_retriever`, retrieving the top-$k=5$ most relevant passages per call) bound strictly to the selected collection. Gemini autonomously decides when to call the retriever tool, formulates targeted search queries based on the conversation history, inspects the returned passages, and can perform additional search steps with refined queries if the initial passages are insufficient before synthesizing its final answer. — *See [`src/agent/tools.py`](src/agent/tools.py) & [`src/agent/stream.py`](src/agent/stream.py)*
- **Multi-Turn Conversation Memory & Step-by-Step SSE Event Streaming:** Conversation history is persisted across turns using an in-memory LangGraph checkpointer (`InMemorySaver`) so users can ask natural follow-up questions without repeating context. During execution, the streaming engine deduplicates graph state transitions and streams each completed workflow step—retriever tool invocations, retrieved evidence passages, and the final AI answer—in real time over Server-Sent Events (SSE). — *See [`src/agent/memory.py`](src/agent/memory.py), [`src/agent/stream.py`](src/agent/stream.py) & [`src/agent/routes.py`](src/agent/routes.py)*

### 4. Document Grounding & Inline Page Citations
- **1-Indexed Page Normalization & Structured Passage Context:** PDF parsers number pages starting at `0`, which causes off-by-one confusion when cited directly. Before returning passages to the language model, the retriever tool converts `0`-based page metadata into human-readable `1`-indexed page numbers (`p.1`, `p.2`, ...) and attaches the exact `source` filename and `page` number to each passage (or marks the retrieval result as empty if no matching chunks are found). — *See [`src/agent/tools.py`](src/agent/tools.py)*
- **Mandatory Factual Grounding & Explicit Abstention:** The system prompt enforces a strict grounding contract: the agent must call the retriever tool before answering factual questions, base every factual claim *exclusively* on the retrieved document passages rather than pretraining memory or assumptions, and clearly state that the answer cannot be found in the available documents whenever retrieved passages are empty or lack sufficient evidence. — *See [`src/agent/graph.py`](src/agent/graph.py)*
- **Inline `[source, p.X]` Citations:** Every factual statement in the assistant's answer must include an inline citation matching the exact `source` filename and `1`-indexed `page` number of the supporting passage (for example, `[annual_report.pdf, p.4]`) so users can verify every claim against the source PDF. — *See [`src/agent/graph.py`](src/agent/graph.py) & [`src/agent/tools.py`](src/agent/tools.py)*

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
| **6. Scalability & Elasticity** | [`docs/SCALABILITY_ELASTICITY_PLAN.md`](docs/SCALABILITY_ELASTICITY_PLAN.md) | 4-package / 2-service workload isolation, horizontal/vertical scaling profiles, explicit per-service concurrency, `/24` VPC subnets, bounded `/tmp` `tmpfs` eviction, incremental MD5 GCS delta sync, and scaling saturation alerts |

---

### 1. Cloud Deployment & Multi-Tenant Persistence ([`docs/GCP_DEPLOYMENT_PLAN.md`](docs/GCP_DEPLOYMENT_PLAN.md))

Multiple users can sign in and use the cloud platform simultaneously—each person gets their own isolated document collections and chat history, while serverless containers automatically scale down to zero when idle to save cost.

- **1.1 Serverless Microservice Architecture**
  - **Serverless Cloud Run Services:** The application runs on **Google Cloud Run** across the React web frontend (`frontend`), the Document Ingestion service (`ingest-api`), and the interactive AI Agent, Search & Management service (`agent-api`), scaling on demand and scaling down to zero when idle. Each service is configured with health checks, explicit cross-origin security rules, and HTTP streaming support. — *See [`infra/cloud_run.tf`](infra/cloud_run.tf), [`src/ingest/app.py`](src/ingest/app.py), [`src/agent/app.py`](src/agent/app.py) & [`src/core/app_factory.py`](src/core/app_factory.py)*
  - **Container Packaging & Cloud Build Pipeline:** A unified Python container image powers the backend services (selecting `src.ingest.app:app` or `src.agent.app:app` via `APP_MODULE`), while an Nginx container serves the React frontend. Both images are built and pushed to Artifact Registry using **Google Cloud Build**. — *See [`docker/Dockerfile.backend`](docker/Dockerfile.backend), [`docker/Dockerfile.frontend`](docker/Dockerfile.frontend) & [`cloudbuild.yaml`](cloudbuild.yaml)*
  - **Least-Privilege Service Accounts:** Terraform provisions dedicated Google Cloud service accounts following least-privilege IAM principles—binding backend containers to a runtime service account granted access only to the project's storage bucket, Vertex AI models, logs, traces, and security filters, while separating frontend runtime and CI/CD deployment permissions. — *See [`infra/iam.tf`](infra/iam.tf)*
  - **Infrastructure & Deployment Automation:** Terraform declaratively provisions all required Google Cloud APIs, Artifact Registry repositories, and cloud resources using remote GCS state, while the root `Makefile` automates bootstrapping, image builds, and deployments (supporting both local Terraform CLI and Cloud Build execution). — *See [`infra/apis.tf`](infra/apis.tf), [`infra/versions.tf`](infra/versions.tf), [`infra/cloudbuild-terraform.yaml`](infra/cloudbuild-terraform.yaml) & [`Makefile`](Makefile)*

- **1.2 Zero-Trust User Authentication**
  - **Managed Sign-In Providers:** **Google Cloud Identity Platform (Firebase Authentication)** is provisioned to let users sign in with either **Google Sign-In (OAuth)** or **Email & Password**. — *See [`infra/auth.tf`](infra/auth.tf) & [`frontend/src/AuthGate.jsx`](frontend/src/AuthGate.jsx)*
  - **Automatic Token Attachment & Session Cleanup:** The web application attaches the signed-in user's authentication token to every API and streaming request, refreshes expired tokens automatically, and clears all local collections and chat state whenever a user signs out or switches accounts. — *See [`frontend/src/firebase.js`](frontend/src/firebase.js), [`frontend/src/api.js`](frontend/src/api.js) & [`frontend/src/App.jsx`](frontend/src/App.jsx)*
  - **Cryptographic Backend Verification:** Every backend endpoint verifies the incoming token signature against Google's public keys, validates that the extracted user ID contains only safe characters, and disables the local development authentication bypass whenever running in the cloud. — *See [`src/core/auth.py`](src/core/auth.py)*

- **1.3 Multi-Tenant Data & Conversation Isolation**
  - **Identity-Bound Tenant Context (`uid`):** A user's tenant scope is derived exclusively from the verified Firebase JWT `uid` (validated against a strict alphanumeric pattern to prevent path traversal)—never from client-supplied request parameters or headers. — *See [`src/core/auth.py`](src/core/auth.py)*
  - **Per-User Vector Storage & Collection Isolation:** Each user's ChromaDB files are isolated under a dedicated Cloud Storage prefix (`gs://<bucket>/users/{uid}/chroma_db/`) and an isolated local container directory (`/tmp/chroma_db/{uid}`). Two users can create, query, or delete collections with the exact same name without ever seeing or overwriting each other's documents. — *See [`src/core/storage/paths.py`](src/core/storage/paths.py) & [`src/manage/collections.py`](src/manage/collections.py)*
  - **Isolated Per-Request PDF Upload Staging:** In cloud deployments, the ingestion service accepts only multipart file uploads (blocking requests that reference local server file paths). Uploaded PDFs are staged in an isolated per-request folder (`/tmp/uploads/{uid}/{request_id}`), archived to the user's private Cloud Storage prefix (`gs://<bucket>/users/{uid}/uploads/`), and deleted from local disk as soon as indexing finishes. — *See [`src/ingest/upload_parser.py`](src/ingest/upload_parser.py) & [`src/core/storage/gcs_sync.py`](src/core/storage/gcs_sync.py)*
  - **Tenant-Namespaced Conversation Memory:** Every multi-turn chat thread is namespaced as `{uid}:{thread_id}` inside the in-memory LangGraph checkpointer so one user can never read, resume, or clear another user's conversation history. — *See [`src/agent/memory.py`](src/agent/memory.py), [`src/agent/routes.py`](src/agent/routes.py) & [`src/agent/stream.py`](src/agent/stream.py)*

- **1.4 Durable Cloud Storage $\leftrightarrow$ Container Memory Synchronization**
  - **Why We Synchronize Instead of Mounting Cloud Storage Directly:** ChromaDB relies on SQLite and memory-mapped index files, whereas Google Cloud Storage does not support the low-level file locks SQLite requires. Sharing a direct storage mount across containers would risk database lock errors and corrupted indexes, while storing data only in container memory would lose all collections whenever serverless instances scale down.
  - **Private Versioned Persistence Bucket:** Each user's authoritative vector database and uploaded PDFs are stored durably in a private, versioned **Google Cloud Storage** bucket with public access strictly blocked. — *See [`infra/storage.tf`](infra/storage.tf)*
  - **On-Demand Snapshot Synchronization & Version Tracking:** A lightweight `version.json` marker in Cloud Storage tracks whenever a user's vector database changes. Read requests download updated files into fast local `/tmp/chroma_db/{uid}` storage only when a newer version exists in Cloud Storage, while write requests (`POST /ingest` and `DELETE /collections/{name}`) push updated database files back to Cloud Storage and advance `version.json`. — *See [`src/core/storage/gcs_sync.py`](src/core/storage/gcs_sync.py) & [`src/core/storage/chroma.py`](src/core/storage/chroma.py)*
  - **Distributed Per-User Write Locks & Local Cache Eviction:** Before modifying a user's vector database, the backend acquires an atomic per-user lock object in Cloud Storage (with automatic TTL recovery if a previous container crashed) to prevent concurrent write conflicts across instances, and evicts the least-recently-used local user caches when `/tmp` storage fills up. — *See [`src/core/storage/locks.py`](src/core/storage/locks.py)*

- **1.5 Build-Once Frontend with Startup Runtime Configuration**
  - **Decoupling Image Builds from Cloud Provisioning:** Single-page web apps normally bake API URLs and Firebase keys into static JavaScript files during `docker build`, which happens *before* Terraform has provisioned the Load Balancer or generated the project's Firebase Web API key.
  - **Dynamic Startup Configuration (`/env-config.js`):** When the frontend container starts on Cloud Run, its startup script reads the environment variables injected by Terraform and writes `/env-config.js`, which the browser loads before starting the React app—allowing a single immutable frontend image to run in any environment without rebuilding. — *See [`docker/entrypoint-frontend.sh`](docker/entrypoint-frontend.sh), [`frontend/index.html`](frontend/index.html) & [`infra/cloud_run.tf`](infra/cloud_run.tf)*

---

### 2. Zero-Internet VPC & Network Security ([`docs/NETWORK_SECURITY_PLAN.md`](docs/NETWORK_SECURITY_PLAN.md))

All external user traffic enters through a single HTTPS Load Balancer, while backend containers run inside a segmented private network with zero outbound access to the public internet.

- **2.1 Single Public HTTPS Entry Point (`Global External Application Load Balancer`)**
  - **Automatic Free HTTPS Certificate:** Terraform reserves a global static IP address and constructs a free `<IP-with-dashes>.sslip.io` domain name so Google Cloud can automatically provision and renew a trusted SSL/TLS certificate (and Identity Platform can authorize the domain) without requiring you to buy a custom domain. Plain HTTP requests on port `80` are automatically redirected to HTTPS on port `443`. — *See [`infra/load_balancer.tf`](infra/load_balancer.tf), [`infra/auth.tf`](infra/auth.tf) & [`infra/outputs.tf`](infra/outputs.tf)*
  - **Single-Origin Path Routing:** The Load Balancer exposes the web interface and both backend APIs under the same HTTPS domain—routing `/api/ingest/*` to `ingest-api`, `/api/agent/*` to `agent-api`, and all other paths (`/*`) to the Frontend service—with edge CDN caching disabled so private API responses and live chat streams are never cached. — *See [`infra/load_balancer.tf`](infra/load_balancer.tf)*

- **2.2 Locked-Down Cloud Run Ingress**
  - **Blocking Direct Container Access:** All three Cloud Run services are configured to accept traffic only from the internal Load Balancer. Anyone attempting to visit an underlying `*.run.app` service URL directly from the internet is automatically blocked at Google's network edge before the request ever reaches a container. — *See [`infra/cloud_run.tf`](infra/cloud_run.tf)*

- **2.3 Segmented Private VPC with Zero Outbound Internet**
  - **Custom VPC Without an Internet Exit:** Backend services run inside a custom Virtual Private Cloud (VPC) that has its default route to the public internet removed. Even if a malicious file or prompt attempted to trick a container into sending data to an outside server, the network drops the connection immediately. — *See [`infra/network.tf`](infra/network.tf)*
  - **Workload-Segmented Subnets & Serverless Direct VPC Egress:** Both backend services route 100% of their outbound traffic into the private VPC using serverless **Cloud Run Direct VPC Egress** (`PRIVATE_RANGES_ONLY` with `0.0.0.0/0` removed, requiring no always-on NAT or proxy VMs). The Ingestion service and the Agent service are placed in separate private subnets (`ingest-subnet` and `agent-subnet`) to isolate document parsing traffic from agent execution at the network layer. — *See [`infra/network.tf`](infra/network.tf), [`infra/variables.tf`](infra/variables.tf) & [`infra/cloud_run.tf`](infra/cloud_run.tf)*

- **2.4 Private Internal Tunnels to Google Cloud APIs**
  - **Private Google Access for Standard Google APIs:** A private Cloud DNS zone for `googleapis.com` and a restricted internal VPC route (`199.36.153.8/30`) direct all backend calls to Cloud Storage, Vertex AI, Identity Platform, Cloud Logging, Cloud Trace, and Cloud DLP over Google's private internal backbone rather than the public internet. — *See [`infra/network.tf`](infra/network.tf)*
  - **Regional Private Service Connect (PSC) for Model Armor:** Because regional Model Armor endpoints use a dedicated regional domain (`modelarmor.<region>.rep.googleapis.com`), the VPC provisions a dedicated Private Service Connect endpoint and private DNS zone so AI safety screening traffic stays entirely on private internal IP addresses. — *See [`infra/network.tf`](infra/network.tf)*

---

### 3. AI-Specific Security — 5 Pillars ([`docs/AI_SECURITY_PLAN.md`](docs/AI_SECURITY_PLAN.md))

Every user question, retrieved document passage, and AI reply flows through a multi-stage security pipeline (`input_guard -> model_call <-> tools -> output_guard`) organized around five pillars:

- **3.1 Pillar 1 — Prompt Injection Mitigation**
  - **Front-Door Prompt Screening:** Before calling Gemini, the agent's `input_guard` node sends the user's question to **Google Cloud Model Armor** to detect prompt-injection attempts, jailbreaks, and malicious links. Unsafe prompts are blocked immediately with a polite refusal without spending LLM tokens. — *See [`infra/ai_security.tf`](infra/ai_security.tf), [`src/core/ai_security/armor.py`](src/core/ai_security/armor.py) & [`src/agent/graph.py`](src/agent/graph.py)*
  - **XML Spotlighting for Retrieved PDF Passages:** Because malicious instructions hidden inside a PDF could try to impersonate system commands, every retrieved document chunk is wrapped inside `<retrieved_chunk source="..." page="...">` tags with any internal `<` or `>` characters escaped first so a document can never break out of its quotation box, and the system prompt instructs Gemini to treat all chunk content strictly as untrusted reference data. — *See [`src/agent/tools.py`](src/agent/tools.py) & [`src/agent/graph.py`](src/agent/graph.py)*
  - **Safe Collection Naming:** Collection names are validated against a strict alphanumeric pattern across all endpoints so attackers cannot smuggle prompt instructions into retriever tool descriptions via collection names. — *See [`src/core/ai_security/guardrails.py`](src/core/ai_security/guardrails.py)*

- **3.2 Pillar 2 — Model Access Controls**
  - **Server-Approved Model Catalogs:** Instead of allowing callers to request arbitrary Vertex AI models, the backend maintains curated allowlists of approved chat models (`gemini-2.5-flash`, `gemini-2.5-pro`, `gemini-2.5-flash-lite`) and embedding models (`gemini-embedding-001`, `text-embedding-005`). — *See [`src/core/config.py`](src/core/config.py), [`infra/variables.tf`](infra/variables.tf) & [`infra/cloud_run.tf`](infra/cloud_run.tf)*
  - **Automatic Endpoint Validation:** Every ingestion, search, and chat request requires an explicit collection name, defaults to the primary approved model when omitted, and immediately rejects unapproved model names with `HTTP 422`. — *See [`src/core/ai_security/guardrails.py`](src/core/ai_security/guardrails.py), [`src/ingest/schemas.py`](src/ingest/schemas.py), [`src/ingest/upload_parser.py`](src/ingest/upload_parser.py), [`src/query/schemas.py`](src/query/schemas.py) & [`src/agent/schemas.py`](src/agent/schemas.py)*

- **3.3 Pillar 3 — AI & Ingestion Guardrails**
  - **Strict Document Grounding Guardrails:** The system prompt and retriever tool enforce anti-hallucination guardrails requiring the model to rely solely on `<retrieved_chunk>` evidence with 1-indexed `[filename.pdf, p.X]` citations and abstain whenever retrieved context is empty or insufficient. — *See [`src/agent/tools.py`](src/agent/tools.py) & [`src/agent/graph.py`](src/agent/graph.py)*
  - **Query & PDF Upload Ceilings:** Up-front validators enforce maximum query length (`4,000` characters), search result bounds (`1`–`50`), PDF file-header signature verification (`%PDF-`), path-traversal filename sanitization, maximum file size (`20 MB` per PDF), maximum files per upload (`10`), and maximum extracted chunks per ingestion (`1,000`). — *See [`src/core/ai_security/guardrails.py`](src/core/ai_security/guardrails.py), [`src/ingest/upload_parser.py`](src/ingest/upload_parser.py) & [`src/ingest/splitter.py`](src/ingest/splitter.py)*
  - **Bounded Agent Retrieval Loops:** The agent's tool-calling loop is capped (`6` steps by default) so ambiguous questions cannot trigger endless retrieval cycles, returning a clean fallback message if the step limit is reached. — *See [`src/core/ai_security/guardrails.py`](src/core/ai_security/guardrails.py) & [`src/agent/stream.py`](src/agent/stream.py)*

- **3.4 Pillar 4 — Output Filtering & Sanitized Errors**
  - **Automated PII & Secret Redaction Templates:** Terraform provisions **Google Cloud Sensitive Data Protection (Cloud DLP)** inspection and de-identification templates wired into **Model Armor** to automatically mask emails, phone numbers, credit card numbers, US Social Security numbers, IBAN codes, API keys, and auth tokens with `[INFO_TYPE]` placeholders (such as `[EMAIL_ADDRESS]`). — *See [`infra/ai_security.tf`](infra/ai_security.tf)*
  - **Two-Stage Redaction Across Search, JSON & Live Streaming:** Sensitive data is masked both when document passages are retrieved from ChromaDB (protecting direct `/query` searches and retriever tool traces) and again on the final AI answer in the `output_guard` node before responses are returned or streamed over SSE. — *See [`src/core/ai_security/armor.py`](src/core/ai_security/armor.py), [`src/query/service.py`](src/query/service.py), [`src/agent/tools.py`](src/agent/tools.py), [`src/agent/graph.py`](src/agent/graph.py) & [`src/agent/stream.py`](src/agent/stream.py)*
  - **Sanitized Server Error Messages:** Unexpected exceptions across all REST and streaming endpoints log full diagnostic stack traces privately on the server while returning a generic, safe error message to the client so internal details never leak. — *See [`src/core/ai_security/safety.py`](src/core/ai_security/safety.py), [`src/ingest/routes.py`](src/ingest/routes.py), [`src/query/routes.py`](src/query/routes.py), [`src/agent/routes.py`](src/agent/routes.py) & [`src/agent/stream.py`](src/agent/stream.py)*

- **3.5 Pillar 5 — Content Safety**
  - **Dual-Layer Harm Filtering (Model Armor RAI + Vertex AI Gemini):**
    1. **Google Cloud Model Armor Responsible AI (RAI) Filters** screen both incoming user prompts and outgoing model responses across Hate Speech, Harassment, Sexually Explicit, and Dangerous Content categories. — *See [`infra/ai_security.tf`](infra/ai_security.tf) & [`src/core/ai_security/armor.py`](src/core/ai_security/armor.py)*
    2. **Vertex AI Gemini Safety Thresholds** explicitly configure every Gemini model instance with pinned harm-blocking thresholds across all four safety categories. — *See [`src/core/ai_security/safety.py`](src/core/ai_security/safety.py) & [`src/agent/graph.py`](src/agent/graph.py)*
  - **Polite Refusal Fallbacks:** Whenever Model Armor or Gemini blocks a request or halts generation due to content safety rules, the backend replaces the blocked or empty output with a clear, polite refusal explanation so users never see a blank message bubble. — *See [`src/core/ai_security/safety.py`](src/core/ai_security/safety.py) & [`src/agent/graph.py`](src/agent/graph.py)*

---

### 4. Compliance, Data Privacy & Governance ([`docs/COMPLIANCE_GOVERNANCE_PLAN.md`](docs/COMPLIANCE_GOVERNANCE_PLAN.md))

Enterprise governance controls ensure that all data and AI calls stay within approved single-region boundaries, every layer of a request is auditable without exposing personal data, users have self-service privacy controls and AI transparency badges, and operational policies are enforced automatically:

- **4.1 Pillar 1 — Data Residency & Single-Region Pinning**
  - **Live Organization Policy Verification:** Allowed geographic regions are governed by the project's **Google Cloud Resource Location Restriction** Organization Policy (`constraints/gcp.resourceLocations`). Before any deployment runs, an automated check queries the live Organization Policy on the project and aborts if no location allowlist is active or if the target region is not permitted. — *See [`scripts/check_region_policy.py`](scripts/check_region_policy.py) & [`Makefile`](Makefile)*
  - **Infrastructure Single-Region Validation:** Terraform validates that the configured deployment location is a concrete single region (such as `us-central1` or `europe-west1`)—rejecting `"global"` and multi-regions (`"us"`, `"eu"`, `"asia"`)—and enforces regional preconditions on storage buckets, security templates, and private endpoints. — *See [`infra/variables.tf`](infra/variables.tf), [`infra/storage.tf`](infra/storage.tf), [`infra/ai_security.tf`](infra/ai_security.tf), [`infra/network.tf`](infra/network.tf) & [`infra/cloud_run.tf`](infra/cloud_run.tf)*
  - **Backend Regional Endpoint Binding:** At runtime, the Python backend validates that the configured location is a single region and explicitly binds all Vertex AI chat models, Vertex AI embedding models, and Model Armor clients to that regional endpoint. — *See [`src/core/compliance/residency.py`](src/core/compliance/residency.py), [`src/core/config.py`](src/core/config.py), [`src/core/storage/chroma.py`](src/core/storage/chroma.py), [`src/agent/graph.py`](src/agent/graph.py) & [`src/core/ai_security/armor.py`](src/core/ai_security/armor.py)*

- **4.2 Pillar 2 — Privacy-Safe Audit Logging, GCP Data Access Logs & Trace Correlation**
  - **End-to-End Request & Trace Correlation:** A correlation middleware assigns or forwards a unique request ID (`X-Request-Id`) on every call and links it with Google Cloud's trace context so administrators can filter by a single ID in **Logs Explorer** to trace a request across the Load Balancer, backend microservices, AI safety checks, and cloud storage operations. — *See [`src/core/context.py`](src/core/context.py), [`src/core/compliance/audit.py`](src/core/compliance/audit.py) & [`src/core/app_factory.py`](src/core/app_factory.py)*
  - **PII-Free Application Audit Stream:** Backend services publish structured JSON audit records to the `interactiverag-audit` stream in Google Cloud Logging for authentication failures, document uploads, searches, agent chats, collection changes, privacy requests, rate-limit blocks, and safety filter actions—replacing raw user questions with one-way SHA-256 hashes and character counts so personal data never leaks into logs. — *See [`src/core/compliance/audit.py`](src/core/compliance/audit.py), [`src/core/auth.py`](src/core/auth.py), [`src/ingest/routes.py`](src/ingest/routes.py), [`src/query/routes.py`](src/query/routes.py), [`src/agent/routes.py`](src/agent/routes.py) & [`src/agent/stream.py`](src/agent/stream.py)*
  - **GCP Data Access Audit Logs, Load Balancer HTTP Access Logs & 365-Day Regional Archive:** Terraform enables Google Cloud `ADMIN_READ`, `DATA_READ`, and `DATA_WRITE` Data Access Audit Logs for Cloud Storage, Vertex AI, Cloud DLP, and Model Armor, enables HTTP access logging on the HTTPS Load Balancer backends, and routes all three log streams (application audit events, GCP Data Access Audit Logs, and Load Balancer HTTP access logs) into a dedicated regional Cloud Logging bucket with 365-day retention. — *See [`infra/compliance.tf`](infra/compliance.tf) & [`infra/load_balancer.tf`](infra/load_balancer.tf)*

- **4.3 Pillar 3 — Regulatory Compliance (GDPR Self-Service Rights & EU AI Act Art. 50 Provenance)**
  - **GDPR Art. 15 Right of Access (`GET /compliance/my-data`):** Authenticated users can view a complete inventory of their stored collections, passage counts, uploaded PDF filenames, live and archived Cloud Storage object counts, total storage bytes, and active chat threads via the API or the **"My Data & Privacy"** modal in the web interface. — *See [`src/core/compliance/dsar.py`](src/core/compliance/dsar.py), [`src/manage/compliance.py`](src/manage/compliance.py) & [`frontend/src/App.jsx`](frontend/src/App.jsx)*
  - **GDPR Art. 17 Right to Erasure (`DELETE /compliance/my-data`) & Storage Hygiene:** Users can permanently erase all of their data in one action—deleting every live and archived object version in Cloud Storage, wiping local vector and upload caches, and clearing all conversation history from agent memory—complemented by Cloud Storage lifecycle rules that automatically prune archived object versions after 7 days. — *See [`src/core/compliance/dsar.py`](src/core/compliance/dsar.py), [`src/manage/compliance.py`](src/manage/compliance.py), [`src/agent/memory.py`](src/agent/memory.py) & [`infra/storage.tf`](infra/storage.tf)*
  - **EU AI Act Art. 50 AI Provenance & Transparency Badges:** Every `/agent` JSON response and `/agent/stream` SSE completion event includes structured provenance metadata and renders visual verification badges in the chat UI allowing the user to see:
    - **AI Generation, Model & Region:** Whether the reply was AI-generated or stopped by a guardrail, which exact **Gemini model** and **cloud region** produced it, and the correlating request ID.
    - **Citation Grounding:** Whether document grounding was enforced, whether the answer is backed by inline `[filename.pdf, p.X]` citations, and how many passages were retrieved.
    - **PII Redaction & Content Safety Activation:** Whether sensitive personal data (PII) was detected and redacted, whether Model Armor screening was active, which security filters ran, and whether any content safety or prompt-protection guardrails were triggered.
    — *See [`src/core/compliance/provenance.py`](src/core/compliance/provenance.py), [`src/core/compliance/state.py`](src/core/compliance/state.py), [`src/agent/schemas.py`](src/agent/schemas.py), [`src/agent/routes.py`](src/agent/routes.py), [`src/agent/stream.py`](src/agent/stream.py) & [`frontend/src/App.jsx`](frontend/src/App.jsx)*

- **4.4 Pillar 4 — Policy Enforcement (Fail-Closed Screening, Rate Limits, Quotas & Pre-Deploy Gates)**
  - **Fail-Closed Security Screening in Production:** In production deployments, if Google Cloud Model Armor or Cloud DLP is unreachable, misconfigured, or times out during prompt screening, chunk redaction, or output filtering, the backend immediately halts processing, logs a high-severity audit event, and returns `HTTP 503 Service Unavailable` so unscreened content is never exposed. — *See [`src/core/ai_security/armor.py`](src/core/ai_security/armor.py), [`src/core/config.py`](src/core/config.py), [`src/ingest/routes.py`](src/ingest/routes.py), [`src/query/routes.py`](src/query/routes.py), [`src/agent/routes.py`](src/agent/routes.py) & [`src/agent/stream.py`](src/agent/stream.py)*
  - **Per-User Rate Limits & Collection Quotas:** Sliding-window rate limiters protect each user account on document uploads (`10/minute` default) and queries (`30/minute` default), returning `HTTP 429 Too Many Requests` with a `Retry-After` header when exceeded. Each user is also capped at a maximum number of collections (`20` default), returning `HTTP 403 Forbidden` when attempting to create a new collection past the limit while still allowing uploads into existing collections. — *See [`src/core/compliance/policy.py`](src/core/compliance/policy.py), [`src/ingest/upload_parser.py`](src/ingest/upload_parser.py), [`src/ingest/routes.py`](src/ingest/routes.py), [`src/query/routes.py`](src/query/routes.py) & [`src/agent/routes.py`](src/agent/routes.py)*
  - **Production Deletion Protection & Pre-Deploy Policy Gate:** Terraform enables accidental deletion protection on Cloud Run services in production, and `make policy-check` automatically verifies live Organization Policy compliance, single-region settings, fail-closed screening, and Terraform validity before any `make plan` or `make deploy` runs. — *See [`infra/cloud_run.tf`](infra/cloud_run.tf), [`scripts/check_static_policies.py`](scripts/check_static_policies.py), [`scripts/check_region_policy.py`](scripts/check_region_policy.py) & [`Makefile`](Makefile)*

---

### 5. Observability & AI Monitoring ([`docs/OBSERVABILITY_PLAN.md`](docs/OBSERVABILITY_PLAN.md))

While compliance audit logging records *who* performed *what* action and *when* for regulatory accountability, the observability layer gives operators real-time visibility into *how fast*, *at what token cost*, and *with what retrieval and grounding quality* every request executed:

- **5.1 Pillar 1 — Structured Logging & Privacy-Safe Correlation**
  - **Dual-Mode Application Logging:** In Cloud Run (`K_SERVICE` set or `STRUCTURED_LOGGING="true"`), all operational logs are emitted to `stdout` as single-line JSON records formatted with Google Cloud's native `severity`, `message`, `timestamp`, `logging.googleapis.com/trace`, `logging.googleapis.com/spanId`, and `logging.googleapis.com/sourceLocation` fields, while preserving human-readable `coloredlogs` formatting for local terminal development. — *See [`src/core/observability/logging.py`](src/core/observability/logging.py), [`src/core/logger.py`](src/core/logger.py) & [`src/core/config.py`](src/core/config.py)*
  - **Neutral Request Context & Automatic Query Scrubbing:** A shared request correlation context (`src/core/context.py`) decouples compliance auditing from operational logging. Every log record is automatically enriched with `request_id`, `trace_id`, `span_id`, `service`, and `uid`, while any accidental raw query text is scrubbed into SHA-256 hashes and character counts so user prompts never leak into operational logs. — *See [`src/core/context.py`](src/core/context.py) & [`src/core/observability/logging.py`](src/core/observability/logging.py)*
  - **Dedicated Regional Observability Log Bucket:** Terraform provisions a regional Cloud Logging bucket (`observability_telemetry`, 30-day default retention) and routes both `interactiverag-telemetry` and Cloud Run container logs into it. — *See [`infra/observability.tf`](infra/observability.tf)*

- **5.2 Pillar 2 — Cloud Monitoring Metrics & AI Observability Dashboard**
  - **Zero-Overhead Log-Based Metrics (`google_logging_metric`):** Instead of adding synchronous monitoring API calls to the request hot path, Google Cloud's Log Router automatically extracts 10 log-based metrics directly from `interactiverag-telemetry` events: turn latency (`agent_turn_latency_ms`), streaming TTFT (`agent_ttft_ms`), Gemini vs. retrieval stage latencies (`agent_llm_stage_latency_ms`, `agent_retrieval_stage_latency_ms`), prompt/completion/total token usage (`agent_input_tokens`, `agent_output_tokens`, `agent_total_tokens`), retrieval similarity scores (`retrieval_mean_similarity_score`), ungrounded response counts (`agent_ungrounded_answers`), and ingested chunk counts (`ingest_chunks_added`). — *See [`infra/observability.tf`](infra/observability.tf)*
  - **AI Quality, Latency & Token Dashboard (`google_monitoring_dashboard`):** Terraform provisions a Cloud Monitoring dashboard charting `p50`/`p95` agent turn latency, `p50`/`p95` streaming TTFT, Gemini LLM vs. vector retrieval stage latencies, input/output/total token consumption by model, vector similarity score distributions, ungrounded/blocked response counts, document ingestion throughput, and HTTP request volume by response code class. — *See [`infra/observability.tf`](infra/observability.tf)*

- **5.3 Pillar 3 — Distributed Tracing & Stage-Level Latency Breakdown**
  - **Hierarchical Per-Stage Spans (`start_span`):** Every stage of document ingestion, vector search, and LangGraph agent execution—including Cloud Storage snapshot sync (`gcs_sync`), PDF splitting (`chunking`), Vertex AI embedding indexing (`embedding`), Model Armor input screening (`input_guard`), each Gemini reasoning call (`llm`), ChromaDB retriever tool execution (`retrieval`), and output screening (`output_guard`)—is timed as a child span with parent-child `span_id` linkage. — *See [`src/core/observability/tracing.py`](src/core/observability/tracing.py), [`src/core/storage/gcs_sync.py`](src/core/storage/gcs_sync.py), [`src/ingest/splitter.py`](src/ingest/splitter.py), [`src/ingest/service.py`](src/ingest/service.py), [`src/query/service.py`](src/query/service.py), [`src/agent/graph.py`](src/agent/graph.py) & [`src/agent/tools.py`](src/agent/tools.py)*
  - **Cloud Trace Export:** When `CLOUD_TRACE_ENABLED="true"` on Cloud Run, completed spans are exported to **Google Cloud Trace** so operators can inspect end-to-end request waterfalls alongside correlated logs. — *See [`src/core/observability/tracing.py`](src/core/observability/tracing.py) & [`infra/cloud_run.tf`](infra/cloud_run.tf)*

- **5.4 Pillar 4 — AI-Specific Monitoring (Latency, Token Usage & Retrieval Quality)**
  - **Turn Latency, Stage Breakdown & Streaming TTFT:** Every completed request (`document.ingest`, `vectorstore.query`, `agent.query`) emits a structured telemetry record to `interactiverag-telemetry` containing total wall-clock latency (`total_latency_ms`), per-stage millisecond rollups (`stage_latency_ms`), and—on `/agent/stream`—Time-to-First-Token (`ttft_ms`). — *See [`src/core/observability/telemetry.py`](src/core/observability/telemetry.py), [`src/core/observability/state.py`](src/core/observability/state.py) & [`src/agent/stream.py`](src/agent/stream.py)*
  - **Multi-Step Gemini Token Accounting:** Across all tool-calling iterations in a conversation turn, `record_llm_usage()` extracts prompt, completion, and total token counts from `AIMessage.usage_metadata` and accumulates `input_tokens`, `output_tokens`, `total_tokens`, and `llm_calls` per turn. — *See [`src/core/observability/telemetry.py`](src/core/observability/telemetry.py) & [`src/agent/graph.py`](src/agent/graph.py)*
  - **Deterministic Retrieval & Grounding Quality Signals:** Every search and agent turn records normalized ChromaDB similarity scores (`top_similarity_score` and `mean_similarity_score`), retriever tool call counts (`tool_calls_count`), retrieved passage counts (`retrieved_passages`), inline `[filename.pdf, p.X]` citation counts (`citation_count`), and grounding status (`grounded`) without making extra LLM judge calls. — *See [`src/core/observability/telemetry.py`](src/core/observability/telemetry.py), [`src/agent/tools.py`](src/agent/tools.py) & [`src/query/service.py`](src/query/service.py)*

---

### 6. Scalability & Elasticity ([`docs/SCALABILITY_ELASTICITY_PLAN.md`](docs/SCALABILITY_ELASTICITY_PLAN.md))

InteractiveRAG combines workload-isolated microservice packaging with horizontal, vertical, and storage scaling optimizations so heavy PDF uploads never degrade interactive user latency:

- **6.1 4-Package Modular Codebase Deployed Across 2 Backend Cloud Run Services**
  - **Dedicated Heavy-Write Service (`ingest-api`):** Mounts **only** `src/ingest` (`POST /ingest` + `GET /health`) with low concurrency (`4`) and `2 vCPU / 4Gi RAM` so CPU-intensive PDF parsing and Rust HNSW index construction never block interactive event loops. — *See [`src/ingest/app.py`](src/ingest/app.py) & [`src/ingest/routes.py`](src/ingest/routes.py)*
  - **Unified Interactive Read, Management & Agent Service (`agent-api`):** Mounts `src/manage` (`GET/DELETE /collections`, `GET/DELETE /compliance/my-data`), `src/query` (`POST /query`), and `src/agent` (`POST /agent`, `POST /agent/stream`) with high concurrency (`32`) and `2 vCPU / 2Gi RAM`, sharing a single warm `/tmp/chroma_db/{uid}` cache, a single `_QUERY_RATE_LIMITER`, and atomic GDPR inventory/erasure across Chroma and conversation memory. — *See [`src/agent/app.py`](src/agent/app.py), [`src/agent/routes.py`](src/agent/routes.py), [`src/query/routes.py`](src/query/routes.py) & [`src/manage/routes.py`](src/manage/routes.py)*
- **6.2 Horizontal, Vertical & Autoscaling Tuning**
  - **Explicit Per-Service Concurrency & Vertical Profiles:** Terraform enforces `max_instance_request_concurrency` (`4` on `ingest-api`, `32` on `agent-api`, `80` on `frontend`), provisions `4Gi` RAM on `ingest-api`, and scales `agent-api` up to `10` instances (`320` concurrent interactive requests). — *See [`infra/cloud_run.tf`](infra/cloud_run.tf) & [`infra/variables.tf`](infra/variables.tf)*
  - **`/24` Direct VPC Egress Subnets & Bounded `/tmp` `tmpfs` Eviction:** Both private VPC subnets (`10.10.1.0/24` and `10.10.2.0/24`) provide `251` usable IPs each for scale-out, while `CHROMA_TMP_MAX_BYTES=536870912` (`512 MiB`) and `CHROMA_TMP_MIN_FREE_BYTES=268435456` (`256 MiB`) protect Cloud Run's in-memory `/tmp` `tmpfs` from OOM kills. — *See [`infra/variables.tf`](infra/variables.tf), [`src/core/config.py`](src/core/config.py) & [`src/core/storage/locks.py`](src/core/storage/locks.py)*
- **6.3 Incremental MD5 GCS Delta Sync, Parallel Screening & Scaling Alerts**
  - **Parallel Retrieval Screening & Incremental MD5 Delta Push:** Retrieved passages are screened across a bounded `ThreadPoolExecutor` (`up to 8 worker threads, 1 passage per thread`), while `_push_user_dir_to_gcs` compares local file base64 MD5 digests against remote GCS blob `md5_hash` metadata to upload only new or modified files and delete removed files. — *See [`src/core/ai_security/armor.py`](src/core/ai_security/armor.py) & [`src/core/storage/gcs_sync.py`](src/core/storage/gcs_sync.py)*
  - **Scaling Dashboard & Saturation Alert Policies:** Terraform provisions Cloud Monitoring dashboard widgets for active instance counts, `p95` concurrency, `p95` CPU, and `p95` memory/`/tmp` utilization alongside automated alert policies for sustained agent turn latency (`> 15,000 ms` over `5m`), memory/`tmpfs` saturation (`> 85%` over `3m`), and `5xx` server error spikes (`> 5` over `2m`). — *See [`infra/observability.tf`](infra/observability.tf)*

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
│   │   ├── stream.py                   # Bounded recursion runner & live SSE event generator
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
