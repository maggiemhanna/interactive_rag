# InteractiveRAG

**InteractiveRAG** is a production-hardened, multi-tenant **Agentic Retrieval-Augmented Generation (RAG)** platform built with **LangGraph**, **Google Cloud Vertex AI**, **ChromaDB**, **FastAPI**, and **React**.

Instead of combining unrelated documents into a single shared database, InteractiveRAG gives every signed-in user an isolated workspace where they can organize PDFs into domain-specific vector collections (for example, separating annual financial reports from clinical research). Through the web dashboard or REST APIs, users can:

- **Ingest & Manage Document Collections (`POST /ingest`, `GET /collections`, `DELETE /collections/{name}`)**: Upload PDF documents that are validated for safety and size, split into overlapping text chunks, embedded using Vertex AI (`gemini-embedding-001` or `text-embedding-005`), and indexed into the user's private collection.
- **Run Direct Semantic Search (`POST /query`)**: Perform top-$k$ similarity searches scoped strictly to a selected collection to inspect relevant document passages and page numbers directly, with sensitive personal data automatically masked.
- **Chat with a Grounded AI Agent (`POST /agent`, `POST /agent/stream`)**: Ask questions in a multi-turn conversation with a stateful LangGraph agent (`gemini-2.5-flash`, `gemini-2.5-pro`, or `gemini-2.5-flash-lite`). The agent dynamically queries the user's chosen collection, grounds every factual statement in retrieved evidence with inline `[filename.pdf, p.X]` citations, screens prompts and responses for safety and sensitive data, attaches AI provenance metadata, and streams its reasoning steps in real time over Server-Sent Events (SSE).
- **Exercise Self-Service Privacy Controls (`GET /compliance/my-data`, `DELETE /compliance/my-data`)**: Export a complete inventory of stored collections, uploaded PDFs, storage bytes, and active conversation threads, or permanently erase all personal data—including live and archived Cloud Storage object versions, local vector caches, and conversation memory—in a single action.

---

## 🧠 Core Agentic RAG Architecture & Retrieval Pipeline

InteractiveRAG separates document ingestion from multi-turn agentic retrieval so documents are parsed, chunked, and indexed once into clean domain-specific collections and then queried dynamically by a tool-calling LangGraph agent:

### 1. PDF Document Parsing, Chunking & Indexing Strategy
- **Up-Front PDF Validation & Filename Sanitization:** Before any file is parsed, the ingestion pipeline strips directory path components from the uploaded filename, verifies that the file has a `.pdf` extension and starts with the `%PDF-` file header signature, and enforces limits on file size (`20 MB` per file), file count (`10` files per upload), and total extracted chunks (`1,000` chunks per upload). — *See [`src/ingestion/upload_parser.py`](src/ingestion/upload_parser.py), [`src/ingestion/splitter.py`](src/ingestion/splitter.py) & [`src/core/ai_security/guardrails.py`](src/core/ai_security/guardrails.py)*
- **Page-Aware PDF Parsing & Overlapping Text Chunking:** Uploaded PDFs are loaded page-by-page using `PyPDFLoader`—preserving the source document name and page number on every page—and split with `RecursiveCharacterTextSplitter` into `1,000`-character passages with a `200`-character sliding overlap. Splitting recursively along paragraph and sentence boundaries with a `200`-character overlap prevents key facts or tables at chunk boundaries from being cut in half. — *See [`src/ingestion/splitter.py`](src/ingestion/splitter.py)*
- **Batched Vector Indexing:** Extracted text chunks are embedded and written into the target collection in batches of `100` chunks per operation inside a per-user write lock, preventing memory spikes and embedding API rate-limit bursts during large multi-PDF uploads. — *See [`src/ingestion/service.py`](src/ingestion/service.py)*

### 2. Embedding Models & Domain-Scoped Vector Databases
- **Regional Vertex AI Embedding Models:** Document chunks (at ingestion time) and search queries (at retrieval time) are converted into dense semantic vectors using approved **Google Cloud Vertex AI** embedding models (`gemini-embedding-001` by default, or `text-embedding-005`). Every embedding client is explicitly pinned to the configured single cloud region so document text never leaves the region. — *See [`src/core/storage/chroma.py`](src/core/storage/chroma.py) & [`src/core/config.py`](src/core/config.py)*
- **Domain-Scoped ChromaDB Vector Collections:** Instead of pooling all of a user's files into a single flat vector index (which degrades retrieval accuracy when documents cover different topics), each user can organize documents into separate named **ChromaDB** collections backed by SQLite metadata storage and HNSW approximate nearest-neighbor vector indexes. Searches (`POST /query`) and agent conversations (`POST /agent`, `POST /agent/stream`) target one chosen collection at a time to keep retrieval focused and free of cross-topic noise. — *See [`src/core/storage/chroma.py`](src/core/storage/chroma.py), [`src/core/storage/routes.py`](src/core/storage/routes.py) & [`src/ingestion/service.py`](src/ingestion/service.py)*

### 3. Agentic RAG Architecture Pattern (Stateful LangGraph Pipeline)
- **Why Agentic RAG Instead of Fixed Single-Shot Retrieval:** Traditional RAG pipelines run a single similarity search using the user's raw prompt, which often struggles with conversational follow-up questions, multi-part questions, or queries that require rephrasing search terms. InteractiveRAG uses **LangGraph** to orchestrate a stateful, cyclic workflow (`START -> input_guard -> model_call <-> tools -> output_guard -> END`) powered by **Vertex AI Gemini** (`gemini-2.5-flash`, `gemini-2.5-pro`, or `gemini-2.5-flash-lite` at `temperature=0` for deterministic, factual responses). — *See [`src/agent/graph.py`](src/agent/graph.py)*
- **Dynamic Collection-Scoped Retriever Tool & Iterative Search:** On each request, the backend builds a structured retriever tool (`<collection>_retriever`, retrieving the top-$k=5$ most relevant passages per call) bound strictly to the user's selected collection. Gemini autonomously decides when to call the retriever tool, formulates targeted search queries based on the conversation history, inspects the returned passages, and can perform additional search steps (up to a bounded limit of `6` graph steps) if the initial passages are insufficient before synthesizing its final answer. — *See [`src/agent/tools.py`](src/agent/tools.py) & [`src/agent/stream.py`](src/agent/stream.py)*
- **Multi-Turn Conversation Memory & Real-Time Event Streaming:** Conversation history is persisted across turns using a tenant-namespaced in-memory checkpointer (`{uid}:{thread_id}`) so users can ask natural follow-up questions without repeating context. During execution, the streaming engine deduplicates graph state transitions and streams tool invocations, retrieved evidence, sanitized final answers, and compliance metadata in real time over Server-Sent Events (SSE). — *See [`src/agent/memory.py`](src/agent/memory.py), [`src/agent/stream.py`](src/agent/stream.py) & [`src/agent/app.py`](src/agent/app.py)*

### 4. Document Grounding, XML Spotlighting & Inline Page Citations
- **1-Indexed Page Normalization & XML Spotlighting:** PDF parsers number pages starting at `0`, which causes off-by-one confusion when cited directly. Before returning passages to the language model, the retriever tool masks sensitive PII, converts `0`-based page metadata into human-readable `1`-indexed page numbers (`p.1`, `p.2`, ...), escapes any `<` or `>` characters inside the passage text, and encloses each passage in an explicit XML block: `<retrieved_chunk source="report.pdf" page="4">...</retrieved_chunk>`. If no matching chunks are found, it returns `<retrieved_context status="empty">`. — *See [`src/agent/tools.py`](src/agent/tools.py)*
- **Mandatory Factual Grounding & Explicit Abstention:** The system prompt enforces a strict grounding contract: the agent must call the retriever tool before answering factual questions, base every factual claim *exclusively* on evidence inside `<retrieved_chunk>` tags rather than pretraining memory or assumptions, and clearly state that the answer cannot be found in the available documents whenever retrieved passages are empty or lack sufficient evidence. — *See [`src/agent/graph.py`](src/agent/graph.py)*
- **Inline `[source, p.X]` Citations & Grounding Verification:** Every factual statement in the assistant's answer must include an inline citation matching the exact `source` filename and `1`-indexed `page` attribute of the supporting `<retrieved_chunk>` tag (for example, `[annual_report.pdf, p.4]`). Once the answer is generated, the provenance module counts the retrieved passages and verified `[source, p.X]` inline citations, surfacing grounding badges directly in the web UI. — *See [`src/agent/graph.py`](src/agent/graph.py), [`src/core/compliance/provenance.py`](src/core/compliance/provenance.py) & [`frontend/src/App.jsx`](frontend/src/App.jsx)*

---

## 🏗️ Cloud Architecture & Defense-in-Depth Blueprints

InteractiveRAG is engineered for reproducible deployment on **Google Cloud Platform (GCP)** across four defense-in-depth layers documented in [`docs/`](docs/):

| Layer | Blueprint Document | Focus Area |
| :--- | :--- | :--- |
| **1. Cloud Deployment & Persistence** | [`docs/GCP_DEPLOYMENT_PLAN.md`](docs/GCP_DEPLOYMENT_PLAN.md) | Serverless Cloud Run microservices, Identity Platform authentication, strict multi-tenant isolation, Cloud Storage vector synchronization, and startup runtime frontend configuration |
| **2. Network Security** | [`docs/NETWORK_SECURITY_PLAN.md`](docs/NETWORK_SECURITY_PLAN.md) | Global External HTTPS Load Balancer (`sslip.io` TLS), locked-down Cloud Run ingress, zero-internet custom VPC, Private Google Access, and Regional Private Service Connect |
| **3. AI-Specific Security** | [`docs/AI_SECURITY_PLAN.md`](docs/AI_SECURITY_PLAN.md) | Prompt injection mitigation (Model Armor & XML spotlighting), model allowlists, grounding & upload guardrails, Cloud DLP redaction, and dual-layer content safety |
| **4. Compliance, Privacy & Governance** | [`docs/COMPLIANCE_GOVERNANCE_PLAN.md`](docs/COMPLIANCE_GOVERNANCE_PLAN.md) | Single-region data residency, end-to-end audit & data-access logging, GDPR self-service rights, EU AI Act provenance transparency, fail-closed screening, and rate/quota enforcement |

---

### 1. Cloud Deployment & Multi-Tenant Persistence ([`docs/GCP_DEPLOYMENT_PLAN.md`](docs/GCP_DEPLOYMENT_PLAN.md))

Multiple users can sign in and use the cloud platform simultaneously—each person gets their own isolated document collections and chat history, while serverless containers automatically scale down to zero when idle to save cost.

- **1.1 Serverless Microservice Architecture**
  - **Auto-Scaling Cloud Run Services:** The application runs as three independent **Google Cloud Run** services—the React web frontend, the Document Ingestion service, and the AI Agent service—that scale up on demand and scale down to zero when idle. Each service is configured with health checks, explicit cross-origin security rules, and real-time streaming support. — *See [`infra/cloud_run.tf`](infra/cloud_run.tf) & [`src/core/app_factory.py`](src/core/app_factory.py)*
  - **Container Packaging & Automated Builds:** A single unified Python container image powers both backend services, while an Nginx container serves the React frontend. Both images are built and pushed reproducibly using **Google Cloud Build**. — *See [`docker/Dockerfile.backend`](docker/Dockerfile.backend), [`docker/Dockerfile.frontend`](docker/Dockerfile.frontend) & [`cloudbuild.yaml`](cloudbuild.yaml)*
  - **Least-Privilege Service Accounts & Infrastructure Automation:** Terraform provisions all cloud APIs and binds the backend containers to a dedicated runtime service account that only has permission to access the application's storage bucket, AI models, logs, and security filters, while the `Makefile` automates deployments using a scoped deployment service account. — *See [`infra/iam.tf`](infra/iam.tf), [`infra/apis.tf`](infra/apis.tf) & [`Makefile`](Makefile)*

- **1.2 Zero-Trust User Authentication**
  - **Managed Sign-In Providers:** **Google Cloud Identity Platform (Firebase Authentication)** is provisioned to let users sign in with either **Google Sign-In (OAuth)** or **Email & Password**. — *See [`infra/auth.tf`](infra/auth.tf) & [`frontend/src/AuthGate.jsx`](frontend/src/AuthGate.jsx)*
  - **Automatic Token Attachment & Session Cleanup:** The web application attaches the signed-in user's authentication token to every API and streaming request, refreshes expired tokens automatically, and clears all local collections and chat state whenever a user signs out or switches accounts. — *See [`frontend/src/firebase.js`](frontend/src/firebase.js), [`frontend/src/api.js`](frontend/src/api.js) & [`frontend/src/App.jsx`](frontend/src/App.jsx)*
  - **Cryptographic Backend Verification:** Every backend endpoint verifies the incoming token signature against Google's public keys, validates that the extracted user ID contains only safe characters, and disables the local development authentication bypass whenever running in the cloud. — *See [`src/core/auth.py`](src/core/auth.py)*

- **1.3 Multi-Tenant Data & Conversation Isolation**
  - **Isolated Per-User Storage Paths:** A user's verified account ID—never a parameter supplied by the browser—determines which Cloud Storage folder and local container cache directory their request can access. Two users can create collections with the exact same name without ever seeing or overwriting each other's documents. — *See [`src/core/storage/paths.py`](src/core/storage/paths.py) & [`src/core/storage/routes.py`](src/core/storage/routes.py)*
  - **Safe Per-Request PDF Upload Staging:** In cloud deployments, the ingestion service accepts only direct PDF file uploads (blocking requests that reference local server file paths). Uploaded files are staged in an isolated temporary folder for that request, archived to the user's private Cloud Storage folder, and deleted from local disk as soon as indexing finishes. — *See [`src/ingestion/upload_parser.py`](src/ingestion/upload_parser.py) & [`src/ingestion/app.py`](src/ingestion/app.py)*
  - **Isolated Conversation Memory:** Every multi-turn chat session is namespaced by the user's verified account ID in agent memory so one user can never read, resume, or clear another user's conversation history. — *See [`src/agent/memory.py`](src/agent/memory.py), [`src/agent/stream.py`](src/agent/stream.py) & [`src/agent/app.py`](src/agent/app.py)*

- **1.4 Durable Cloud Storage $\leftrightarrow$ Container Memory Synchronization**
  - **Why We Synchronize Instead of Mounting Cloud Storage Directly:** ChromaDB relies on SQLite and memory-mapped index files, whereas Google Cloud Storage does not support the low-level file locks SQLite requires. Sharing a direct storage mount across the Ingestion and Agent services would risk database lock errors and corrupted indexes, while storing data only in container memory would lose all collections whenever containers scale down.
  - **Private Versioned Persistence Bucket:** Each user's authoritative vector database and uploaded PDFs are stored durably in a private, versioned **Google Cloud Storage** bucket with public access strictly blocked. — *See [`infra/storage.tf`](infra/storage.tf)*
  - **On-Demand Snapshot Sync & Version Tracking:** A lightweight version marker in Cloud Storage tracks whenever a user's vector database changes. Read requests (listing collections, searching, or chatting) download updated files into fast container memory only when a newer version exists in Cloud Storage, while write requests upload only the changed files and update the version marker so the Agent service immediately sees newly ingested documents. — *See [`src/core/storage/gcs_sync.py`](src/core/storage/gcs_sync.py) & [`src/core/storage/chroma.py`](src/core/storage/chroma.py)*
  - **Distributed Write Locks & Memory Eviction:** Before modifying a user's vector database, the backend acquires an atomic per-user lock in Cloud Storage (with automatic recovery if a previous container crashed) to prevent concurrent write conflicts, and automatically evicts the least-recently-used inactive user caches when local container memory runs low. — *See [`src/core/storage/locks.py`](src/core/storage/locks.py)*

- **1.5 Build-Once Frontend with Startup Runtime Configuration**
  - **Decoupling Image Builds from Cloud Provisioning:** Single-page web apps normally bake API URLs and Firebase keys into static JavaScript files during `docker build`, which happens *before* Terraform has provisioned the Load Balancer or generated the project's Firebase Web API key.
  - **Dynamic Startup Configuration (`/env-config.js`):** When the frontend container starts on Cloud Run, its startup script reads the environment variables injected by Terraform and writes `/env-config.js`, which the browser loads before starting the React app—allowing a single immutable frontend image to run in any environment without rebuilding. — *See [`docker/entrypoint-frontend.sh`](docker/entrypoint-frontend.sh), [`frontend/index.html`](frontend/index.html) & [`infra/cloud_run.tf`](infra/cloud_run.tf)*

---

### 2. Zero-Internet VPC & Network Security ([`docs/NETWORK_SECURITY_PLAN.md`](docs/NETWORK_SECURITY_PLAN.md))

All external user traffic enters through a single HTTPS Load Balancer, while backend containers run inside a segmented private network with zero outbound access to the public internet.

- **2.1 Single Public HTTPS Entry Point (`Global External Application Load Balancer`)**
  - **Automatic Free HTTPS Certificate:** Terraform reserves a global static IP address and constructs a free `<IP-with-dashes>.sslip.io` domain name so Google Cloud can automatically provision and renew a trusted SSL/TLS certificate (and Identity Platform can authorize the domain) without requiring you to buy a custom domain. Plain HTTP requests on port `80` are automatically redirected to HTTPS on port `443`. — *See [`infra/load_balancer.tf`](infra/load_balancer.tf), [`infra/auth.tf`](infra/auth.tf) & [`infra/outputs.tf`](infra/outputs.tf)*
  - **Single-Origin Path Routing:** The Load Balancer exposes the web interface and both backend APIs under the same HTTPS domain—routing `/api/ingest/*` to the Ingestion service, `/api/agent/*` to the Agent service, and all other paths (`/*`) to the Frontend service—with edge CDN caching disabled so private API responses and live chat streams are never cached. — *See [`infra/load_balancer.tf`](infra/load_balancer.tf)*

- **2.2 Locked-Down Cloud Run Ingress**
  - **Blocking Direct Container Access:** All three Cloud Run services are configured to accept traffic only from the internal Load Balancer. Anyone attempting to visit an underlying `*.run.app` service URL directly from the internet is automatically blocked at Google's network edge before the request ever reaches a container. — *See [`infra/cloud_run.tf`](infra/cloud_run.tf)*

- **2.3 Segmented Private VPC with Zero Outbound Internet**
  - **Custom VPC Without an Internet Exit:** Backend services run inside a custom Virtual Private Cloud (VPC) that has its default route to the public internet removed. Even if a malicious file or prompt attempted to trick a container into sending data to an outside server, the network drops the connection immediately. — *See [`infra/network.tf`](infra/network.tf)*
  - **Workload-Isolated Subnets & Serverless Egress:** Both backend services route 100% of their outbound traffic into the private VPC using serverless **Cloud Run Direct VPC Egress** (which requires no always-on proxy VMs and costs `$0/month` when idle). The PDF Ingestion service and the AI Agent service are placed in separate private subnets to isolate document parsing from agent execution, with third-party library telemetry disabled. — *See [`infra/network.tf`](infra/network.tf), [`infra/variables.tf`](infra/variables.tf) & [`infra/cloud_run.tf`](infra/cloud_run.tf)*

- **2.4 Private Internal Tunnels to Google Cloud APIs**
  - **Private Google Access for Standard Google APIs:** A private Cloud DNS zone for `googleapis.com` and a restricted internal VPC route direct all backend calls to Cloud Storage, Vertex AI, Identity Platform, Cloud Logging, and Cloud DLP over Google's private internal backbone rather than the public internet. — *See [`infra/network.tf`](infra/network.tf)*
  - **Regional Private Service Connect (PSC) for Model Armor:** Because regional Model Armor endpoints use a dedicated regional domain (`modelarmor.<region>.rep.googleapis.com`), the VPC provisions a dedicated Private Service Connect endpoint and private DNS zone so AI safety screening traffic stays entirely on private internal IP addresses. — *See [`infra/network.tf`](infra/network.tf)*

---

### 3. AI-Specific Security — 5 Pillars ([`docs/AI_SECURITY_PLAN.md`](docs/AI_SECURITY_PLAN.md))

Every user question, retrieved document passage, and AI reply flows through a multi-stage security pipeline (`input_guard -> model_call <-> tools -> output_guard`) organized around five pillars:

- **3.1 Pillar 1 — Prompt Injection Mitigation**
  - **Front-Door Prompt Screening:** Before calling Gemini, the agent's input guard sends the user's question to **Google Cloud Model Armor** to detect prompt-injection attempts, jailbreaks, and malicious links. Unsafe prompts are blocked immediately with a polite refusal without spending LLM tokens. — *See [`infra/ai_security.tf`](infra/ai_security.tf), [`src/core/ai_security/armor.py`](src/core/ai_security/armor.py) & [`src/agent/graph.py`](src/agent/graph.py)*
  - **XML Spotlighting for Retrieved PDF Passages:** Because malicious instructions hidden inside a PDF could try to impersonate system commands, every retrieved document chunk is wrapped inside `<retrieved_chunk source="..." page="...">` tags with any internal `<` or `>` characters escaped first so a document can never break out of its quotation box, and the system prompt instructs Gemini to treat all chunk content strictly as untrusted reference data. — *See [`src/agent/tools.py`](src/agent/tools.py) & [`src/agent/graph.py`](src/agent/graph.py)*
  - **Safe Collection Naming:** Collection names are validated against a strict alphanumeric pattern across all endpoints so attackers cannot smuggle prompt instructions into retriever tool descriptions via collection names. — *See [`src/core/ai_security/guardrails.py`](src/core/ai_security/guardrails.py)*

- **3.2 Pillar 2 — Model Access Controls**
  - **Server-Approved Model Catalogs:** Instead of allowing callers to request arbitrary Vertex AI models, the backend maintains curated allowlists of approved chat models (`gemini-2.5-flash`, `gemini-2.5-pro`, `gemini-2.5-flash-lite`) and embedding models (`gemini-embedding-001`, `text-embedding-005`). — *See [`src/core/config.py`](src/core/config.py), [`infra/variables.tf`](infra/variables.tf) & [`infra/cloud_run.tf`](infra/cloud_run.tf)*
  - **Automatic Endpoint Validation:** Every ingestion, search, and chat request requires an explicit collection name, defaults to the primary approved model when omitted, and immediately rejects unapproved model names with `HTTP 422`. — *See [`src/core/ai_security/guardrails.py`](src/core/ai_security/guardrails.py), [`src/ingestion/schemas.py`](src/ingestion/schemas.py), [`src/ingestion/upload_parser.py`](src/ingestion/upload_parser.py) & [`src/agent/schemas.py`](src/agent/schemas.py)*

- **3.3 Pillar 3 — AI & Ingestion Guardrails**
  - **Strict Document Grounding & 1-Indexed Page Citations:** The retriever converts 0-based PDF page metadata into human-readable 1-based page numbers (`p.1`, `p.2`, ...), and the system prompt requires the agent to ground every factual claim in retrieved passages with inline `[filename.pdf, p.X]` citations—or explicitly state when the documents do not contain the answer rather than guessing from pretraining knowledge. — *See [`src/agent/tools.py`](src/agent/tools.py) & [`src/agent/graph.py`](src/agent/graph.py)*
  - **Query & PDF Upload Ceilings:** Up-front validators enforce maximum query length (`4,000` characters), search result bounds (`1`–`50`), PDF file-header signature verification (`%PDF-`), maximum file size (`20 MB` per PDF), maximum files per upload (`10`), and maximum extracted chunks per ingestion (`1,000`). — *See [`src/core/ai_security/guardrails.py`](src/core/ai_security/guardrails.py), [`src/ingestion/upload_parser.py`](src/ingestion/upload_parser.py) & [`src/ingestion/splitter.py`](src/ingestion/splitter.py)*
  - **Bounded Agent Retrieval Loops:** The agent's tool-calling loop is capped (`6` steps by default) so ambiguous questions cannot trigger endless retrieval cycles, returning a clean fallback message if the step limit is reached. — *See [`src/core/ai_security/guardrails.py`](src/core/ai_security/guardrails.py) & [`src/agent/stream.py`](src/agent/stream.py)*

- **3.4 Pillar 4 — Output Filtering & Sanitized Errors**
  - **Automated PII & Secret Redaction Templates:** Terraform provisions **Google Cloud Sensitive Data Protection (Cloud DLP)** inspection and de-identification templates wired into **Model Armor** to automatically mask emails, phone numbers, credit card numbers, US Social Security numbers, IBAN codes, API keys, and auth tokens with `[INFO_TYPE]` placeholders (such as `[EMAIL_ADDRESS]`). — *See [`infra/ai_security.tf`](infra/ai_security.tf)*
  - **Two-Stage Redaction Across Search, JSON & Live Streaming:** Sensitive data is masked both when document passages are retrieved from ChromaDB (protecting direct `/query` searches and retriever tool traces) and again on the final AI answer in the output guard node before responses are returned or streamed over SSE. — *See [`src/core/ai_security/armor.py`](src/core/ai_security/armor.py), [`src/ingestion/service.py`](src/ingestion/service.py), [`src/agent/tools.py`](src/agent/tools.py), [`src/agent/graph.py`](src/agent/graph.py) & [`src/agent/stream.py`](src/agent/stream.py)*
  - **Sanitized Server Error Messages:** Unexpected exceptions across all REST and streaming endpoints log full diagnostic stack traces privately on the server while returning a generic, safe error message to the client so internal details never leak. — *See [`src/core/ai_security/safety.py`](src/core/ai_security/safety.py), [`src/ingestion/app.py`](src/ingestion/app.py), [`src/agent/app.py`](src/agent/app.py) & [`src/agent/stream.py`](src/agent/stream.py)*

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
  - **PII-Free Application Audit Stream:** Backend services publish structured JSON audit records to the `interactiverag-audit` stream in Google Cloud Logging for authentication failures, document uploads, searches, agent chats, collection changes, privacy requests, rate-limit blocks, and safety filter actions—replacing raw user questions with one-way SHA-256 hashes and character counts so personal data never leaks into logs. — *See [`src/core/compliance/audit.py`](src/core/compliance/audit.py), [`src/core/auth.py`](src/core/auth.py), [`src/ingestion/app.py`](src/ingestion/app.py), [`src/agent/app.py`](src/agent/app.py) & [`src/agent/stream.py`](src/agent/stream.py)*
  - **GCP Data Access Audit Logs, Load Balancer HTTP Access Logs & 365-Day Regional Archive:** Terraform enables Google Cloud `ADMIN_READ`, `DATA_READ`, and `DATA_WRITE` Data Access Audit Logs for Cloud Storage, Vertex AI, Cloud DLP, and Model Armor, enables HTTP access logging on the HTTPS Load Balancer backends, and routes all three log streams (application audit events, GCP Data Access Audit Logs, and Load Balancer HTTP access logs) into a dedicated regional Cloud Logging bucket with 365-day retention. — *See [`infra/compliance.tf`](infra/compliance.tf) & [`infra/load_balancer.tf`](infra/load_balancer.tf)*

- **4.3 Pillar 3 — Regulatory Compliance (GDPR Self-Service Rights & EU AI Act Art. 50 Provenance)**
  - **GDPR Art. 15 Right of Access (`GET /compliance/my-data`):** Authenticated users can view a complete inventory of their stored collections, passage counts, uploaded PDF filenames, live and archived Cloud Storage object counts, total storage bytes, and active chat threads via the API or the **"My Data & Privacy"** modal in the web interface. — *See [`src/core/compliance/dsar.py`](src/core/compliance/dsar.py), [`src/core/compliance/routes.py`](src/core/compliance/routes.py) & [`frontend/src/App.jsx`](frontend/src/App.jsx)*
  - **GDPR Art. 17 Right to Erasure (`DELETE /compliance/my-data`) & Storage Hygiene:** Users can permanently erase all of their data in one action—deleting every live and archived object version in Cloud Storage, wiping local vector and upload caches, and clearing all conversation history from agent memory—complemented by Cloud Storage lifecycle rules that automatically prune archived object versions after 7 days. — *See [`src/core/compliance/dsar.py`](src/core/compliance/dsar.py), [`src/core/compliance/routes.py`](src/core/compliance/routes.py), [`src/agent/memory.py`](src/agent/memory.py) & [`infra/storage.tf`](infra/storage.tf)*
  - **EU AI Act Art. 50 AI Provenance & Transparency Badges:** Every `/agent` JSON response and `/agent/stream` SSE completion event includes structured provenance metadata and renders visual verification badges in the chat UI allowing the user to see:
    - **AI Generation, Model & Region:** Whether the reply was AI-generated or stopped by a guardrail, which exact **Gemini model** and **cloud region** produced it, and the correlating request ID.
    - **Citation Grounding:** Whether document grounding was enforced, whether the answer is backed by inline `[filename.pdf, p.X]` citations, and how many passages were retrieved.
    - **PII Redaction & Content Safety Activation:** Whether sensitive personal data (PII) was detected and redacted, whether Model Armor screening was active, which security filters ran, and whether any content safety or prompt-protection guardrails were triggered.
    — *See [`src/core/compliance/provenance.py`](src/core/compliance/provenance.py), [`src/core/compliance/state.py`](src/core/compliance/state.py), [`src/agent/schemas.py`](src/agent/schemas.py), [`src/agent/app.py`](src/agent/app.py), [`src/agent/stream.py`](src/agent/stream.py) & [`frontend/src/App.jsx`](frontend/src/App.jsx)*

- **4.4 Pillar 4 — Policy Enforcement (Fail-Closed Screening, Rate Limits, Quotas & Pre-Deploy Gates)**
  - **Fail-Closed Security Screening in Production:** In production deployments, if Google Cloud Model Armor or Cloud DLP is unreachable, misconfigured, or times out during prompt screening, chunk redaction, or output filtering, the backend immediately halts processing, logs a high-severity audit event, and returns `HTTP 503 Service Unavailable` so unscreened content is never exposed. — *See [`src/core/ai_security/armor.py`](src/core/ai_security/armor.py), [`src/core/config.py`](src/core/config.py), [`src/ingestion/app.py`](src/ingestion/app.py), [`src/agent/app.py`](src/agent/app.py) & [`src/agent/stream.py`](src/agent/stream.py)*
  - **Per-User Rate Limits & Collection Quotas:** Sliding-window rate limiters protect each user account on document uploads (`10/minute` default) and queries (`30/minute` default), returning `HTTP 429 Too Many Requests` with a `Retry-After` header when exceeded. Each user is also capped at a maximum number of collections (`20` default), returning `HTTP 403 Forbidden` when attempting to create a new collection past the limit while still allowing uploads into existing collections. — *See [`src/core/compliance/policy.py`](src/core/compliance/policy.py), [`src/ingestion/upload_parser.py`](src/ingestion/upload_parser.py), [`src/ingestion/app.py`](src/ingestion/app.py) & [`src/agent/app.py`](src/agent/app.py)*
  - **Production Deletion Protection & Pre-Deploy Policy Gate:** Terraform enables accidental deletion protection on Cloud Run services in production, and `make policy-check` automatically verifies live Organization Policy compliance, single-region settings, fail-closed screening, and Terraform validity before any `make plan` or `make deploy` runs. — *See [`infra/cloud_run.tf`](infra/cloud_run.tf), [`scripts/check_static_policies.py`](scripts/check_static_policies.py), [`scripts/check_region_policy.py`](scripts/check_region_policy.py) & [`Makefile`](Makefile)*

---

## 📁 Repository Structure

```text
InteractiveRAG/
├── src/                                # Unified Python backend packages
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
│   │   │   ├── routes.py               # Self-service privacy endpoints (GET/DELETE /compliance/my-data)
│   │   │   └── state.py                # Per-request compliance guardrail & retrieval state helpers
│   │   ├── observability/              # Observability & AI monitoring package
│   │   │   ├── logging.py              # Structured GCP JSON formatter & request correlation filter
│   │   │   ├── state.py                # Per-request latency, TTFT, token, score & span state accumulators
│   │   │   └── tracing.py              # Hierarchical per-stage span tracer & Cloud Trace exporter
│   │   ├── storage/                    # Multi-tenant ChromaDB & Cloud Storage persistence
│   │   │   ├── chroma.py               # Regional Vertex AI embeddings & ChromaDB client factory
│   │   │   ├── gcs_sync.py             # Per-user GCS <-> /tmp sync, version markers & PDF archive
│   │   │   ├── locks.py                # Atomic GCS distributed write locks & LRU cache eviction
│   │   │   ├── paths.py                # Tenant-isolated path helpers & client cache release
│   │   │   └── routes.py               # Shared collection management routes (/collections)
│   │   ├── app_factory.py              # Shared FastAPI app factory, CORS, middleware & /health
│   │   ├── auth.py                     # Identity Platform / Firebase Bearer JWT verification
│   │   ├── config.py                   # Approved model catalogs, guardrail limits & settings
│   │   ├── context.py                  # Shared RequestContext & RequestCorrelationMiddleware
│   │   └── logger.py                   # Application logging setup
│   ├── ingestion/                      # Document Ingestion & Semantic Search API (Port 8000)
│   │   ├── app.py                      # FastAPI endpoints (/ingest, /query, /collections, /compliance/my-data)
│   │   ├── schemas.py                  # Validated IngestRequest & QueryRequest models
│   │   ├── service.py                  # Batched ChromaDB indexing & DLP-sanitized similarity search
│   │   ├── splitter.py                 # PDF page loading & overlapping text chunking
│   │   └── upload_parser.py            # Multipart PDF upload staging, quota & magic-byte checks
│   ├── agent/                          # LangGraph Agentic RAG & SSE Streaming API (Port 8001)
│   │   ├── app.py                      # FastAPI endpoints (/agent, /agent/stream, /collections, /compliance/my-data)
│   │   ├── display.py                  # Terminal message formatting helpers
│   │   ├── graph.py                    # Hardened StateGraph (input_guard -> model_call <-> tools -> output_guard)
│   │   ├── memory.py                   # Tenant-isolated conversation checkpoint memory
│   │   ├── schemas.py                  # Validated AgentRequest, AgentState & ComplianceMetadata schemas
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
│   ├── network.tf                      # Zero-internet VPC, subnets, Private Google Access & PSC
│   ├── load_balancer.tf                # Global HTTPS Load Balancer, NEGs & managed sslip.io cert
│   ├── ai_security.tf                  # Cloud DLP inspect/de-identify templates & Model Armor
│   ├── cloud_run.tf                    # Frontend, Ingestion API & Agent API Cloud Run services
│   ├── compliance.tf                   # Cloud Audit Logs, 365-day regional audit bucket & log sink
│   ├── variables.tf                    # Parameterized infrastructure, security & compliance settings
│   ├── outputs.tf                      # Deployed URLs, IPs, bucket names & template identifiers
│   ├── terraform.tfvars.example        # Template for environment-specific variable values
│   └── cloudbuild-terraform.yaml       # Cloud Build pipeline for remote Terraform execution
├── scripts/                            # Automated pre-deployment compliance & policy scripts
│   ├── check_region_policy.py          # Verifies target region against live GCP Org Policy
│   └── check_static_policies.py        # Offline verification of Terraform & runtime security policies
├── docs/                               # Architecture & security blueprints
│   ├── GCP_DEPLOYMENT_PLAN.md          # Layer 1: Cloud Deployment & Multi-Tenant Persistence
│   ├── NETWORK_SECURITY_PLAN.md        # Layer 2: Zero-Internet VPC & Network Security
│   ├── AI_SECURITY_PLAN.md             # Layer 3: AI-Specific Security (5 Pillars)
│   └── COMPLIANCE_GOVERNANCE_PLAN.md   # Layer 4: Compliance, Data Privacy & Governance
├── tests/                              # Automated unit suites & live smoke scripts
│   ├── auth.py                         # Token verification, tenant UID validation & bypass tests
│   ├── storage.py                      # Per-user GCS sync, distributed lock & LRU cache tests
│   ├── multi_tenant_api.py             # Cross-tenant isolation tests across REST & SSE APIs
│   ├── ai_security.py                  # Unit tests for all 5 AI security pillars
│   ├── compliance.py                   # Unit tests for residency, audit logs, DSAR, provenance & quotas
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

# Optional AI security, rate-limit & quota overrides (defaults shown):
# ALLOWED_LLM_MODELS="gemini-2.5-flash,gemini-2.5-pro,gemini-2.5-flash-lite"
# ALLOWED_EMBEDDING_MODELS="gemini-embedding-001,text-embedding-005"
# GEMINI_SAFETY_THRESHOLD="BLOCK_LOW_AND_ABOVE"
# MODEL_ARMOR_TEMPLATE_ID=""
# RATE_LIMIT_INGEST_PER_MINUTE="10"
# RATE_LIMIT_QUERY_PER_MINUTE="30"
# MAX_COLLECTIONS_PER_USER="20"
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

1. **Document Ingestion API (Port 8000)**
   ```bash
   python -m src.ingestion
   ```
2. **Agentic RAG API (Port 8001)**
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

### 1. Offline Unit, Multi-Tenant, AI Security & Compliance Test Suites
Run the full offline test suite and static policy verifier (no live cloud resources required):
```bash
make check-static-policy
python -m unittest tests.auth tests.storage tests.multi_tenant_api tests.ai_security tests.compliance -v
```

### 2. Live Local Smoke Scripts
With `src.ingestion` (port `8000`) and `src.agent` (port `8001`) running locally:
```bash
# Ingest sample PDFs into ChromaDB and verify similarity search
python -m tests.ingestion

# Test the Agentic RAG pipeline in both standard JSON and live SSE streaming modes
python -m tests.agent
```

### 3. Manual API Testing via `curl`
For local requests with `AUTH_DISABLED=true`, call `http://localhost:8000` and `http://localhost:8001` directly. When calling the deployed Load Balancer (`https://<LB-HOST>`), prefix paths with `/api/ingest` or `/api/agent` and include `-H "Authorization: Bearer <ID_TOKEN>"`:

```bash
# 1. Upload & index a PDF into a collection
curl -X POST "http://localhost:8000/ingest" \
  -F "files=@./data/stock-market-performance/Stock_Market_Performance_2024.pdf" \
  -F "collection_name=stock_market_vs" \
  -F "embedding_model=gemini-embedding-001"

# 2. Run direct top-k semantic similarity search
curl -X POST "http://localhost:8000/query" \
  -H "Content-Type: application/json" \
  -d '{
    "query": "What is ARM Holdings?",
    "collection_name": "stock_market_vs",
    "k": 3
  }'

# 3. Ask the Agentic RAG assistant over a live SSE stream
curl -N -X POST "http://localhost:8001/agent/stream" \
  -H "Content-Type: application/json" \
  -d '{
    "query": "What was ARM Holdings financial performance in 2024?",
    "collection_name": "stock_market_vs",
    "model_name": "gemini-2.5-flash"
  }'

# 4. Inspect your personal data inventory (GDPR Art. 15 Right of Access)
curl -s "http://localhost:8000/compliance/my-data" | python3 -m json.tool
```

### 4. Interactive Web Dashboard Workflow
1. Open **http://localhost:3000** (or your deployed `https://<LB-IP-with-dashes>.sslip.io` URL and sign in via **Identity Platform**).
2. **Ingest PDFs**: Upload one or more PDF files into a named collection using an approved embedding model.
3. **Manage Collections & Privacy**: View your isolated collections and passage counts, delete collections you no longer need, or open **"My Data & Privacy"** to inspect your full data footprint or erase all account data.
4. **Chat & Verify Provenance Badges**: Select an approved Gemini model and ask questions in the chat panel to watch real-time retriever tool calls, grounded `[filename.pdf, p.X]` citations, DLP-filtered responses, and EU AI Act transparency badges.
