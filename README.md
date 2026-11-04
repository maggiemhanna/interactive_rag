# InteractiveRAG

**InteractiveRAG** is a production-hardened, multi-tenant **Agentic Retrieval-Augmented Generation (RAG)** platform built with **LangGraph**, **Google Cloud Vertex AI**, **ChromaDB**, **FastAPI**, and **React**.

Instead of combining unrelated documents into a single shared database, InteractiveRAG gives every signed-in user an isolated workspace where they can organize PDFs into domain-specific vector collections (for example, separating annual financial reports from clinical research). Through the web dashboard or REST APIs, users can:

- **Ingest PDF Documents (`POST /ingest`)**: Upload PDF documents that are parsed page-by-page, split into overlapping text chunks, converted into vector embeddings using Vertex AI (`gemini-embedding-001`, `text-embedding-004`, or `text-embedding-005`), and indexed into a named vector collection bound to that embedding model.
- **Run Direct Hybrid Search (`POST /query`)**: Perform top-$k$ hybrid searches combining dense Vertex AI vector similarity with sparse BM25 lexical matching via Reciprocal Rank Fusion (RRF), scoped strictly to a selected collection (automatically using the collection's bound embedding model) to inspect relevant document chunks, normalized similarity scores, and page numbers directly.
- **Chat with a Grounded AI Agent (`POST /agent`, `POST /agent/stream`)**: Ask questions in a multi-turn conversation with a stateful LangGraph agent that dynamically queries the user's chosen collection using its bound embedding model, grounds every factual statement in retrieved evidence with inline `[filename.pdf, p.X]` citations, and streams each workflow step in real time over Server-Sent Events (SSE).
- **Manage Document Collections (`GET /collections`, `DELETE /collections/{collection_name}`)**: List all vector collections in the user's workspace along with their indexed chunk counts, bound `embedding_model`, and server-approved model catalogs (`allowed_llm_models`, `allowed_embedding_models`), or delete a collection when it is no longer needed.
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
- **Overlapping Recursive Text Chunking (`RecursiveCharacterTextSplitter`):** Extracted pages are split into `1,000`-character chunks with a `200`-character sliding overlap along paragraph and sentence boundaries so key facts or tables at chunk boundaries are never cut in half.  
  — *Implemented in [`src/ingest/splitter.py`](src/ingest/splitter.py)*  
  — *Called in [`src/ingest/service.py`](src/ingest/service.py)*
- **Sequential Batched Vector Indexing:** Extracted text chunks are embedded and written into the target ChromaDB collection sequentially in batches of `100` chunks per operation so multi-PDF uploads are indexed reliably.  
  — *Implemented in [`src/ingest/service.py`](src/ingest/service.py)*  
  — *Called in [`src/ingest/routes.py`](src/ingest/routes.py)*

### 2. Embedding Models, Hybrid Search & Domain-Scoped Vector Databases
- **Vertex AI Dense Vector Embeddings & Collection-Bound Embedding Consistency:** Document chunks (at ingestion time) and search queries (at retrieval time) are converted into dense semantic vectors using **Google Cloud Vertex AI** embedding models (`gemini-embedding-001` by default, `text-embedding-004`, or `text-embedding-005`). When a collection is first created, its chosen `embedding_model` is persisted in ChromaDB collection metadata (`resolve_collection_embedding_model`); all subsequent document appends (`POST /ingest`), direct similarity searches (`POST /query`), and agent retriever calls (`POST /agent`, `POST /agent/stream`) on that collection automatically resolve and use its bound embedding model so vectors always remain in a consistent embedding space.  
  — *Implemented in [`src/core/storage/chroma.py`](src/core/storage/chroma.py)*  
  — *Called in [`src/ingest/service.py`](src/ingest/service.py), [`src/query/service.py`](src/query/service.py) & [`src/agent/tools.py`](src/agent/tools.py)*
- **Domain-Scoped Named Collections:** Instead of pooling all uploaded files into a single flat vector index (which degrades retrieval accuracy when documents cover different topics), documents are organized into separate named **ChromaDB** collections (for example, separating financial reports from clinical studies) that can be listed or deleted independently.  
  — *Implemented in [`src/core/storage/chroma.py`](src/core/storage/chroma.py)*  
  — *Called in [`src/ingest/service.py`](src/ingest/service.py), [`src/query/service.py`](src/query/service.py) & [`src/manage/collections.py`](src/manage/collections.py)*
- **ChromaDB SQLite Metadata & HNSW Vector Index Storage:** Each collection persists its chunk text and page metadata in a SQLite catalog paired with a Hierarchical Navigable Small World (HNSW) approximate nearest-neighbor vector index for fast similarity lookups.  
  — *Implemented in [`src/core/storage/chroma.py`](src/core/storage/chroma.py)*  
  — *Called in [`src/ingest/service.py`](src/ingest/service.py) & [`src/query/service.py`](src/query/service.py)*
- **Hybrid Search via Reciprocal Rank Fusion (Dense Vector + Sparse BM25):** Retrieval (`similarity_search_with_scores`) combines dense semantic vector search (`vectorstore.similarity_search_with_score`) with sparse lexical keyword matching (`BM25Retriever` backed by `rank-bm25` over the collection's stored documents) using **Reciprocal Rank Fusion (RRF)** (`rrf_k=60`, `dense_weight=0.5`, `bm25_weight=0.5`). Zero-score BM25 candidates are filtered out so non-matching chunks do not receive spurious lexical rank bonuses, fused RRF scores are normalized into `[0.0, 1.0]` for downstream observability telemetry, and retrieval gracefully degrades to dense-only similarity scores when no lexical token overlap exists.  
  — *Implemented in [`src/core/storage/chroma.py`](src/core/storage/chroma.py)*  
  — *Called in [`src/query/service.py`](src/query/service.py) & [`src/agent/tools.py`](src/agent/tools.py)*
- **Collection-Targeted Search & Agent Retrieval:** Every similarity search (`POST /query`) and agent conversation (`POST /agent`, `POST /agent/stream`) targets one explicitly chosen collection at a time to keep retrieval focused and free of cross-topic noise.  
  — *Implemented in [`src/query/service.py`](src/query/service.py) & [`src/agent/tools.py`](src/agent/tools.py)*  
  — *Called in [`src/query/routes.py`](src/query/routes.py), [`src/agent/graph.py`](src/agent/graph.py) & [`src/agent/routes.py`](src/agent/routes.py)*

### 3. Agentic RAG Architecture Pattern (Stateful LangGraph Pipeline)
- **Why Agentic RAG Instead of Fixed Single-Shot Retrieval:** Traditional RAG pipelines run a single similarity search using the user's raw prompt, which often struggles with conversational follow-up questions, multi-part questions, or queries that require rephrasing search terms. InteractiveRAG uses **LangGraph** to orchestrate a stateful, cyclic tool-calling workflow powered by **Vertex AI Gemini** (`gemini-2.5-flash`, `gemini-2.5-pro`, or `gemini-2.5-flash-lite` at `temperature=0` for deterministic, factual responses).  
  — *Implemented in [`src/agent/graph.py`](src/agent/graph.py)*  
  — *Called in [`src/agent/routes.py`](src/agent/routes.py) & [`src/agent/service.py`](src/agent/service.py)*
- **Dynamic Collection-Scoped Retriever Tool:** On each request, the backend builds a structured retriever tool (retrieving the top-$k=5$ most relevant chunks per call) bound strictly to the user's selected collection.  
  — *Implemented in [`src/agent/tools.py`](src/agent/tools.py)*  
  — *Called in [`src/agent/graph.py`](src/agent/graph.py)*
- **Iterative Query Formulation & Multi-Step Search:** Gemini autonomously decides when to call the retriever tool, formulates targeted search queries based on the conversation history, inspects the returned chunks, and can perform additional search steps with refined queries if the initial chunks are insufficient before synthesizing its final answer.  
  — *Implemented in [`src/agent/graph.py`](src/agent/graph.py)*  
  — *Called in [`src/agent/routes.py`](src/agent/routes.py) & [`src/agent/service.py`](src/agent/service.py)*
- **Multi-Turn Conversation Memory (`InMemorySaver`):** Conversation history is persisted across turns using an in-memory LangGraph checkpointer keyed by conversation thread so users can ask natural follow-up questions without repeating earlier context.  
  — *Implemented in [`src/agent/memory.py`](src/agent/memory.py)*  
  — *Called in [`src/agent/graph.py`](src/agent/graph.py), [`src/agent/routes.py`](src/agent/routes.py) & [`src/agent/service.py`](src/agent/service.py)*
- **Real-Time Step-by-Step SSE Event Streaming:** During execution, the streaming engine deduplicates graph state transitions and streams each completed workflow step—retriever tool invocations, retrieved evidence chunks, and the final AI answer—in real time over Server-Sent Events (SSE).  
  — *Implemented in [`src/agent/service.py`](src/agent/service.py)*  
  — *Called in [`src/agent/routes.py`](src/agent/routes.py)*

### 4. Document Grounding & Inline Page Citations
- **1-Indexed Page Number Normalization:** PDF parsers number pages starting at `0`, which causes off-by-one confusion when cited directly. Before returning chunks to the language model, the retrieval guardrail formatter converts `0`-based page metadata into human-readable `1`-indexed page numbers (`p.1`, `p.2`, ...) and attaches the exact `source` filename and `page` number to each chunk.  
  — *Implemented in [`src/core/ai_security/guardrails.py`](src/core/ai_security/guardrails.py)*  
  — *Called in [`src/agent/tools.py`](src/agent/tools.py)*
- **Mandatory Document Grounding:** The system prompt enforces a strict grounding contract requiring the agent to call the retriever tool before answering factual questions and base every factual claim *exclusively* on the retrieved document chunks rather than pretraining memory or assumptions.  
  — *Implemented in [`src/agent/graph.py`](src/agent/graph.py)*  
  — *Called in [`src/agent/routes.py`](src/agent/routes.py) & [`src/agent/service.py`](src/agent/service.py)*
- **Explicit Abstention When Evidence Is Missing:** Whenever the retriever returns no matching chunks or the retrieved chunks lack sufficient evidence to answer the question, the agent explicitly states that the answer cannot be found in the available documents rather than guessing.  
  — *Implemented in [`src/core/ai_security/guardrails.py`](src/core/ai_security/guardrails.py) & [`src/agent/graph.py`](src/agent/graph.py)*  
  — *Called in [`src/agent/tools.py`](src/agent/tools.py), [`src/agent/routes.py`](src/agent/routes.py) & [`src/agent/service.py`](src/agent/service.py)*
- **Inline `[source, p.X]` Page Citations:** Every factual statement in the assistant's answer must include an inline citation matching the exact `source` filename and `1`-indexed `page` number of the supporting chunk (for example, `[annual_report.pdf, p.4]`) so users can verify every claim against the source PDF.  
  — *Implemented in [`src/core/ai_security/guardrails.py`](src/core/ai_security/guardrails.py) & [`src/agent/graph.py`](src/agent/graph.py)*  
  — *Called in [`src/agent/tools.py`](src/agent/tools.py), [`src/core/compliance/provenance.py`](src/core/compliance/provenance.py) & [`src/core/observability/telemetry.py`](src/core/observability/telemetry.py)*

---

## 🏗️ Cloud Architecture & Defense-in-Depth Blueprints

InteractiveRAG is engineered for reproducible deployment on **Google Cloud Platform (GCP)** across **13 defense-in-depth architectural layers**, backed by six blueprint documents in [`docs/`](docs/) and the automated test and policy suites in [`tests/`](tests/) and [`infra/scripts/`](infra/scripts/):

| Layer | Blueprint / Reference | Pillars & Focus Areas |
| :--- | :--- | :--- |
| **1. Cloud Deployment & Multi-Tenant Persistence** | [`docs/GCP_DEPLOYMENT_PLAN.md`](docs/GCP_DEPLOYMENT_PLAN.md) | Serverless Cloud Run microservices (`ingest-api`, `agent-api`, `frontend`), strict multi-tenant data & conversation isolation, and durable Cloud Storage $\leftrightarrow$ `/tmp` vector synchronization |
| **2. Infrastructure as Code (IaC)** | [`docs/GCP_DEPLOYMENT_PLAN.md`](docs/GCP_DEPLOYMENT_PLAN.md) | Declarative Terraform (`infra/terraform/*.tf`), remote versioned GCS state, Google Cloud Build image & Terraform pipelines (`infra/cloudbuild/*.yaml`), reproducible environments, and environment parity |
| **3. CI/CD & Deployment** | [`docs/GCP_DEPLOYMENT_PLAN.md`](docs/GCP_DEPLOYMENT_PLAN.md) | Staged pre-flight/build/deploy pipeline design, immutable Cloud Run revision rollouts, readiness probes, and Git-SHA + versioned-state rollback support |
| **4. Modularity & Abstraction** | [`docs/GCP_DEPLOYMENT_PLAN.md`](docs/GCP_DEPLOYMENT_PLAN.md), [`docs/SCALABILITY_ELASTICITY_PLAN.md`](docs/SCALABILITY_ELASTICITY_PLAN.md) | Loose coupling across 4 domain packages + shared `src/core`, Pydantic/LangChain interface contracts, dynamic LLM & embedding model swappability, and dependency injection |
| **5. Configuration Management** | [`docs/GCP_DEPLOYMENT_PLAN.md`](docs/GCP_DEPLOYMENT_PLAN.md) | Externalized runtime & Terraform config, automatic Cloud Run vs. local environment separation, runtime/infrastructure feature flags, and build-once startup frontend config (`/env-config.js`) |
| **6. Authentication & Authorization** | [`docs/GCP_DEPLOYMENT_PLAN.md`](docs/GCP_DEPLOYMENT_PLAN.md) | Dedicated runtime & deployer service accounts, bucket- and project-scoped IAM, Principle of Least Privilege, Identity Platform OAuth 2.0 / OIDC JWT verification, and API key scoping |
| **7. Zero-Internet VPC & Network Security** | [`docs/NETWORK_SECURITY_PLAN.md`](docs/NETWORK_SECURITY_PLAN.md) | Global External HTTPS Load Balancer (`sslip.io` TLS), locked-down Cloud Run ingress, zero-internet custom VPC, Private Google Access, and Regional Private Service Connect |
| **8. AI-Specific Security** | [`docs/AI_SECURITY_PLAN.md`](docs/AI_SECURITY_PLAN.md) | Prompt injection mitigation (Model Armor & XML spotlighting), model allowlists, grounding & upload guardrails, two-stage output filtering, and dual-layer content safety |
| **9. Data Protection & Privacy** | [`docs/AI_SECURITY_PLAN.md`](docs/AI_SECURITY_PLAN.md), [`docs/COMPLIANCE_GOVERNANCE_PLAN.md`](docs/COMPLIANCE_GOVERNANCE_PLAN.md) | Encryption at rest & in transit, Sensitive Data Protection (Cloud DLP), PII handling & query hashing, architectural data classification, and Secret Manager integration |
| **10. Compliance & Governance** | [`docs/COMPLIANCE_GOVERNANCE_PLAN.md`](docs/COMPLIANCE_GOVERNANCE_PLAN.md) | Single-region data residency, end-to-end audit & data-access logging, GDPR self-service rights, EU AI Act provenance transparency, fail-closed screening, and rate/quota enforcement |
| **11. Observability & AI Monitoring** | [`docs/OBSERVABILITY_PLAN.md`](docs/OBSERVABILITY_PLAN.md) | Structured GCP JSON operational logging, Cloud Monitoring log-based metrics & dashboards, distributed per-stage tracing, and AI-specific latency, token & retrieval quality telemetry |
| **12. Scalability, Elasticity & Resource Efficiency** | [`docs/SCALABILITY_ELASTICITY_PLAN.md`](docs/SCALABILITY_ELASTICITY_PLAN.md) | Workload-isolated microservice packaging, compute right-sizing (`cpu_idle`, `startup_cpu_boost`), scale-to-zero & concurrency autoscaling, session affinity, bounded `/tmp` LRU cache eviction, parallel DLP screening, and incremental MD5 delta sync |
| **13. Testing & Quality Engineering** | [`tests/`](tests/), [`infra/scripts/`](infra/scripts/) | Offline unit test suites, multi-tenant FastAPI integration tests, live HTTP/SSE E2E smoke tests, automated pre-deploy static & Org-Policy gates, and cross-layer test coverage |

---

### 1. Cloud Deployment & Multi-Tenant Persistence ([`docs/GCP_DEPLOYMENT_PLAN.md`](docs/GCP_DEPLOYMENT_PLAN.md))

Multiple users can sign in and use the cloud platform simultaneously—each person gets their own isolated document collections and chat history, while serverless containers automatically scale down to zero when idle to save cost.

- **1.1 Serverless Cloud Run Microservice Architecture**
  - **Serverless Cloud Run Gen2 Services:** The application runs on **Google Cloud Run** across the React web frontend (`frontend`), the Document Ingestion service (`ingest-api`), and the interactive AI Agent, Search & Management service (`agent-api`), scaling on demand and scaling down to zero when idle. Each service is configured with `/health` liveness/readiness checks, explicit cross-origin security rules (`CORSMiddleware`), request correlation middleware, and HTTP Server-Sent Events (SSE) streaming support.  
    — *Provisioned in [`infra/terraform/cloud_run.tf`](infra/terraform/cloud_run.tf)*  
    — *Implemented in [`src/core/app_factory.py`](src/core/app_factory.py)*  
    — *Called in [`src/ingest/app.py`](src/ingest/app.py) & [`src/agent/app.py`](src/agent/app.py)*

- **1.2 Multi-Tenant Data & Conversation Isolation**
  - **Identity-Bound Tenant Context (`uid`):** A user's tenant scope is derived exclusively from the verified Firebase JWT `uid` (validated against a strict alphanumeric pattern to prevent path traversal)—never from client-supplied request parameters or headers.  
    — *Implemented in [`src/core/auth.py`](src/core/auth.py)*  
    — *Called in [`src/core/storage/paths.py`](src/core/storage/paths.py), [`src/ingest/routes.py`](src/ingest/routes.py), [`src/query/routes.py`](src/query/routes.py), [`src/agent/routes.py`](src/agent/routes.py), [`src/manage/collections.py`](src/manage/collections.py) & [`src/manage/compliance.py`](src/manage/compliance.py)*
  - **Per-User Vector Storage & Collection Isolation:** Each user's ChromaDB files are isolated under a dedicated Cloud Storage prefix (`gs://<bucket>/users/{uid}/chroma_db/`) and an isolated local container directory (`/tmp/chroma_db/{uid}`). Two users can create, query, or delete collections with the exact same name without ever seeing or overwriting each other's documents.  
    — *Implemented in [`src/core/storage/paths.py`](src/core/storage/paths.py) & [`src/core/storage/chroma.py`](src/core/storage/chroma.py)*  
    — *Called in [`src/ingest/service.py`](src/ingest/service.py), [`src/query/service.py`](src/query/service.py) & [`src/manage/collections.py`](src/manage/collections.py)*
  - **Isolated Per-Request PDF Upload Staging:** In cloud deployments, the ingestion service accepts only multipart file uploads (blocking requests that reference local server file paths). Uploaded PDFs are staged in an isolated per-request folder (`/tmp/uploads/{uid}/{request_id}`), archived to the user's private Cloud Storage prefix (`gs://<bucket>/users/{uid}/uploads/`), and deleted from local disk as soon as indexing finishes.  
    — *Implemented in [`src/core/ai_security/guardrails.py`](src/core/ai_security/guardrails.py), [`src/ingest/upload_parser.py`](src/ingest/upload_parser.py) & [`src/core/storage/gcs_sync.py`](src/core/storage/gcs_sync.py)*  
    — *Called in [`src/ingest/routes.py`](src/ingest/routes.py)*
  - **Tenant-Namespaced Conversation Memory:** Every multi-turn chat thread is namespaced as `{uid}:{thread_id}` inside the in-memory LangGraph checkpointer so one user can never read, resume, or clear another user's conversation history.  
    — *Implemented in [`src/agent/memory.py`](src/agent/memory.py) & [`src/core/compliance/dsar.py`](src/core/compliance/dsar.py)*  
    — *Called in [`src/agent/routes.py`](src/agent/routes.py), [`src/agent/service.py`](src/agent/service.py) & [`src/manage/compliance.py`](src/manage/compliance.py)*

- **1.3 Durable Cloud Storage $\leftrightarrow$ Container Memory Synchronization**
  - **Why We Synchronize Instead of Mounting Cloud Storage Directly:** ChromaDB relies on SQLite and memory-mapped index files, whereas Google Cloud Storage does not support the low-level file locks SQLite requires. Sharing a direct storage mount across containers would risk database lock errors and corrupted indexes, while storing data only in container memory would lose all collections whenever serverless instances scale down.
  - **Private Versioned Persistence Bucket:** Each user's authoritative vector database and uploaded PDFs are stored durably in a private, versioned **Google Cloud Storage** bucket with public access strictly blocked.  
    — *Provisioned in [`infra/terraform/storage.tf`](infra/terraform/storage.tf)*  
    — *Called in [`infra/terraform/cloud_run.tf`](infra/terraform/cloud_run.tf)*
  - **On-Demand Snapshot Synchronization & Version Tracking:** A lightweight `version.json` marker in Cloud Storage tracks whenever a user's vector database changes. Read requests download updated files into fast local `/tmp/chroma_db/{uid}` storage only when a newer version exists in Cloud Storage, while write requests (`POST /ingest` and `DELETE /collections/{name}`) push updated database files back to Cloud Storage and advance `version.json`.  
    — *Implemented in [`src/core/storage/gcs_sync.py`](src/core/storage/gcs_sync.py)*  
    — *Called in [`src/core/storage/chroma.py`](src/core/storage/chroma.py), [`src/ingest/routes.py`](src/ingest/routes.py) & [`src/manage/collections.py`](src/manage/collections.py)*
  - **Distributed Per-User Write Locks in Cloud Storage:** Before modifying a user's vector database, the backend acquires an atomic per-user lock object in Cloud Storage (`if_generation_match=0`, with automatic TTL recovery if a previous container crashed) to prevent concurrent write conflicts across instances.  
    — *Implemented in [`src/core/storage/locks.py`](src/core/storage/locks.py)*  
    — *Called in [`src/core/storage/gcs_sync.py`](src/core/storage/gcs_sync.py)*
  - **Automatic Local `/tmp` LRU Cache Eviction:** An automatic Least-Recently-Used (LRU) cache evictor removes the oldest inactive user caches when local `/tmp` storage fills up while never evicting a directory currently locked by an active request.  
    — *Implemented in [`src/core/storage/locks.py`](src/core/storage/locks.py)*  
    — *Called in [`src/core/storage/gcs_sync.py`](src/core/storage/gcs_sync.py)*

---

### 2. Infrastructure as Code (IaC) ([`docs/GCP_DEPLOYMENT_PLAN.md`](docs/GCP_DEPLOYMENT_PLAN.md))

All cloud infrastructure, security policies, networking, and monitoring resources are managed declaratively as version-controlled code rather than through manual Google Cloud Console changes:

- **2.1 Pillar 1 — Declarative Terraform Stack & Remote State (`Terraform`)**
  - **Full-Stack Terraform Provisioning (`infra/terraform/*.tf`):** Terraform (`>= 1.5.0`, `hashicorp/google` and `hashicorp/google-beta` `~> 6.0`) declaratively provisions all 19 required Google Cloud APIs, Artifact Registry, dedicated runtime IAM service accounts, the versioned Cloud Storage persistence bucket, Identity Platform authentication, the zero-internet custom VPC and `/24` subnets, Private Google Access DNS, Regional Private Service Connect (PSC) for Model Armor, Cloud DLP inspect/de-identify templates, Model Armor templates, all three Cloud Run Gen2 services, the Global External HTTPS Load Balancer with managed `sslip.io` TLS certificate, Cloud Audit Logs, regional Cloud Logging buckets/sinks, log-based metrics, and Cloud Monitoring dashboards and alerts.  
    — *Provisioned in [`infra/terraform/versions.tf`](infra/terraform/versions.tf), [`infra/terraform/apis.tf`](infra/terraform/apis.tf), [`infra/terraform/iam.tf`](infra/terraform/iam.tf), [`infra/terraform/storage.tf`](infra/terraform/storage.tf), [`infra/terraform/auth.tf`](infra/terraform/auth.tf), [`infra/terraform/network.tf`](infra/terraform/network.tf), [`infra/terraform/load_balancer.tf`](infra/terraform/load_balancer.tf), [`infra/terraform/ai_security.tf`](infra/terraform/ai_security.tf), [`infra/terraform/cloud_run.tf`](infra/terraform/cloud_run.tf), [`infra/terraform/compliance.tf`](infra/terraform/compliance.tf), [`infra/terraform/observability.tf`](infra/terraform/observability.tf) & [`infra/terraform/outputs.tf`](infra/terraform/outputs.tf)*
  - **Remote Versioned GCS State Backend:** Terraform state is stored in a dedicated, versioned Google Cloud Storage bucket (`gs://<app>-tfstate-<project_number>/<app>/<env>/state`) with uniform bucket-level access and enforced public access prevention. Bucket and prefix parameters are injected dynamically during `make tf-init` (`-backend-config`) so `infra/terraform/versions.tf` remains portable across projects and environments without hardcoded bucket names.  
    — *Provisioned in [`infra/terraform/versions.tf`](infra/terraform/versions.tf)*  
    — *Called in [`Makefile`](Makefile) & [`infra/cloudbuild/run-terraform.yaml`](infra/cloudbuild/run-terraform.yaml)*

- **2.2 Pillar 2 — Cloud-Native Image & Terraform Execution (`Cloud Build`)**
  - **Parallel Multi-Image Cloud Build Pipeline (`infra/cloudbuild/build-images.yaml`):** **Google Cloud Build** builds both `linux/amd64` production container images (`backend` and `frontend`) concurrently (`waitFor: ['-']`), tags each image with both the Git commit SHA (`${_IMAGE_TAG}`) and `latest`, pushes them to the regional Artifact Registry repository, and archives build logs in the state bucket (`gs://${_LOGS_BUCKET}/cloudbuild-logs`).  
    — *Implemented in [`infra/cloudbuild/build-images.yaml`](infra/cloudbuild/build-images.yaml), [`infra/docker/Dockerfile.backend`](infra/docker/Dockerfile.backend) & [`infra/docker/Dockerfile.frontend`](infra/docker/Dockerfile.frontend)*  
    — *Called in [`Makefile`](Makefile)*
  - **Hybrid Local / Cloud Build Terraform Runner (`infra/cloudbuild/run-terraform.yaml`):** The root `Makefile` auto-detects whether a local `terraform` binary is installed (`TF_MODE=local`) and transparently falls back to executing `terraform init`, `validate`, `plan`, `apply`, `output`, and `destroy` remotely inside **Google Cloud Build** (`TF_MODE=cloudbuild`) using the pinned `hashicorp/terraform:1.9.8` image and a dedicated least-privilege deployer service account (`<app>-deployer`).  
    — *Implemented in [`infra/cloudbuild/run-terraform.yaml`](infra/cloudbuild/run-terraform.yaml)*  
    — *Called in [`Makefile`](Makefile)*

- **2.3 Pillar 3 — Reproducible Environments**
  - **Idempotent Zero-Touch Project Bootstrap (`make bootstrap`):** A single idempotent command enables seed GCP APIs, creates the versioned Terraform state bucket, provisions and binds the least-privilege deployer service account (`make ensure-deployer-sa`), and executes a targeted Terraform apply for Artifact Registry so a clean GCP project can be bootstrapped from scratch without manual setup.  
    — *Provisioned in [`infra/terraform/apis.tf`](infra/terraform/apis.tf)*  
    — *Called in [`Makefile`](Makefile)*
  - **Deterministic Hostname Resolution & Single-Apply Convergence:** Terraform computes deterministic Cloud Run regional hostnames (`<service>-<project_number>.<region>.run.app`) and the Load Balancer's `<IP-with-dashes>.sslip.io` domain upfront in `locals`, breaking circular dependencies between Cloud Run, Identity Platform `authorized_domains`, and backend `ALLOWED_ORIGINS` CORS lists so the entire stack converges in a single apply.  
    — *Provisioned in [`infra/terraform/cloud_run.tf`](infra/terraform/cloud_run.tf), [`infra/terraform/load_balancer.tf`](infra/terraform/load_balancer.tf) & [`infra/terraform/auth.tf`](infra/terraform/auth.tf)*
  - **Pinned Base Images & Plan-Time Invariant Preconditions:** Dockerfiles pin explicit base image versions (`python:3.12-slim`, `node:22-slim`, `nginx:1.27-alpine`), `infra/cloudbuild/run-terraform.yaml` pins `hashicorp/terraform:1.9.8`, and Terraform `validation` / `lifecycle { precondition }` blocks reject invalid regions or out-of-range parameters at `terraform plan` time.  
    — *Provisioned in [`infra/terraform/variables.tf`](infra/terraform/variables.tf), [`infra/terraform/storage.tf`](infra/terraform/storage.tf), [`infra/terraform/ai_security.tf`](infra/terraform/ai_security.tf), [`infra/terraform/compliance.tf`](infra/terraform/compliance.tf) & [`infra/terraform/observability.tf`](infra/terraform/observability.tf)*  
    — *Implemented in [`infra/docker/Dockerfile.backend`](infra/docker/Dockerfile.backend), [`infra/docker/Dockerfile.frontend`](infra/docker/Dockerfile.frontend) & [`infra/cloudbuild/run-terraform.yaml`](infra/cloudbuild/run-terraform.yaml)*

- **2.4 Pillar 4 — Environment Parity**
  - **Single Parameterized Terraform Codebase Across `dev`, `staging` & `prod`:** All environments share the exact same `.tf` definitions in `infra/terraform/`, parameterized by `var.environment` (which namespaces resource identifiers as `-${var.environment}`, isolates remote state prefixes under `$(APP_NAME)/$(ENV)/state`, and toggles production-only safeguards such as `deletion_protection = var.environment == "prod"`), preventing configuration drift between staging and production.  
    — *Provisioned in [`infra/terraform/variables.tf`](infra/terraform/variables.tf) & [`infra/terraform/cloud_run.tf`](infra/terraform/cloud_run.tf)*  
    — *Called in [`Makefile`](Makefile)*
  - **Immutable Build-Once Container Artifacts Across Services & Environments:** A single `backend` image powers both `ingest-api` (`APP_MODULE=src.ingest.app:app`) and `agent-api` (`APP_MODULE=src.agent.app:app`), while the `frontend` image injects environment-specific endpoints and Firebase settings at container startup via `/env-config.js`—allowing the exact same container digest to run across environments without rebuilding.  
    — *Provisioned in [`infra/terraform/cloud_run.tf`](infra/terraform/cloud_run.tf)*  
    — *Implemented in [`infra/docker/Dockerfile.backend`](infra/docker/Dockerfile.backend), [`infra/docker/Dockerfile.frontend`](infra/docker/Dockerfile.frontend) & [`infra/docker/entrypoint-frontend.sh`](infra/docker/entrypoint-frontend.sh)*

> **Note on Unimplemented / Non-Relevant Pillars:**
> - **Pulumi (*Not Implemented — Not Relevant*):** Pulumi is not used because 100% of our Google Cloud infrastructure is already declaratively defined and managed with **Terraform** ([`infra/terraform/*.tf`](infra/terraform/)), which provides first-party Google Cloud provider support and integrates directly with our remote GCS state backend and Cloud Build runner.

---

### 3. CI/CD & Deployment ([`docs/GCP_DEPLOYMENT_PLAN.md`](docs/GCP_DEPLOYMENT_PLAN.md))

Container builds, compliance verification, and infrastructure rollouts are orchestrated through an automated, policy-gated deployment pipeline:

- **3.1 Pillar 1 — Pipeline Design**
  - **Staged Pre-Flight, Build, Policy-Gate & Deploy Pipeline:** The [`Makefile`](Makefile) and **Google Cloud Build** configs structure delivery into four deterministic stages:
    1. **Bootstrap (`make bootstrap`):** Enables seed APIs, provisions the versioned remote state bucket and least-privilege `<app>-deployer` service account, and creates Artifact Registry.
    2. **Parallel Container Build & Push (`make build-push` / `make build-push-local`):** Executes [`infra/cloudbuild/build-images.yaml`](infra/cloudbuild/build-images.yaml) on Cloud Build (or local Docker Buildx) to compile and push `linux/amd64` `backend` and `frontend` images tagged with `$(IMAGE_TAG)` (defaulting to `git rev-parse --short HEAD`) and `latest`.
    3. **Mandatory Pre-Deploy Policy Gate (`make policy-check`):** Wired as an automatic prerequisite of both `make plan` and `make deploy`. Runs `check-project` (verifying active `gcloud` authentication), `check-region-policy` (querying the live GCP `constraints/gcp.resourceLocations` Organization Policy), `check-static-policy` (offline verification of single-region defaults, fail-closed Model Armor, Cloud Run concurrency/deletion-protection, and observability sinks/alerts), and `make validate` (`terraform validate`).
    4. **Infrastructure Preview & Apply (`make plan` -> `make deploy` -> `make outputs`):** Applies the Terraform stack (locally or via [`infra/cloudbuild/run-terraform.yaml`](infra/cloudbuild/run-terraform.yaml)), including idempotent state auto-import for project-singleton Identity Platform resources.  
    — *Implemented in [`Makefile`](Makefile), [`infra/cloudbuild/build-images.yaml`](infra/cloudbuild/build-images.yaml), [`infra/cloudbuild/run-terraform.yaml`](infra/cloudbuild/run-terraform.yaml), [`infra/scripts/check_region_policy.py`](infra/scripts/check_region_policy.py) & [`infra/scripts/check_static_policies.py`](infra/scripts/check_static_policies.py)*

- **3.2 Pillar 2 — Deployment Strategies**
  - **Zero-Downtime Immutable Cloud Run Revision Rollouts:** Deploying a new `var.image_tag` creates an immutable Cloud Run Gen2 revision for `ingest-api`, `agent-api`, and `frontend`. Cloud Run automatically starts the new revision, verifies container readiness (`GET /health`), and atomically shifts incoming traffic from the previous revision to the new healthy revision without dropping active connections.  
    — *Provisioned in [`infra/terraform/cloud_run.tf`](infra/terraform/cloud_run.tf)*  
    — *Implemented in [`src/core/app_factory.py`](src/core/app_factory.py)*
  - **Zero-Downtime TLS Certificate Replacement (`create_before_destroy`):** The Google-managed SSL certificate on the Global HTTPS Load Balancer is configured with `lifecycle { create_before_destroy = true }` so replacement certificates are provisioned and bound to the HTTPS proxy before old certificates are removed.  
    — *Provisioned in [`infra/terraform/load_balancer.tf`](infra/terraform/load_balancer.tf)*

- **3.3 Pillar 3 — Rollback Support**
  - **Git-SHA Immutable Image Tags & Versioned Storage for Fast Rollback:** Because every `make build-push` tags container images in Artifact Registry with the exact Git commit SHA (`IMAGE_TAG`) alongside `latest` and Object Versioning is enabled on both the Terraform state bucket (`gs://<app>-tfstate-<project_number>`) and the ChromaDB persistence bucket (`google_storage_bucket.chroma_persistence`), operators can deterministically roll back any deployment to a prior known-good revision via `make deploy IMAGE_TAG=<previous-git-sha>` or Cloud Run revision traffic shifting.  
    — *Provisioned in [`infra/terraform/cloud_run.tf`](infra/terraform/cloud_run.tf) & [`infra/terraform/storage.tf`](infra/terraform/storage.tf)*  
    — *Called in [`Makefile`](Makefile) & [`infra/cloudbuild/build-images.yaml`](infra/cloudbuild/build-images.yaml)*

> **Note on Unimplemented Pillars:**
> - **Automated Git-Webhook CI Triggers, Progressive Traffic Splitting (Blue/Green & Canary) & Automated Rollback Triggers (*Not Implemented — Relevant*):** While Cloud Run performs zero-downtime revision swaps and supports manual Git-SHA rollbacks, automated Git-push CI triggers (e.g., GitHub Actions or Cloud Build Triggers running `unittest` on pull requests), declarative percentage-based blue/green or canary traffic-splitting blocks (`traffic {}`) in `infra/terraform/cloud_run.tf`, and automated rollback triggers tied to our Cloud Monitoring 5xx/latency alert policies ([`infra/terraform/observability.tf`](infra/terraform/observability.tf)) are not yet implemented and remain relevant future production enhancements.

---

### 4. Modularity & Abstraction ([`docs/GCP_DEPLOYMENT_PLAN.md`](docs/GCP_DEPLOYMENT_PLAN.md), [`docs/SCALABILITY_ELASTICITY_PLAN.md`](docs/SCALABILITY_ELASTICITY_PLAN.md))

The backend is architected around clean package boundaries, typed schema contracts, swappable AI models, and dependency injection so domain logic, security guardrails, and infrastructure adapters evolve independently:

- **4.1 Pillar 1 — Loose Coupling**
  - **4 Domain Packages + Shared Platform Core (`src/`):** Application logic is separated into a shared platform foundation ([`src/core/`](src/core/) for auth, config, storage, AI security, compliance, and observability) and **4 loosely coupled domain packages** that never import one another's route handlers:
    1. [`src/ingest/`](src/ingest/) — Multipart upload staging, `PyPDFLoader` extraction, `RecursiveCharacterTextSplitter` chunking, and batched ChromaDB indexing (`ingest-api`).
    2. [`src/manage/`](src/manage/) — Collection listing/deletion and GDPR Art. 15/17 self-service data inventory and erasure (`agent-api`).
    3. [`src/query/`](src/query/) — Direct top-$k$ hybrid dense + BM25 RRF similarity search with parallel Cloud DLP redaction (`agent-api`).
    4. [`src/agent/`](src/agent/) — Stateful LangGraph agentic workflow, dynamic retriever tool construction, tenant-namespaced memory, and SSE streaming (`agent-api`).  
    — *Implemented in [`src/core/`](src/core/), [`src/ingest/`](src/ingest/), [`src/manage/`](src/manage/), [`src/query/`](src/query/) & [`src/agent/`](src/agent/)*
  - **Composable Service Entrypoints (`create_app`):** [`src/core/app_factory.py`](src/core/app_factory.py) centralizes FastAPI creation, CORS, request correlation middleware, and `/health`, allowing [`src/ingest/app.py`](src/ingest/app.py) and [`src/agent/app.py`](src/agent/app.py) to compose independent microservices simply by passing their respective `APIRouter` lists.  
    — *Implemented in [`src/core/app_factory.py`](src/core/app_factory.py), [`src/ingest/app.py`](src/ingest/app.py) & [`src/agent/app.py`](src/agent/app.py)*
  - **Decoupled Cross-Cutting Context (`ContextVar`):** Request correlation IDs, distributed trace spans, compliance guardrail flags, and AI telemetry metrics propagate across modules via thread- and async-safe Python `ContextVar` state rather than tightly coupling domain functions to logging or audit internals.  
    — *Implemented in [`src/core/context.py`](src/core/context.py), [`src/core/compliance/state.py`](src/core/compliance/state.py) & [`src/core/observability/state.py`](src/core/observability/state.py)*

- **4.2 Pillar 2 — Interface Contracts**
  - **Strict Pydantic Request/Response & LangGraph State Schemas:** Every API and workflow boundary is governed by explicit typed contracts—including [`IngestRequest`](src/ingest/schemas.py), [`QueryRequest`](src/query/schemas.py), [`AgentRequest`](src/agent/schemas.py), [`AgentResponse`](src/agent/schemas.py), [`ProvenanceMetadata`](src/agent/schemas.py), [`AgentState`](src/agent/schemas.py) (`TypedDict`), [`AuthenticatedUser`](src/core/auth.py), and [`ArmorDecision`](src/core/ai_security/armor.py)—enforcing input validation (`HTTP 422`) before business logic executes.  
    — *Implemented in [`src/ingest/schemas.py`](src/ingest/schemas.py), [`src/query/schemas.py`](src/query/schemas.py), [`src/agent/schemas.py`](src/agent/schemas.py), [`src/core/auth.py`](src/core/auth.py) & [`src/core/ai_security/armor.py`](src/core/ai_security/armor.py)*
  - **LangChain `Embeddings`, `BaseMessage` & `StructuredTool` Abstractions:** Storage, retrieval, and agent modules program against standard LangChain interfaces (`Embeddings`, `Document`, `BaseMessage`, `StructuredTool`), allowing [`similarity_search_with_scores()`](src/core/storage/chroma.py) to support both full Chroma hybrid RRF search and lightweight retriever-compatible implementations.  
    — *Implemented in [`src/core/storage/chroma.py`](src/core/storage/chroma.py), [`src/agent/tools.py`](src/agent/tools.py) & [`src/agent/graph.py`](src/agent/graph.py)*

- **4.3 Pillar 3 — Model Swappability**
  - **Dynamic Per-Request Generative LLM Swappability:** Users and API callers can switch between any approved Vertex AI generative model in `ALLOWED_LLM_MODELS` (`gemini-2.5-flash`, `gemini-2.5-pro`, or `gemini-2.5-flash-lite`) on a per-request basis (`model_name` in `POST /agent` and `POST /agent/stream`) or expand the catalog via environment configuration without changing code.  
    — *Implemented in [`src/core/config.py`](src/core/config.py), [`src/core/ai_security/guardrails.py`](src/core/ai_security/guardrails.py) & [`src/agent/graph.py`](src/agent/graph.py)*  
    — *Called in [`src/agent/schemas.py`](src/agent/schemas.py), [`src/agent/routes.py`](src/agent/routes.py) & [`frontend/src/App.jsx`](frontend/src/App.jsx)*
  - **Per-Collection Embedding Model Swappability with Automatic Resolution:** Users can choose any approved Vertex AI embedding model in `ALLOWED_EMBEDDING_MODELS` (`gemini-embedding-001`, `text-embedding-004`, or `text-embedding-005`) when creating a collection (`POST /ingest`). The selected model is persisted in ChromaDB collection metadata and automatically resolved via [`resolve_collection_embedding_model()`](src/core/storage/chroma.py) across all subsequent uploads, `/query` searches, and `/agent` retriever calls.  
    — *Implemented in [`src/core/storage/chroma.py`](src/core/storage/chroma.py) & [`src/core/ai_security/guardrails.py`](src/core/ai_security/guardrails.py)*  
    — *Called in [`src/ingest/service.py`](src/ingest/service.py), [`src/query/service.py`](src/query/service.py), [`src/agent/tools.py`](src/agent/tools.py) & [`src/manage/collections.py`](src/manage/collections.py)*

- **4.4 Pillar 4 — Dependency Injection**
  - **FastAPI `Depends()` Request-Scoped Injection:** Authentication verification, tenant UID resolution, and multipart PDF upload validation are injected into route handlers via FastAPI `Depends(get_current_user)`, `Depends(get_current_user_info)`, and `Depends(parse_ingest_payload)`.  
    — *Implemented in [`src/core/auth.py`](src/core/auth.py) & [`src/ingest/upload_parser.py`](src/ingest/upload_parser.py)*  
    — *Called in [`src/ingest/routes.py`](src/ingest/routes.py), [`src/query/routes.py`](src/query/routes.py), [`src/agent/routes.py`](src/agent/routes.py), [`src/manage/collections.py`](src/manage/collections.py) & [`src/manage/compliance.py`](src/manage/compliance.py)*
  - **Factory & Collaborator Injection for Offline Test Doubles:** Core constructors—including `create_app(..., routers=...)`, `build_agent(retriever_tool, model_name=..., checkpointer=...)`, and modular client factories (`get_embeddings`, `get_storage_client`, `ChatGoogleGenerativeAI`)—accept injected collaborators or factory overrides so the offline test suite ([`tests/helpers.py`](tests/helpers.py)) can substitute `FakeGCSBucket`, `DeterministicFakeEmbeddings`, and `GroundedCitingChatModel` deterministically.  
    — *Implemented in [`src/core/app_factory.py`](src/core/app_factory.py), [`src/agent/graph.py`](src/agent/graph.py), [`src/core/storage/chroma.py`](src/core/storage/chroma.py), [`src/core/storage/gcs_sync.py`](src/core/storage/gcs_sync.py) & [`tests/helpers.py`](tests/helpers.py)*

> **Note on Unimplemented / Non-Relevant Pillars:**
> - **Multi-Cloud LLM / External Vector Database Adapter Plugins (*Not Implemented — Not Relevant*):** Swapping to non-GCP LLM providers or external vector database servers is not relevant to this project's architecture, which is specifically designed around single-region Google Cloud residency (**Vertex AI** accessed over Private Google Access) and serverless per-tenant **ChromaDB** snapshots synchronized with **Google Cloud Storage**.

---

### 5. Configuration Management ([`docs/GCP_DEPLOYMENT_PLAN.md`](docs/GCP_DEPLOYMENT_PLAN.md))

All runtime, security, scaling, and frontend settings are externalized from application code and automatically adapt across local development and cloud environments:

- **5.1 Pillar 1 — Externalized Configuration**
  - **Single-Source-of-Truth Backend Config (`src/core/config.py`):** All runtime parameters—GCP project and single-region location (`GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION`), approved model catalogs (`ALLOWED_LLM_MODELS`, `ALLOWED_EMBEDDING_MODELS`), upload and retrieval guardrails (`MAX_UPLOAD_MB`, `MAX_FILES_PER_REQUEST`, `MAX_CHUNKS_PER_INGEST`, `MAX_QUERY_CHARS`, `AGENT_RECURSION_LIMIT`), security templates (`MODEL_ARMOR_TEMPLATE_ID`, `GEMINI_SAFETY_THRESHOLD`), per-user rate limits and collection quotas (`RATE_LIMIT_INGEST_PER_MINUTE`, `RATE_LIMIT_QUERY_PER_MINUTE`, `MAX_COLLECTIONS_PER_USER`), and `/tmp` cache eviction bounds (`CHROMA_TMP_MAX_BYTES`, `CHROMA_TMP_MIN_FREE_BYTES`, `CHROMA_LOCK_TTL_SECONDS`, `CHROMA_LOCK_TIMEOUT_SECONDS`)—are externalized into environment variables accessed via typed, validated getters in [`src/core/config.py`](src/core/config.py).  
    — *Implemented in [`src/core/config.py`](src/core/config.py)*
  - **Parameterized Terraform Infrastructure Config (`infra/terraform/variables.tf`):** Over 40 validated Terraform input variables (with a documented template in [`infra/terraform/terraform.tfvars.example`](infra/terraform/terraform.tfvars.example)) externalize per-service CPU/memory limits, concurrency ceilings, min/max instance bounds, VPC subnet CIDRs, log retention windows, and alert thresholds, and wire them directly into the Cloud Run container definitions in [`infra/terraform/cloud_run.tf`](infra/terraform/cloud_run.tf).  
    — *Provisioned in [`infra/terraform/variables.tf`](infra/terraform/variables.tf), [`infra/terraform/terraform.tfvars.example`](infra/terraform/terraform.tfvars.example) & [`infra/terraform/cloud_run.tf`](infra/terraform/cloud_run.tf)*
  - **Build-Once Frontend with Startup Runtime Configuration (`/env-config.js`):** Single-page web apps normally bake API URLs and Firebase keys into static JavaScript files during `docker build`, which happens *before* Terraform has provisioned the Load Balancer or generated the project's Firebase Web API key. When the `frontend` container starts on Cloud Run, [`infra/docker/entrypoint-frontend.sh`](infra/docker/entrypoint-frontend.sh) reads the environment variables injected by Terraform (`INGESTION_API_URL`, `AGENT_API_URL`, Firebase auth settings, `ALLOWED_LLM_MODELS`, and `ALLOWED_EMBEDDING_MODELS`) and writes `/usr/share/nginx/html/env-config.js`, which the browser loads before starting React (falling back to the root `.env` via [`frontend/vite.config.js`](frontend/vite.config.js) during local development).  
    — *Provisioned in [`infra/terraform/cloud_run.tf`](infra/terraform/cloud_run.tf)*  
    — *Implemented in [`infra/docker/entrypoint-frontend.sh`](infra/docker/entrypoint-frontend.sh) & [`frontend/vite.config.js`](frontend/vite.config.js)*  
    — *Called in [`frontend/index.html`](frontend/index.html) & [`frontend/src/firebase.js`](frontend/src/firebase.js)*

- **5.2 Pillar 2 — Environment Separation**
  - **Automatic Cloud Run vs. Local Runtime Separation (`is_cloud_run()`):** [`src/core/config.py`](src/core/config.py) detects Cloud Run execution via `K_SERVICE` / `K_REVISION` (`is_cloud_run()`) to enforce strict separation between local developer convenience and cloud production security:
    - **Cloud Run Hardening (`K_SERVICE` set):** Hard-disables `AUTH_DISABLED=true` in [`is_auth_bypassed()`](src/core/auth.py), blocks local server-path PDF ingestion (`pdf_paths`) with `HTTP 403` in [`src/ingest/routes.py`](src/ingest/routes.py), fails closed (`[]`) on missing `ALLOWED_ORIGINS`, enables `/tmp/chroma_db/{uid}` $\leftrightarrow$ Cloud Storage synchronization, and defaults to structured GCP JSON logging and Cloud Trace export.
    - **Local Development (`K_SERVICE` unset):** Loads `.env`, permits `AUTH_DISABLED=true` with `DEV_USER_ID` / `X-Dev-User-Id`, allows localhost CORS origins (`http://localhost:3000`, `http://localhost:5173`), persists ChromaDB directly to local disk when `GCS_BUCKET_NAME` is unset, and formats human-readable console logs.  
    — *Implemented in [`src/core/config.py`](src/core/config.py), [`src/core/auth.py`](src/core/auth.py), [`src/ingest/routes.py`](src/ingest/routes.py), [`src/core/storage/paths.py`](src/core/storage/paths.py) & [`src/core/observability/logging.py`](src/core/observability/logging.py)*
  - **Isolated Per-Environment Cloud State & Resource Naming (`var.environment`):** Terraform separates `dev`, `staging`, and `prod` stacks via `var.environment`—isolating GCS state prefixes (`$(APP_NAME)/$(ENV)/state`), suffixing all GCP resource names (`-${var.environment}`), and gating production deletion protection (`deletion_protection = var.environment == "prod"`).  
    — *Provisioned in [`infra/terraform/variables.tf`](infra/terraform/variables.tf), [`infra/terraform/cloud_run.tf`](infra/terraform/cloud_run.tf) & [`infra/terraform/storage.tf`](infra/terraform/storage.tf)*  
    — *Called in [`Makefile`](Makefile)*

- **5.3 Pillar 3 — Feature Flags**
  - **Runtime & Infrastructure Feature Toggles (`parse_bool_env` & Terraform Booleans):** Security, storage, observability, and developer capabilities are controlled via boolean feature flags parsed by [`parse_bool_env()`](src/core/config.py) and Terraform boolean variables:
    - `MODEL_ARMOR_FAIL_CLOSED` / `var.model_armor_fail_closed` — Controls fail-closed (`HTTP 503`) vs. fail-open screening when Model Armor or Cloud DLP is unavailable (enforced as `true` in Cloud Run by [`infra/scripts/check_static_policies.py`](infra/scripts/check_static_policies.py)).
    - `ARCHIVE_UPLOADS_TO_GCS` / `var.archive_uploads_to_gcs` — Toggles archiving original uploaded PDFs to `gs://<bucket>/users/{uid}/uploads/`.
    - `AUDIT_CLOUD_LOGGING_ENABLED` & `TELEMETRY_CLOUD_LOGGING_ENABLED` — Toggle shipping structured audit and telemetry records to Google Cloud Logging (auto-disabled during `unittest`/`pytest` runs).
    - `STRUCTURED_LOGGING` & `CLOUD_TRACE_ENABLED` — Toggle single-line GCP JSON log formatting and Cloud Trace waterfall export.
    - `AUTH_DISABLED`, `AGENT_CONSOLE_TRACE` & `UVICORN_RELOAD` — Local development toggles for auth bypass, Rich terminal trace panels, and hot reload.
    - `var.enable_google_oauth` & `var.audit_log_bucket_locked` — Terraform feature flags toggling Google OAuth 2.0 federated sign-in and WORM retention lock on the compliance audit log bucket.  
    — *Provisioned in [`infra/terraform/variables.tf`](infra/terraform/variables.tf), [`infra/terraform/auth.tf`](infra/terraform/auth.tf), [`infra/terraform/compliance.tf`](infra/terraform/compliance.tf) & [`infra/terraform/cloud_run.tf`](infra/terraform/cloud_run.tf)*  
    — *Implemented in [`src/core/config.py`](src/core/config.py)*

> **Note on Unimplemented Pillars:**
> - **Dynamic Remote Feature-Flag Evaluation Service (*Not Implemented — Relevant*):** Feature flags are currently evaluated from environment variables injected into Cloud Run revisions (requiring a revision update to toggle in production). Integrating a dynamic remote feature-flag service (such as Firebase Remote Config or OpenFeature for live per-tenant or percentage-based feature rollouts without container redeployment) is not yet implemented and is a relevant future enhancement.

---

### 6. Authentication & Authorization ([`docs/GCP_DEPLOYMENT_PLAN.md`](docs/GCP_DEPLOYMENT_PLAN.md))

Identity and access controls follow a zero-trust model across both cloud infrastructure (dedicated service accounts, scoped IAM, and least privilege) and application requests (Identity Platform OAuth/OIDC tokens and claims-derived tenant isolation):

- **6.1 Pillar 1 — Dedicated Service Accounts**
  - **Separated Runtime, Frontend & CI/CD Identities:** Rather than relying on the default Compute Engine service account (which typically carries broad `roles/editor` access), the platform isolates machine identities across dedicated service accounts:
    1. **Backend Runtime Service Account (`<app>-be-<env>`):** Attached to `ingest-api` and `agent-api` Cloud Run services.
    2. **Frontend Runtime Service Account (`<app>-fe-<env>`):** Attached to the static `frontend` Nginx Cloud Run service.
    3. **Cloud Build & Terraform Deployer Service Account (`<app>-deployer`):** Created by `make ensure-deployer-sa` in [`Makefile`](Makefile) for executing Cloud Build image builds and remote Terraform runs.
    4. **Model Armor Managed Service Agent (`google_project_service_identity.modelarmor_sa`):** Provisioned to allow regional Model Armor templates to invoke Cloud DLP inspection and de-identification templates on the backend's behalf.  
    — *Provisioned in [`infra/terraform/iam.tf`](infra/terraform/iam.tf), [`infra/terraform/ai_security.tf`](infra/terraform/ai_security.tf), [`infra/terraform/cloud_run.tf`](infra/terraform/cloud_run.tf) & [`Makefile`](Makefile)*

- **6.2 Pillar 2 — Identity & Access Management (IAM)**
  - **Explicit Declarative IAM Bindings & Uniform Bucket Access:** All permissions are managed declaratively via `google_project_iam_member` and `google_storage_bucket_iam_member` resources in [`infra/terraform/iam.tf`](infra/terraform/iam.tf) and [`infra/terraform/ai_security.tf`](infra/terraform/ai_security.tf). The Cloud Storage persistence bucket enforces `uniform_bucket_level_access = true` and `public_access_prevention = "enforced"` so legacy per-object ACLs are disabled and access is governed exclusively by IAM.  
    — *Provisioned in [`infra/terraform/iam.tf`](infra/terraform/iam.tf), [`infra/terraform/ai_security.tf`](infra/terraform/ai_security.tf) & [`infra/terraform/storage.tf`](infra/terraform/storage.tf)*

- **6.3 Pillar 3 — Principle of Least Privilege**
  - **Zero-Privilege Frontend & Bucket-Scoped Backend IAM:**
    - `frontend_sa` is intentionally granted **zero IAM roles** because the Nginx container only serves static assets and `/env-config.js` and never calls Google Cloud APIs.
    - `backend_sa` is granted `roles/storage.objectUser` **strictly at the bucket level** on `google_storage_bucket.chroma_persistence` (never at the project level), preventing backend containers from accessing any other bucket in the project (such as the Terraform state bucket). Its project-level roles are limited to `roles/aiplatform.user`, `roles/modelarmor.user`, `roles/dlp.user`, `roles/logging.logWriter`, `roles/monitoring.metricWriter`, and `roles/cloudtrace.agent`.
    - `TF_DEPLOYER_SA` is granted only the resource-admin roles required to deploy the stack and is intentionally denied `roles/orgpolicy.policyAdmin` (Separation of Duties: the deployer can read effective Organization Policies via [`infra/scripts/check_region_policy.py`](infra/scripts/check_region_policy.py) but can never weaken or modify them).
    - Inside the container, [`infra/docker/Dockerfile.backend`](infra/docker/Dockerfile.backend) drops root privileges and executes Uvicorn as an unprivileged user (`appuser`, UID `10001`).  
    — *Provisioned in [`infra/terraform/iam.tf`](infra/terraform/iam.tf) & [`Makefile`](Makefile)*  
    — *Implemented in [`infra/docker/Dockerfile.backend`](infra/docker/Dockerfile.backend)*

- **6.4 Pillar 4 — OAuth 2.0 / OIDC & Zero-Trust User Authorization**
  - **Managed Sign-In Providers (Google OAuth 2.0 / OIDC & Email/Password):** **Google Cloud Identity Platform (Firebase Authentication)** is provisioned to let users sign in with either federated **Google Sign-In (OAuth 2.0 / OIDC)** or **Email & Password**.  
    — *Provisioned in [`infra/terraform/auth.tf`](infra/terraform/auth.tf)*  
    — *Implemented in [`frontend/src/firebase.js`](frontend/src/firebase.js)*  
    — *Called in [`frontend/src/AuthGate.jsx`](frontend/src/AuthGate.jsx)*
  - **Automatic Bearer Token Attachment, Refresh & Session Cleanup:** The web application attaches the signed-in user's OIDC Firebase ID token (`Authorization: Bearer <ID_TOKEN>`) to every REST and SSE streaming request, refreshes expired tokens automatically, and clears all local collection lists, active conversation threads, and chat messages immediately on sign-out or account switch.  
    — *Implemented in [`frontend/src/firebase.js`](frontend/src/firebase.js), [`frontend/src/api.js`](frontend/src/api.js) & [`frontend/src/App.jsx`](frontend/src/App.jsx)*  
    — *Called in [`frontend/src/AuthGate.jsx`](frontend/src/AuthGate.jsx)*
  - **Cryptographic RS256 Backend Verification & Tenant Authorization (`uid`):** Every protected backend endpoint verifies the incoming RS256 JWT signature, expiration (`exp`), and audience against Google's public signing certificates via the Firebase Admin SDK (`verify_bearer_token`), validates that the extracted `uid` matches `UID_PATTERN` (`^[a-zA-Z0-9_-]{1,128}$`), hard-disables the local development auth bypass whenever running in Cloud Run (`K_SERVICE` set), and scopes all tenant data access (`gs://<bucket>/users/{uid}/`, `/tmp/chroma_db/{uid}`, and `{uid}:{thread_id}`) strictly to the verified `uid`.  
    — *Implemented in [`src/core/auth.py`](src/core/auth.py)*  
    — *Called in [`src/ingest/routes.py`](src/ingest/routes.py), [`src/query/routes.py`](src/query/routes.py), [`src/agent/routes.py`](src/agent/routes.py), [`src/manage/collections.py`](src/manage/collections.py) & [`src/manage/compliance.py`](src/manage/compliance.py)*

- **6.5 Pillar 5 — API Key Scoping**
  - **Keyless Backend Service-to-Service Auth (ADC):** Backend services never use static API keys or long-lived service-account JSON key files to call Vertex AI, Cloud Storage, Model Armor, Cloud DLP, or Cloud Logging; they authenticate exclusively via short-lived Google Cloud **Application Default Credentials (ADC)** bound to `backend_sa` (and short-lived `GOOGLE_OAUTH_ACCESS_TOKEN` tokens in `Makefile`).  
    — *Provisioned in [`infra/terraform/iam.tf`](infra/terraform/iam.tf), [`infra/terraform/cloud_run.tf`](infra/terraform/cloud_run.tf) & [`Makefile`](Makefile)*
  - **Authorized-Domain & CORS Scoping for the Browser Identity Platform Key:** The public Identity Platform browser Web API key (`google_identity_platform_config.auth.client[0].api_key`) is restricted via Identity Platform's `authorized_domains` allowlist in [`infra/terraform/auth.tf`](infra/terraform/auth.tf) (permitting only `localhost`, `127.0.0.1`, the project's Firebase domains, the deterministic frontend Cloud Run domain, the Load Balancer's `<IP-with-dashes>.sslip.io` domain, and explicit `var.extra_allowed_origins`), paired with strict FastAPI CORS origin enforcement (`ALLOWED_ORIGINS`).  
    — *Provisioned in [`infra/terraform/auth.tf`](infra/terraform/auth.tf) & [`infra/terraform/cloud_run.tf`](infra/terraform/cloud_run.tf)*  
    — *Implemented in [`src/core/config.py`](src/core/config.py) & [`src/core/app_factory.py`](src/core/app_factory.py)*

> **Note on Unimplemented Pillars:**
> - **Terraform-Managed `google_apikeys_key` Target/Referrer Restrictions & Sub-Tenant RBAC Roles (*Not Implemented — Relevant*):** While backend calls are 100% keyless via IAM/ADC and the browser Identity Platform key is constrained by `authorized_domains` and CORS (`ALLOWED_ORIGINS`), provisioning a dedicated `google_apikeys_key` resource in Terraform with explicit HTTP referrer and API target restrictions (limiting the browser key strictly to `identitytoolkit.googleapis.com` and `securetoken.googleapis.com`) and fine-grained sub-tenant RBAC roles (e.g., team admin vs. read-only viewer roles within a shared workspace) are not yet implemented and remain relevant future security enhancements.

---

### 7. Zero-Internet VPC & Network Security ([`docs/NETWORK_SECURITY_PLAN.md`](docs/NETWORK_SECURITY_PLAN.md))

All external user traffic enters through a single HTTPS Load Balancer, while backend containers run inside a segmented private network with zero outbound access to the public internet.

- **7.1 Single Public HTTPS Entry Point (`Global External Application Load Balancer`)**
  - **Automatic Free HTTPS Certificate:** Terraform reserves a global static IP address and constructs a free `<IP-with-dashes>.sslip.io` domain name so Google Cloud can automatically provision and renew a trusted SSL/TLS certificate (and Identity Platform can authorize the domain) without requiring you to buy a custom domain. Plain HTTP requests on port `80` are automatically redirected to HTTPS on port `443`.  
    — *Provisioned in [`infra/terraform/load_balancer.tf`](infra/terraform/load_balancer.tf)*  
    — *Called in [`infra/terraform/auth.tf`](infra/terraform/auth.tf) & [`infra/terraform/outputs.tf`](infra/terraform/outputs.tf)*
  - **Single-Origin URL Path Routing:** The Load Balancer exposes the web interface and both backend APIs under the same HTTPS domain—routing `/api/ingest/*` to `ingest-api`, `/api/agent/*` to `agent-api`, and all other paths (`/*`) to the Frontend service.  
    — *Provisioned in [`infra/terraform/load_balancer.tf`](infra/terraform/load_balancer.tf)*
  - **Disabled Edge CDN Caching for Private Traffic:** Cloud CDN caching is explicitly disabled across all Load Balancer backends so private multi-tenant API responses and live chat streams are never cached at the network edge.  
    — *Provisioned in [`infra/terraform/load_balancer.tf`](infra/terraform/load_balancer.tf)*

- **7.2 Locked-Down Cloud Run Ingress**
  - **Blocking Direct Container Access:** All three Cloud Run services are configured to accept traffic only from the internal Load Balancer (`INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER`). Anyone attempting to visit an underlying `*.run.app` service URL directly from the internet is automatically blocked at Google's network edge before the request ever reaches a container.  
    — *Provisioned in [`infra/terraform/cloud_run.tf`](infra/terraform/cloud_run.tf)*

- **7.3 Segmented Private VPC with Zero Outbound Internet**
  - **Custom VPC Without an Internet Exit:** Backend services run inside a custom Virtual Private Cloud (VPC) that has its default route to the public internet removed. Even if a malicious file or prompt attempted to trick a container into sending data to an outside server, the network drops the connection immediately.  
    — *Provisioned in [`infra/terraform/network.tf`](infra/terraform/network.tf)*
  - **Workload-Segmented Private Subnets:** The Ingestion service (`ingest-subnet`) and the Agent service (`agent-subnet`) are placed in separate private subnets to isolate document parsing traffic from agent execution at the network layer.  
    — *Provisioned in [`infra/terraform/network.tf`](infra/terraform/network.tf)*  
    — *Called in [`infra/terraform/cloud_run.tf`](infra/terraform/cloud_run.tf)*
  - **Serverless Direct VPC Egress:** Both backend services route 100% of their outbound traffic into the private VPC using serverless **Cloud Run Direct VPC Egress** (routing traffic strictly to private internal IP ranges without requiring always-on NAT or proxy VMs).  
    — *Provisioned in [`infra/terraform/cloud_run.tf`](infra/terraform/cloud_run.tf)*

- **7.4 Private Internal Tunnels to Google Cloud APIs**
  - **Private Google Access for Standard Google APIs:** A private Cloud DNS zone for `googleapis.com` and a restricted internal VPC route (`199.36.153.8/30`) direct all backend calls to Cloud Storage, Vertex AI, Identity Platform, Cloud Logging, Cloud Trace, and Cloud DLP over Google's private internal backbone rather than the public internet.  
    — *Provisioned in [`infra/terraform/network.tf`](infra/terraform/network.tf)*
  - **Regional Private Service Connect (PSC) for Model Armor:** Because regional Model Armor endpoints use a dedicated regional domain (`modelarmor.<region>.rep.googleapis.com`), the VPC provisions a dedicated Private Service Connect endpoint and private DNS zone so AI safety screening traffic stays entirely on private internal IP addresses.  
    — *Provisioned in [`infra/terraform/network.tf`](infra/terraform/network.tf)*

---

### 8. AI-Specific Security — 5 Pillars ([`docs/AI_SECURITY_PLAN.md`](docs/AI_SECURITY_PLAN.md))

Every user question, retrieved document chunk, and AI reply flows through a multi-stage security pipeline (prompt screening, iterative retrieval and reasoning, and output screening) organized around five pillars:

- **8.1 Pillar 1 — Prompt Injection Mitigation**
  - **Front-Door Prompt Screening:** Before calling Gemini, the agent's input screening stage sends the user's question to **Google Cloud Model Armor** to detect prompt-injection attempts, jailbreaks, and malicious links. Unsafe prompts are blocked immediately with a polite refusal without spending LLM tokens.  
    — *Provisioned in [`infra/terraform/ai_security.tf`](infra/terraform/ai_security.tf)*  
    — *Implemented in [`src/core/ai_security/armor.py`](src/core/ai_security/armor.py)*  
    — *Called in [`src/agent/graph.py`](src/agent/graph.py)*
  - **XML Spotlighting for Retrieved PDF Chunks:** Because malicious instructions hidden inside a PDF could try to impersonate system commands, every retrieved document chunk is wrapped inside `<retrieved_chunk source="..." page="...">` tags with any internal `<` or `>` characters escaped first so a document can never break out of its quotation box, and the system prompt instructs Gemini to treat all chunk content strictly as untrusted reference data.  
    — *Implemented in [`src/core/ai_security/guardrails.py`](src/core/ai_security/guardrails.py)*  
    — *Called in [`src/agent/tools.py`](src/agent/tools.py) & [`src/agent/graph.py`](src/agent/graph.py)*
  - **Safe Collection Naming:** Collection names are validated against a strict alphanumeric pattern across all endpoints and sanitized before binding to tool identifiers so attackers cannot smuggle prompt instructions into retriever tool descriptions via collection names.  
    — *Implemented in [`src/core/ai_security/guardrails.py`](src/core/ai_security/guardrails.py)*  
    — *Called in [`src/ingest/schemas.py`](src/ingest/schemas.py), [`src/ingest/upload_parser.py`](src/ingest/upload_parser.py), [`src/query/schemas.py`](src/query/schemas.py), [`src/agent/schemas.py`](src/agent/schemas.py), [`src/agent/tools.py`](src/agent/tools.py) & [`src/manage/collections.py`](src/manage/collections.py)*

- **8.2 Pillar 2 — Model Access Controls**
  - **Server-Approved Model Catalogs:** Instead of allowing callers to request arbitrary Vertex AI models, the backend maintains curated allowlists of approved chat models (`gemini-2.5-flash`, `gemini-2.5-pro`, `gemini-2.5-flash-lite`) and embedding models (`gemini-embedding-001`, `text-embedding-004`, `text-embedding-005`).  
    — *Provisioned in [`infra/terraform/cloud_run.tf`](infra/terraform/cloud_run.tf)*  
    — *Implemented in [`src/core/config.py`](src/core/config.py) & [`src/core/ai_security/guardrails.py`](src/core/ai_security/guardrails.py)*
  - **Automatic Endpoint Validation & Collection-Bound Embedding Enforcement:** Every ingestion, search, and chat request requires an explicit collection name, defaults to the primary approved model when omitted, immediately rejects unapproved model names with `HTTP 422`, and automatically enforces each existing collection's bound `embedding_model` across subsequent uploads, similarity searches, and agent retrieval calls.  
    — *Implemented in [`src/core/ai_security/guardrails.py`](src/core/ai_security/guardrails.py) & [`src/core/storage/chroma.py`](src/core/storage/chroma.py)*  
    — *Called in [`src/ingest/schemas.py`](src/ingest/schemas.py), [`src/ingest/upload_parser.py`](src/ingest/upload_parser.py), [`src/ingest/service.py`](src/ingest/service.py), [`src/query/schemas.py`](src/query/schemas.py), [`src/query/service.py`](src/query/service.py), [`src/agent/schemas.py`](src/agent/schemas.py) & [`src/agent/tools.py`](src/agent/tools.py)*

- **8.3 Pillar 3 — AI & Ingestion Guardrails**
  - **Strict Document Grounding Guardrails:** The system prompt and retriever tool enforce anti-hallucination guardrails requiring the model to rely solely on `<retrieved_chunk>` evidence with 1-indexed `[filename.pdf, p.X]` citations and abstain whenever retrieved context is empty or insufficient.  
    — *Implemented in [`src/core/ai_security/guardrails.py`](src/core/ai_security/guardrails.py), [`src/agent/tools.py`](src/agent/tools.py) & [`src/agent/graph.py`](src/agent/graph.py)*  
    — *Called in [`src/core/compliance/provenance.py`](src/core/compliance/provenance.py) & [`src/core/observability/telemetry.py`](src/core/observability/telemetry.py)*
  - **Query Length & Top-$k$ Search Bounds:** Request validators enforce maximum query text length (`4,000` characters) and similarity search result bounds (`k` between `1` and `50`).  
    — *Implemented in [`src/core/ai_security/guardrails.py`](src/core/ai_security/guardrails.py)*  
    — *Called in [`src/query/schemas.py`](src/query/schemas.py) & [`src/agent/schemas.py`](src/agent/schemas.py)*
  - **PDF File-Signature Verification & Upload Ceilings:** Ingestion validators enforce `%PDF-` magic-byte header verification, path-traversal filename sanitization, maximum file size (`20 MB` per PDF), maximum files per upload (`10`), and maximum extracted chunks per ingestion (`1,000`).  
    — *Implemented in [`src/core/ai_security/guardrails.py`](src/core/ai_security/guardrails.py)*  
    — *Called in [`src/ingest/upload_parser.py`](src/ingest/upload_parser.py) & [`src/ingest/splitter.py`](src/ingest/splitter.py)*
  - **Bounded Agent Retrieval Loops:** The agent's tool-calling loop is capped (`6` steps by default) so ambiguous questions cannot trigger endless retrieval cycles, returning a clean fallback message if the step limit is reached.  
    — *Implemented in [`src/core/config.py`](src/core/config.py) & [`src/core/ai_security/safety.py`](src/core/ai_security/safety.py)*  
    — *Called in [`src/agent/routes.py`](src/agent/routes.py) & [`src/agent/service.py`](src/agent/service.py)*

- **8.4 Pillar 4 — Output Filtering & Sanitized Errors**
  - **Automated PII & Secret Redaction Templates:** Terraform provisions **Google Cloud Sensitive Data Protection (Cloud DLP)** inspection and de-identification templates wired into **Model Armor** to automatically mask emails, phone numbers, credit card numbers, US Social Security numbers, IBAN codes, API keys, and auth tokens with `[INFO_TYPE]` placeholders (such as `[EMAIL_ADDRESS]`).  
    — *Provisioned in [`infra/terraform/ai_security.tf`](infra/terraform/ai_security.tf)*  
    — *Called in [`infra/terraform/cloud_run.tf`](infra/terraform/cloud_run.tf)*
  - **Two-Stage Redaction Across Search, JSON & Live Streaming:** Sensitive data is masked both when document chunks are retrieved from ChromaDB (protecting direct `/query` searches and retriever tool traces) and again on the final AI answer in the output screening stage before responses are returned or streamed over SSE.  
    — *Provisioned in [`infra/terraform/ai_security.tf`](infra/terraform/ai_security.tf)*  
    — *Implemented in [`src/core/ai_security/armor.py`](src/core/ai_security/armor.py)*  
    — *Called in [`src/query/service.py`](src/query/service.py), [`src/agent/tools.py`](src/agent/tools.py), [`src/agent/graph.py`](src/agent/graph.py) & [`src/agent/service.py`](src/agent/service.py)*
  - **Sanitized Server Error Messages:** Unexpected exceptions across all REST and streaming endpoints log full diagnostic stack traces privately on the server while returning a generic, safe error message to the client so internal details never leak.  
    — *Implemented in [`src/core/ai_security/safety.py`](src/core/ai_security/safety.py)*  
    — *Called in [`src/ingest/routes.py`](src/ingest/routes.py), [`src/query/routes.py`](src/query/routes.py), [`src/agent/routes.py`](src/agent/routes.py) & [`src/agent/service.py`](src/agent/service.py)*

- **8.5 Pillar 5 — Content Safety**
  - **Dual-Layer Harm Filtering (Model Armor RAI + Vertex AI Gemini):**
    1. **Google Cloud Model Armor Responsible AI (RAI) Filters** screen both incoming user prompts and outgoing model responses across Hate Speech, Harassment, Sexually Explicit, and Dangerous Content categories.  
       — *Provisioned in [`infra/terraform/ai_security.tf`](infra/terraform/ai_security.tf)*  
       — *Implemented in [`src/core/ai_security/armor.py`](src/core/ai_security/armor.py)*  
       — *Called in [`src/agent/graph.py`](src/agent/graph.py)*
    2. **Vertex AI Gemini Safety Thresholds** explicitly configure every Gemini model instance with pinned harm-blocking thresholds across all four safety categories.  
       — *Implemented in [`src/core/ai_security/safety.py`](src/core/ai_security/safety.py)*  
       — *Called in [`src/agent/graph.py`](src/agent/graph.py)*
  - **Polite Refusal Fallbacks:** Whenever Model Armor or Gemini blocks a request or halts generation due to content safety rules, the backend replaces the blocked or empty output with a clear, polite refusal explanation so users never see a blank message bubble.  
    — *Implemented in [`src/core/ai_security/safety.py`](src/core/ai_security/safety.py)*  
    — *Called in [`src/agent/graph.py`](src/agent/graph.py)*

---

### 9. Data Protection & Privacy ([`docs/AI_SECURITY_PLAN.md`](docs/AI_SECURITY_PLAN.md), [`docs/COMPLIANCE_GOVERNANCE_PLAN.md`](docs/COMPLIANCE_GOVERNANCE_PLAN.md))

Sensitive user documents, prompts, credentials, and logs are protected across their entire lifecycle through encryption, automated Cloud DLP redaction, privacy-preserving hashing, storage tier classification, and managed secrets:

- **9.1 Pillar 1 — Encryption at Rest & in Transit**
  - **End-to-End Encryption in Transit (HTTPS/TLS + Edge Redirect + Private Backbone):**
    - External browser and API traffic terminates TLS at the **Global External Application Load Balancer** using a Google-managed SSL/TLS certificate (`google_compute_managed_ssl_certificate.lb_cert` for `<IP-with-dashes>.sslip.io`), while plain HTTP requests on port `80` are automatically redirected to `https://` on port `443` with `301 Moved Permanently` (`google_compute_url_map.http_redirect`).
    - Direct internet access to Cloud Run `*.run.app` endpoints is blocked (`INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER`), and all backend calls to Cloud Storage, Vertex AI, Cloud DLP, Model Armor, and Cloud Logging travel over encrypted TLS channels inside Google's private network via **Private Google Access** (`199.36.153.8/30`) and **Regional Private Service Connect (PSC)**.  
    — *Provisioned in [`infra/terraform/load_balancer.tf`](infra/terraform/load_balancer.tf), [`infra/terraform/cloud_run.tf`](infra/terraform/cloud_run.tf) & [`infra/terraform/network.tf`](infra/terraform/network.tf)*
  - **Encryption at Rest & Ephemeral In-Memory Container Caches:**
    - All persistent cloud storage—including per-user ChromaDB snapshots and PDF archives in `google_storage_bucket.chroma_persistence`, remote Terraform state in `gs://<app>-tfstate-<project_number>`, regional Cloud Logging compliance and observability buckets, Artifact Registry images, and Secret Manager secrets—is encrypted at rest by default using Google-managed AES-256 encryption.
    - On Cloud Run Gen2, local `/tmp/chroma_db/{uid}` and `/tmp/uploads/{uid}/{request_id}` directories live exclusively in ephemeral in-memory `tmpfs` (never written to unencrypted persistent local disks) and are purged immediately after upload indexing, LRU cache eviction, or GDPR erasure.  
    — *Provisioned in [`infra/terraform/storage.tf`](infra/terraform/storage.tf), [`infra/terraform/compliance.tf`](infra/terraform/compliance.tf), [`infra/terraform/observability.tf`](infra/terraform/observability.tf) & [`infra/terraform/cloud_run.tf`](infra/terraform/cloud_run.tf)*  
    — *Implemented in [`src/ingest/upload_parser.py`](src/ingest/upload_parser.py), [`src/core/storage/locks.py`](src/core/storage/locks.py) & [`src/core/compliance/dsar.py`](src/core/compliance/dsar.py)*

- **9.2 Pillar 2 — Sensitive Data Protection (`Cloud DLP`)**
  - **Regional Cloud DLP Inspection & De-Identification Templates:** Terraform provisions a regional **Google Cloud Sensitive Data Protection (Cloud DLP)** inspect template (`google_data_loss_prevention_inspect_template.ai_output_inspect`, configured with `min_likelihood = "POSSIBLE"` across `EMAIL_ADDRESS`, `PHONE_NUMBER`, `CREDIT_CARD_NUMBER`, `US_SOCIAL_SECURITY_NUMBER`, `IBAN_CODE`, `GCP_API_KEY`, and `AUTH_TOKEN`) paired with a de-identify template (`google_data_loss_prevention_deidentify_template.ai_output_deidentify`) that replaces detected values with `[INFO_TYPE]` labels and attaches both to the regional **Model Armor** template (`sdp_settings.advanced_config`).  
    — *Provisioned in [`infra/terraform/ai_security.tf`](infra/terraform/ai_security.tf)*
  - **Two-Stage Parallel Redaction & Fail-Closed Enforcement:** [`src/core/ai_security/armor.py`](src/core/ai_security/armor.py) runs Cloud DLP redaction both at retrieval time (`redact_chunks`, screening top-$k$ chunks concurrently across up to `8` worker threads before they reach `/query` callers or the LLM context) and at output time (`sanitize_model_output`, screening the final Gemini response before JSON return or SSE emission), failing closed with `HTTP 503` in production if DLP/Model Armor is unavailable.  
    — *Implemented in [`src/core/ai_security/armor.py`](src/core/ai_security/armor.py)*  
    — *Called in [`src/query/service.py`](src/query/service.py), [`src/agent/tools.py`](src/agent/tools.py), [`src/agent/graph.py`](src/agent/graph.py) & [`src/agent/service.py`](src/agent/service.py)*

- **9.3 Pillar 3 — PII Handling & Privacy-by-Design**
  - **One-Way SHA-256 Query Hashing & Automatic Log Scrubbing:** Neither compliance audit logs (`interactiverag-audit`) nor operational logs/telemetry (`interactiverag-telemetry`) ever record raw user prompts or document text. [`hash_query()`](src/core/compliance/audit.py) converts user questions into deterministic one-way SHA-256 hashes (`query_sha256`) and character counts (`query_length`), while [`QueryScrubbingFilter`](src/core/observability/logging.py) intercepts any accidental `query`, `prompt`, or `raw_query` fields on standard log records before they reach `stdout`.  
    — *Implemented in [`src/core/compliance/audit.py`](src/core/compliance/audit.py) & [`src/core/observability/logging.py`](src/core/observability/logging.py)*
  - **Disabled Third-Party Telemetry & Edge Caching:** ChromaDB anonymized telemetry is disabled (`ANONYMIZED_TELEMETRY="FALSE"`), and Cloud CDN caching is disabled (`enable_cdn = false`) across all Load Balancer backends so private tenant responses are never cached at edge locations.  
    — *Provisioned in [`infra/terraform/cloud_run.tf`](infra/terraform/cloud_run.tf) & [`infra/terraform/load_balancer.tf`](infra/terraform/load_balancer.tf)*
  - **Self-Service GDPR Art. 15 Access, Art. 17 Erasure & 7-Day Version Hygiene:** Users can inspect their complete data footprint (`GET /compliance/my-data`) or permanently erase all live and archived Cloud Storage object versions (`versions=True`), local `/tmp` caches, and `{uid}:*` conversation threads (`DELETE /compliance/my-data`), complemented by GCS lifecycle rules that automatically delete archived object versions after 7 days or 3 newer snapshots.  
    — *Provisioned in [`infra/terraform/storage.tf`](infra/terraform/storage.tf)*  
    — *Implemented in [`src/core/compliance/dsar.py`](src/core/compliance/dsar.py) & [`src/manage/compliance.py`](src/manage/compliance.py)*

- **9.4 Pillar 4 — Data Classification & Storage Segregation**
  - **Architectural Segregation Across Four Sensitivity & Retention Tiers:** Data is classified by sensitivity and lifecycle and routed into segregated storage boundaries:
    1. **Tier 1 — Restricted Tenant Content (Uploaded PDFs, Vector Indexes & Chat Threads):** Isolated per verified `uid` (`gs://<bucket>/users/{uid}/`, `/tmp/chroma_db/{uid}`, `{uid}:{thread_id}`); treated as untrusted, potentially PII-bearing data subject to XML spotlighting, two-stage Cloud DLP redaction, 7-day noncurrent version pruning, and self-service GDPR Art. 17 erasure.
    2. **Tier 2 — Confidential Credentials & Identity Material (OAuth Client Secrets & Bearer JWTs):** Stored in **Google Cloud Secret Manager** or verified in memory via RS256 public keys; never persisted in `.tf` files, container images, or logs, and actively scanned by Cloud DLP (`GCP_API_KEY`, `AUTH_TOKEN`).
    3. **Tier 3 — Regulated Compliance Audit Records (`interactiverag-audit` & GCP Data Access Logs):** Scrubbed of raw PII (`query_sha256` only) and routed to a dedicated regional Cloud Logging bucket (`compliance_audit`) with **365-day retention** and optional WORM retention lock (`var.audit_log_bucket_locked`).
    4. **Tier 4 — Operational & AI Telemetry Diagnostics (`interactiverag-telemetry` & Cloud Trace Spans):** Scrubbed of raw prompts and routed to a separate regional Cloud Logging bucket (`observability_telemetry`) with **30-day retention**.  
    — *Provisioned in [`infra/terraform/storage.tf`](infra/terraform/storage.tf), [`infra/terraform/auth.tf`](infra/terraform/auth.tf), [`infra/terraform/ai_security.tf`](infra/terraform/ai_security.tf), [`infra/terraform/compliance.tf`](infra/terraform/compliance.tf) & [`infra/terraform/observability.tf`](infra/terraform/observability.tf)*  
    — *Implemented in [`src/core/storage/paths.py`](src/core/storage/paths.py), [`src/core/compliance/audit.py`](src/core/compliance/audit.py) & [`src/core/observability/telemetry.py`](src/core/observability/telemetry.py)*

- **9.5 Pillar 5 — Secrets Management**
  - **Google Cloud Secret Manager Integration for OAuth Credentials:** Sensitive OAuth 2.0 credentials are never stored in Terraform files or `terraform.tfvars`; [`infra/terraform/variables.tf`](infra/terraform/variables.tf) holds only the Secret Manager secret names (`var.google_oauth_client_id_secret_name` and `var.google_oauth_client_secret_name`), and [`infra/terraform/auth.tf`](infra/terraform/auth.tf) fetches the secret versions from **Google Cloud Secret Manager** (`data "google_secret_manager_secret_version"`) at apply time.  
    — *Provisioned in [`infra/terraform/auth.tf`](infra/terraform/auth.tf) & [`infra/terraform/variables.tf`](infra/terraform/variables.tf)*
  - **Keyless Runtime Auth, Build-Context Exclusions & Output Secret Scanning:** Backend containers authenticate to GCP services via IAM/ADC without static secret keys; [`.gitignore`](.gitignore) and [`.dockerignore`](.dockerignore) exclude `.env` and `infra/terraform/terraform.tfvars` from Git history and Docker image layers; [`sanitize_error_message()`](src/core/ai_security/safety.py) prevents internal stack traces or config values from leaking in error responses; and Cloud DLP automatically masks `GCP_API_KEY` and `AUTH_TOKEN` patterns in retrieved chunks and LLM outputs.  
    — *Provisioned in [`infra/terraform/iam.tf`](infra/terraform/iam.tf) & [`infra/terraform/ai_security.tf`](infra/terraform/ai_security.tf)*  
    — *Implemented in [`.gitignore`](.gitignore), [`.dockerignore`](.dockerignore) & [`src/core/ai_security/safety.py`](src/core/ai_security/safety.py)*

> **Note on Unimplemented Pillars:**
> - **Customer-Managed Encryption Keys (Cloud KMS CMEK) & Automated Per-Document Sensitivity Labeling (*Not Implemented — Relevant*):** While all buckets and logs are encrypted at rest with Google-managed encryption keys (GMEK) and data is segregated by architectural tier and scanned by Cloud DLP across 7 PII/credential `info_types`, Customer-Managed Encryption Keys (Cloud KMS CMEK) and automated document-level sensitivity tagging (e.g., classifying individual uploaded PDFs as `PUBLIC` / `INTERNAL` / `CONFIDENTIAL` in metadata at ingestion time) are not yet implemented and remain relevant future enhancements for regulated enterprises.

---

### 10. Compliance & Governance ([`docs/COMPLIANCE_GOVERNANCE_PLAN.md`](docs/COMPLIANCE_GOVERNANCE_PLAN.md))

Enterprise governance controls ensure that all data and AI calls stay within approved single-region boundaries, every layer of a request is auditable without exposing personal data, users have self-service privacy controls and AI transparency badges, and operational policies are enforced automatically:

- **10.1 Pillar 1 — Data Residency & Single-Region Pinning**
  - **Live Organization Policy Verification:** Allowed geographic regions are governed by the project's **Google Cloud Resource Location Restriction** Organization Policy. Before any deployment runs, an automated check queries the live Organization Policy on the project and aborts if no location allowlist is active or if the target region is not permitted.  
    — *Implemented in [`infra/scripts/check_region_policy.py`](infra/scripts/check_region_policy.py)*  
    — *Called in [`Makefile`](Makefile)*
  - **Infrastructure Single-Region Validation:** Terraform validates that the configured deployment location is a concrete single region (such as `us-central1` or `europe-west1`)—rejecting `"global"` and multi-regions (`"us"`, `"eu"`, `"asia"`)—and enforces regional preconditions on storage buckets, security templates, and private endpoints.  
    — *Provisioned in [`infra/terraform/storage.tf`](infra/terraform/storage.tf), [`infra/terraform/ai_security.tf`](infra/terraform/ai_security.tf), [`infra/terraform/network.tf`](infra/terraform/network.tf) & [`infra/terraform/cloud_run.tf`](infra/terraform/cloud_run.tf)*
  - **Backend Regional Endpoint Binding:** At runtime, the Python backend validates that the configured location is a single region and explicitly binds all Vertex AI chat models, Vertex AI embedding models, and Model Armor clients to that regional endpoint.  
    — *Implemented in [`src/core/compliance/residency.py`](src/core/compliance/residency.py)*  
    — *Called in [`src/core/storage/chroma.py`](src/core/storage/chroma.py), [`src/agent/graph.py`](src/agent/graph.py) & [`src/core/ai_security/armor.py`](src/core/ai_security/armor.py)*

- **10.2 Pillar 2 — Privacy-Safe Audit Logging, GCP Data Access Logs & Trace Correlation**
  - **End-to-End Request & Trace Correlation:** A correlation middleware assigns or forwards a unique request ID (`X-Request-Id`) on every call and links it with Google Cloud's trace context so administrators can filter by a single ID in **Logs Explorer** to trace a request across the Load Balancer, backend microservices, AI safety checks, and cloud storage operations.  
    — *Implemented in [`src/core/context.py`](src/core/context.py) & [`src/core/compliance/audit.py`](src/core/compliance/audit.py)*  
    — *Called in [`src/core/app_factory.py`](src/core/app_factory.py)*
  - **PII-Free Application Audit Stream:** Backend services publish structured JSON audit records to a dedicated audit stream in **Google Cloud Logging** for authentication failures, document uploads, searches, agent chats, collection changes, privacy requests, rate-limit blocks, and safety filter actions—replacing raw user questions with one-way SHA-256 hashes and character counts so personal data never leaks into logs.  
    — *Implemented in [`src/core/compliance/audit.py`](src/core/compliance/audit.py)*  
    — *Called in [`src/core/auth.py`](src/core/auth.py), [`src/ingest/routes.py`](src/ingest/routes.py), [`src/query/routes.py`](src/query/routes.py), [`src/agent/routes.py`](src/agent/routes.py), [`src/agent/service.py`](src/agent/service.py), [`src/manage/collections.py`](src/manage/collections.py) & [`src/manage/compliance.py`](src/manage/compliance.py)*
  - **GCP Data Access Audit Logs & Load Balancer Access Logs:** Terraform enables Google Cloud Data Access Audit Logs (covering administrative reads, data reads, and data writes) for Cloud Storage, Vertex AI, Cloud DLP, and Model Armor alongside HTTP access logging on the HTTPS Load Balancer backends.  
    — *Provisioned in [`infra/terraform/compliance.tf`](infra/terraform/compliance.tf) & [`infra/terraform/load_balancer.tf`](infra/terraform/load_balancer.tf)*
  - **365-Day Regional Audit Log Archive:** A dedicated Cloud Logging sink routes application audit events, GCP Data Access Audit Logs, and Load Balancer HTTP access logs into a regional Cloud Logging bucket configured with 365-day retention.  
    — *Provisioned in [`infra/terraform/compliance.tf`](infra/terraform/compliance.tf)*

- **10.3 Pillar 3 — Regulatory Compliance (GDPR Self-Service Rights & EU AI Act Art. 50 Provenance)**
  - **GDPR Art. 15 Right of Access (`GET /compliance/my-data`):** Authenticated users can view a complete inventory of their stored collections, chunk counts, uploaded PDF filenames, live and archived Cloud Storage object counts, total storage bytes, and active chat threads via the API or the **"My Data & Privacy"** modal in the web interface.  
    — *Implemented in [`src/core/compliance/dsar.py`](src/core/compliance/dsar.py)*  
    — *Called in [`src/manage/compliance.py`](src/manage/compliance.py) & [`frontend/src/App.jsx`](frontend/src/App.jsx)*
  - **GDPR Art. 17 Right to Erasure (`DELETE /compliance/my-data`) & Storage Hygiene:** Users can permanently erase all of their data in one action—deleting every live and archived object version in Cloud Storage, wiping local vector and upload caches, and clearing all conversation history from agent memory—complemented by Cloud Storage lifecycle rules that automatically prune archived object versions after 7 days.  
    — *Provisioned in [`infra/terraform/storage.tf`](infra/terraform/storage.tf)*  
    — *Implemented in [`src/core/compliance/dsar.py`](src/core/compliance/dsar.py)*  
    — *Called in [`src/manage/compliance.py`](src/manage/compliance.py) & [`frontend/src/App.jsx`](frontend/src/App.jsx)*
  - **EU AI Act Art. 50 AI Provenance & Transparency Badges:** Every `/agent` JSON response and `/agent/stream` SSE completion event includes structured provenance metadata and renders visual verification badges in the chat UI allowing the user to see:
    - **AI Generation, Model & Region:** Whether the reply was AI-generated or stopped by a guardrail, which exact **Gemini model** and **cloud region** produced it, and the correlating request ID.
    - **Citation Grounding:** Whether document grounding was enforced, whether the answer is backed by inline `[filename.pdf, p.X]` citations, and how many chunks were retrieved.
    - **PII Redaction & Content Safety Activation:** Whether sensitive personal data (PII) was detected and redacted, whether Model Armor screening was active, which security filters ran, and whether any content safety or prompt-protection guardrails were triggered.  
    — *Implemented in [`src/core/compliance/provenance.py`](src/core/compliance/provenance.py) & [`src/core/compliance/state.py`](src/core/compliance/state.py)*  
    — *Called in [`src/agent/schemas.py`](src/agent/schemas.py), [`src/agent/routes.py`](src/agent/routes.py), [`src/agent/service.py`](src/agent/service.py) & [`frontend/src/App.jsx`](frontend/src/App.jsx)*

- **10.4 Pillar 4 — Policy Enforcement (Fail-Closed Screening, Rate Limits, Quotas & Pre-Deploy Gates)**
  - **Fail-Closed Security Screening in Production:** In production deployments, if Google Cloud Model Armor or Cloud DLP is unreachable, misconfigured, or times out during prompt screening, chunk redaction, or output filtering, the backend immediately halts processing, logs a high-severity audit event, and returns `HTTP 503 Service Unavailable` so unscreened content is never exposed.  
    — *Provisioned in [`infra/terraform/cloud_run.tf`](infra/terraform/cloud_run.tf)*  
    — *Implemented in [`src/core/ai_security/armor.py`](src/core/ai_security/armor.py)*  
    — *Called in [`src/ingest/routes.py`](src/ingest/routes.py), [`src/query/routes.py`](src/query/routes.py), [`src/agent/routes.py`](src/agent/routes.py) & [`src/agent/service.py`](src/agent/service.py)*
  - **Per-User Sliding-Window Rate Limiters:** Sliding-window rate limiters protect each user account on document uploads (`10/minute` default) and queries (`30/minute` default), returning `HTTP 429 Too Many Requests` with a `Retry-After` header when exceeded.  
    — *Provisioned in [`infra/terraform/cloud_run.tf`](infra/terraform/cloud_run.tf)*  
    — *Implemented in [`src/core/compliance/policy.py`](src/core/compliance/policy.py)*  
    — *Called in [`src/ingest/upload_parser.py`](src/ingest/upload_parser.py), [`src/query/routes.py`](src/query/routes.py) & [`src/agent/routes.py`](src/agent/routes.py)*
  - **Per-User Collection Count Quotas:** Each user is capped at a maximum number of collections (`20` default), returning `HTTP 403 Forbidden` when attempting to create a new collection past the limit while still allowing uploads into existing collections.  
    — *Provisioned in [`infra/terraform/cloud_run.tf`](infra/terraform/cloud_run.tf)*  
    — *Implemented in [`src/core/compliance/policy.py`](src/core/compliance/policy.py)*  
    — *Called in [`src/ingest/upload_parser.py`](src/ingest/upload_parser.py)*
  - **Cloud Run Production Deletion Protection:** Terraform enables accidental deletion protection on Cloud Run services in production deployments.  
    — *Provisioned in [`infra/terraform/cloud_run.tf`](infra/terraform/cloud_run.tf)*
  - **Automated Pre-Deploy Policy Gate (`make policy-check`):** Automated policy checks verify live Organization Policy compliance, single-region settings, fail-closed screening, and Terraform validity before any `make plan` or `make deploy` runs.  
    — *Implemented in [`infra/scripts/check_static_policies.py`](infra/scripts/check_static_policies.py) & [`infra/scripts/check_region_policy.py`](infra/scripts/check_region_policy.py)*  
    — *Called in [`Makefile`](Makefile)*

---

### 11. Observability & AI Monitoring ([`docs/OBSERVABILITY_PLAN.md`](docs/OBSERVABILITY_PLAN.md))

While compliance audit logging records *who* performed *what* action and *when* for regulatory accountability, the observability layer gives operators real-time visibility into *how fast*, *at what token cost*, and *with what retrieval and grounding quality* every request executed:

- **11.1 Pillar 1 — Structured Logging & Privacy-Safe Correlation**
  - **Dual-Mode Application Logging:** When running in the cloud, backend services emit single-line JSON log records containing severity levels, timestamps, source file locations, and trace identifiers so **Google Cloud Logging** can index and filter them automatically—while preserving readable color-coded terminal output during local development.  
    — *Provisioned in [`infra/terraform/cloud_run.tf`](infra/terraform/cloud_run.tf)*  
    — *Implemented in [`src/core/observability/logging.py`](src/core/observability/logging.py)*  
    — *Called in [`src/core/logger.py`](src/core/logger.py)*
  - **Shared Request Correlation Context:** Every operational log entry is automatically enriched with the active request ID, trace ID, span ID, service name, and authenticated user ID so logs from different components can be correlated without coupling operational logging to compliance audit modules.  
    — *Implemented in [`src/core/context.py`](src/core/context.py)*  
    — *Called in [`src/core/observability/logging.py`](src/core/observability/logging.py) & [`src/core/compliance/audit.py`](src/core/compliance/audit.py)*
  - **Automatic Query Scrubbing Filter:** A built-in log filter automatically intercepts any accidental raw query text before it is written to standard output and replaces it with a one-way SHA-256 hash and character count so user questions never leak into operational logs.  
    — *Implemented in [`src/core/observability/logging.py`](src/core/observability/logging.py)*  
    — *Called in [`src/core/logger.py`](src/core/logger.py)*
  - **Dedicated Regional Observability Log Bucket:** Operational container logs and AI performance telemetry are routed into a dedicated regional **Google Cloud Logging** bucket with a 30-day retention policy, keeping high-volume operational diagnostics separate from the 365-day compliance audit archive.  
    — *Provisioned in [`infra/terraform/observability.tf`](infra/terraform/observability.tf)*

- **11.2 Pillar 2 — Cloud Monitoring Metrics & AI Observability Dashboard**
  - **Zero-Overhead Log-Based Metrics:** Instead of making extra network calls to a monitoring API during user requests, **Google Cloud's Log Router** automatically extracts 10 custom performance and quality metrics directly from structured telemetry logs—tracking end-to-end turn latency, time-to-first-token, LLM vs. retrieval stage latency, input/output/total token consumption, retrieval similarity scores, ungrounded answer counts, and ingested chunk throughput with zero request-path overhead.  
    — *Provisioned in [`infra/terraform/observability.tf`](infra/terraform/observability.tf)*  
    — *Called in [`src/core/observability/telemetry.py`](src/core/observability/telemetry.py)*
  - **AI Quality, Latency & Token Dashboard:** Terraform provisions a **Google Cloud Monitoring** dashboard that gives operators a single view of median and tail (95th-percentile) response latency, time-to-first-token, Gemini reasoning vs. vector search stage durations, token consumption by model, retrieval similarity score distributions, ungrounded or blocked response rates, document ingestion throughput, and HTTP traffic volume.  
    — *Provisioned in [`infra/terraform/observability.tf`](infra/terraform/observability.tf)*

- **11.3 Pillar 3 — Distributed Tracing & Stage-Level Latency Breakdown**
  - **Hierarchical Per-Stage Execution Spans:** Every major step of a request—including Cloud Storage synchronization, PDF page splitting, Vertex AI embedding generation, Model Armor input screening, each Gemini reasoning step, ChromaDB similarity search, and output safety screening—is timed as an individual child span linked to the parent request trace so operators can pinpoint exactly which stage dominates latency.  
    — *Implemented in [`src/core/observability/tracing.py`](src/core/observability/tracing.py)*  
    — *Called in [`src/core/storage/gcs_sync.py`](src/core/storage/gcs_sync.py), [`src/ingest/splitter.py`](src/ingest/splitter.py), [`src/ingest/service.py`](src/ingest/service.py), [`src/query/service.py`](src/query/service.py), [`src/agent/graph.py`](src/agent/graph.py) & [`src/agent/tools.py`](src/agent/tools.py)*
  - **Cloud Trace Waterfall Export:** In cloud deployments, completed execution spans are exported to **Google Cloud Trace** so operators can inspect end-to-end request waterfall timelines alongside correlated application logs.  
    — *Provisioned in [`infra/terraform/cloud_run.tf`](infra/terraform/cloud_run.tf) & [`infra/terraform/iam.tf`](infra/terraform/iam.tf)*  
    — *Implemented in [`src/core/observability/tracing.py`](src/core/observability/tracing.py)*  
    — *Called in [`src/core/app_factory.py`](src/core/app_factory.py)*

- **11.4 Pillar 4 — AI-Specific Monitoring (Latency, Token Usage & Retrieval Quality)**
  - **Turn Latency, Stage Breakdown & Time-to-First-Token:** Every completed ingestion, similarity search, and agent conversation turn emits a structured telemetry summary recording total wall-clock duration, a millisecond breakdown across internal pipeline stages, and—for live streaming chats—the Time-to-First-Token (how quickly the user sees the first streamed event).  
    — *Implemented in [`src/core/observability/telemetry.py`](src/core/observability/telemetry.py) & [`src/core/observability/state.py`](src/core/observability/state.py)*  
    — *Called in [`src/ingest/routes.py`](src/ingest/routes.py), [`src/query/routes.py`](src/query/routes.py), [`src/agent/routes.py`](src/agent/routes.py) & [`src/agent/service.py`](src/agent/service.py)*
  - **Multi-Step Gemini Token Accounting:** Because an agent turn may invoke Gemini multiple times across iterative tool-calling steps, the backend extracts prompt, completion, and total token counts from each model call and aggregates total token usage and LLM call counts across the entire conversation turn for accurate cost tracking.  
    — *Implemented in [`src/core/observability/telemetry.py`](src/core/observability/telemetry.py)*  
    — *Called in [`src/agent/graph.py`](src/agent/graph.py)*
  - **Deterministic Retrieval & Grounding Quality Signals:** Every search and agent turn records normalized vector similarity scores (top and average match quality), the number of retriever tool calls and chunks fetched, the number of inline `[filename.pdf, p.X]` citations produced, and whether the final answer was properly grounded—providing continuous retrieval quality signals without paying for extra LLM-as-a-judge calls.  
    — *Implemented in [`src/core/observability/telemetry.py`](src/core/observability/telemetry.py)*  
    — *Called in [`src/agent/tools.py`](src/agent/tools.py), [`src/query/service.py`](src/query/service.py), [`src/agent/routes.py`](src/agent/routes.py) & [`src/agent/service.py`](src/agent/service.py)*

---

### 12. Scalability, Elasticity & Resource Efficiency ([`docs/SCALABILITY_ELASTICITY_PLAN.md`](docs/SCALABILITY_ELASTICITY_PLAN.md))

InteractiveRAG combines workload-isolated microservice packaging with compute right-sizing, scale-to-zero autoscaling, bounded in-memory `/tmp` cache management, and incremental Cloud Storage delta sync so heavy PDF uploads never degrade interactive user latency while minimizing compute and storage cost:

- **12.1 Workload-Isolated Microservice Packaging (4 Packages Across 2 Backend Services)**
  - **Dedicated Heavy-Write Ingestion Service (`ingest-api`):** Runs only the document ingestion package (`POST /ingest`) on dedicated instances so CPU-intensive PDF parsing and vector index construction never compete with or slow down live user chats.  
    — *Provisioned in [`infra/terraform/cloud_run.tf`](infra/terraform/cloud_run.tf)*  
    — *Implemented in [`src/ingest/routes.py`](src/ingest/routes.py)*  
    — *Called in [`src/ingest/app.py`](src/ingest/app.py)*
  - **Unified Interactive Read, Management & Agent Service (`agent-api`):** Co-locates collection management, direct similarity search, and multi-turn LangGraph agent execution on a single interactive service so searches and follow-up chat turns share the same warm local vector cache, unified query rate limiter, and atomic GDPR data inventory and erasure workflow.  
    — *Provisioned in [`infra/terraform/cloud_run.tf`](infra/terraform/cloud_run.tf)*  
    — *Implemented in [`src/agent/routes.py`](src/agent/routes.py), [`src/query/routes.py`](src/query/routes.py) & [`src/manage/routes.py`](src/manage/routes.py)*  
    — *Called in [`src/agent/app.py`](src/agent/app.py)*

- **12.2 Compute Right-Sizing & Autoscaling Strategies (Horizontal, Vertical & Network Profiles)**
  - **Right-Sized Vertical CPU & Memory Allocations (`Compute Right-Sizing`):** Rather than over-provisioning every container for peak PDF parsing memory, compute resources are right-sized per workload—provisioning `ingest-api` with `2 vCPU` and `4 GiB` of RAM (for concurrent `PyPDFLoader` parsing, batch embedding generation, and `/tmp` HNSW index writes), `agent-api` with `2 vCPU` and `2 GiB` of RAM (for I/O-bound LangGraph streaming and warm `/tmp` vector cache reads), and `frontend` with `1 vCPU` and `512 MiB` of RAM.  
    — *Provisioned in [`infra/terraform/cloud_run.tf`](infra/terraform/cloud_run.tf) & [`infra/terraform/variables.tf`](infra/terraform/variables.tf)*
  - **Request-Scoped CPU Billing (`cpu_idle = true`) & Cold-Start Boost (`startup_cpu_boost = true`):** All three Cloud Run services enable `cpu_idle = true` (throttling CPU and billing only while a container is actively handling a request) and `startup_cpu_boost = true` (temporarily boosting CPU during container initialization to reduce Python/LangChain cold-start latency without paying for permanently oversized CPU).  
    — *Provisioned in [`infra/terraform/cloud_run.tf`](infra/terraform/cloud_run.tf)*
  - **Workload-Tuned Request Concurrency Ceilings:** Each Cloud Run service enforces a per-instance request concurrency limit tailored to its workload—limiting `ingest-api` to `4` concurrent uploads per container to prevent CPU contention, allowing `32` concurrent interactive requests per container on `agent-api` (since chat and search spend most of their time waiting on external Vertex AI / DLP network I/O), and `80` concurrent requests on the static `frontend`.  
    — *Provisioned in [`infra/terraform/cloud_run.tf`](infra/terraform/cloud_run.tf) & [`infra/terraform/variables.tf`](infra/terraform/variables.tf)*
  - **Scale-to-Zero Elasticity & Service-Specific Horizontal Scale-Out Caps (`Autoscaling Strategies`):** Cloud Run automatically scales each service from `min_instance_count = 0` (zero idle compute cost when no requests are active, configurable via `var.*_min_instances`) up to service-specific maximums—capping `ingest-api` at `5` instances (`20` concurrent PDF uploads) to protect downstream Vertex AI and Cloud DLP quotas, while scaling `agent-api` up to `10` instances (`320` concurrent interactive requests) and `frontend` up to `5` instances (`400` concurrent requests).  
    — *Provisioned in [`infra/terraform/cloud_run.tf`](infra/terraform/cloud_run.tf) & [`infra/terraform/variables.tf`](infra/terraform/variables.tf)*
  - **Session Affinity for Multi-Turn Conversations:** Cloud Run `session_affinity = true` is enabled on `agent-api` so consecutive turns from the same user prefer the same warm container instance, maximizing hits on the user's in-memory `MEMORY_SAVER` checkpointer and local `/tmp/chroma_db/{uid}` vector cache.  
    — *Provisioned in [`infra/terraform/cloud_run.tf`](infra/terraform/cloud_run.tf)*
  - **Expanded `/24` Private VPC Subnets for Scale-Out Headroom:** Both private backend subnets are sized as `/24` CIDR blocks (`251` usable IP addresses each), ensuring serverless Direct VPC Egress never runs out of private IP addresses during traffic spikes or revision rollouts.  
    — *Provisioned in [`infra/terraform/network.tf`](infra/terraform/network.tf) & [`infra/terraform/variables.tf`](infra/terraform/variables.tf)*  
    — *Called in [`infra/terraform/cloud_run.tf`](infra/terraform/cloud_run.tf)*
  - **Bounded In-Memory `/tmp` Vector Cache Quotas:** Because Cloud Run mounts `/tmp` directly inside container RAM, both backend services enforce a `512 MiB` cap (`CHROMA_TMP_MAX_BYTES`) on cached user vector databases and maintain at least `256 MiB` of free container memory (`CHROMA_TMP_MIN_FREE_BYTES`)—automatically evicting the oldest inactive user caches via LRU before memory pressure can trigger an out-of-memory crash.  
    — *Provisioned in [`infra/terraform/cloud_run.tf`](infra/terraform/cloud_run.tf) & [`infra/terraform/variables.tf`](infra/terraform/variables.tf)*  
    — *Implemented in [`src/core/storage/locks.py`](src/core/storage/locks.py)*  
    — *Called in [`src/core/storage/gcs_sync.py`](src/core/storage/gcs_sync.py)*

- **12.3 API Throughput, Incremental Storage Sync & Scaling Observability**
  - **Parallel Retrieval-Time Privacy Screening:** When a similarity search or agent tool call retrieves top-$k$ chunks from ChromaDB, the backend screens all retrieved chunks for sensitive personal data concurrently across a bounded pool of up to `8` worker threads (`1` chunk per thread) instead of scanning them one after another—cutting retrieval-stage screening latency to the duration of a single chunk scan.  
    — *Implemented in [`src/core/ai_security/armor.py`](src/core/ai_security/armor.py)*  
    — *Called in [`src/query/service.py`](src/query/service.py) & [`src/agent/tools.py`](src/agent/tools.py)*
  - **Incremental MD5 Cloud Storage Delta Synchronization:** When pushing updated ChromaDB files back to Cloud Storage after an upload or collection deletion, the backend compares local file MD5 checksums against remote Cloud Storage object metadata and uploads only new or modified files, avoiding redundant multi-megabyte uploads as user vector databases grow.  
    — *Implemented in [`src/core/storage/gcs_sync.py`](src/core/storage/gcs_sync.py)*  
    — *Called in [`src/ingest/routes.py`](src/ingest/routes.py) & [`src/manage/collections.py`](src/manage/collections.py)*
  - **Infrastructure Scaling & Utilization Dashboard Panels:** The Cloud Monitoring dashboard includes dedicated scaling panels charting active vs. idle container instance counts, 95th-percentile concurrent requests per instance, CPU utilization, and container memory utilization across all services.  
    — *Provisioned in [`infra/terraform/observability.tf`](infra/terraform/observability.tf)*
  - **Automated Latency, Memory Saturation & Error Alert Policies:** Cloud Monitoring alert policies automatically notify operators whenever 95th-percentile agent turn latency exceeds `15 seconds` over a sustained `5-minute` window, container memory utilization exceeds `85%` over `3 minutes`, or server `5xx` error rates exceed `0.05 requests/second` over `2 minutes`.  
    — *Provisioned in [`infra/terraform/observability.tf`](infra/terraform/observability.tf)*

> **Note on Unimplemented / Non-Relevant Pillars:**
> - **GPU Right-Sizing & Spot / Preemptible Compute Usage (*Not Implemented — Not Relevant*):** Provisioning or right-sizing GPUs and Spot/Preemptible VMs (Compute Engine or GKE node pools) is not relevant to InteractiveRAG's serverless architecture because all deep-learning inference (Gemini 2.5 reasoning, dense vector embedding generation, and Model Armor safety screening) is offloaded to fully managed, pay-per-token **Google Cloud Vertex AI** and **Model Armor** regional APIs, while the application tier runs on serverless **Google Cloud Run** containers with scale-to-zero (`min_instance_count = 0`) and request-scoped CPU billing (`cpu_idle = true`), eliminating idle VM and GPU hardware costs entirely.

---

### 13. Testing & Quality Engineering ([`tests/`](tests/), [`infra/scripts/`](infra/scripts/))

Quality, security, and multi-tenant isolation are verified across unit, integration, end-to-end, and infrastructure policy layers before code or Terraform changes are deployed:

- **13.1 Pillar 1 — Unit, Integration & End-to-End (E2E) Testing**
  - **Deterministic Offline Unit Test Suites (`tests/`):** Six domain-specific `unittest` suites verify every backend and infrastructure layer completely offline using deterministic in-memory test doubles in [`tests/helpers.py`](tests/helpers.py) (`FakeGCSBucket` & `FakeGCSBlob` supporting atomic `if_generation_match` preconditions, MD5 digests, and object versioning; `DeterministicFakeEmbeddings`; `GroundedCitingChatModel`; `make_test_pdf`; and Model Armor/Cloud DLP response builders):
    - [`tests/auth.py`](tests/auth.py) — RS256 Bearer JWT verification, expired/revoked/forged token rejection (`HTTP 401`), UID path-traversal validation, local dev bypass, and Cloud Run fail-closed enforcement.
    - [`tests/storage.py`](tests/storage.py) — Per-user GCS $\leftrightarrow$ `/tmp` snapshot synchronization, `version.json` tracking, incremental MD5 delta uploads, atomic GCS distributed write locks with stale-lock TTL recovery, bounded `/tmp` LRU cache eviction, and hybrid dense + BM25 RRF search.
    - [`tests/ai_security.py`](tests/ai_security.py) — All 5 AI security pillars (Model Armor prompt injection screening, XML spotlighting, model allowlists & collection-bound embedding consistency, PDF `%PDF-` magic-byte/size/chunk guardrails, two-stage Cloud DLP redaction, sanitized errors, and dual-layer RAI content safety).
    - [`tests/compliance.py`](tests/compliance.py) — Single-region residency validation, live Org Policy parser checks, PII-free SHA-256 audit logging, GDPR Art. 15 inventory & Art. 17 versioned erasure, EU AI Act Art. 50 provenance metadata, fail-closed `HTTP 503` screening, sliding-window rate limiters (`HTTP 429`), and collection quotas (`HTTP 403`).
    - [`tests/observability.py`](tests/observability.py) — Structured GCP JSON logging, automatic query scrubbing filter, trace/span correlation, hierarchical per-stage latency breakdown, Time-to-First-Token (TTFT), multi-step Gemini token accounting, and retrieval/grounding telemetry.
    - [`tests/scalability_elasticity.py`](tests/scalability_elasticity.py) — 4-package/2-service route isolation (`ingest-api` vs. `agent-api`), Cloud Run concurrency/CPU/memory/session-affinity HCL invariants, `/24` VPC subnet sizing, `/tmp` cache eviction under memory pressure, parallel DLP chunk screening, and Cloud Monitoring scaling alerts.
  - **Multi-Tenant FastAPI Integration Suite (`tests/multi_tenant_api.py`):** Exercises the assembled FastAPI microservices (`src.ingest.app:app` and `src.agent.app:app`) via `fastapi.testclient.TestClient` across multi-user PDF upload, GCS archiving, similarity search, collection management, GDPR erasure, and live SSE streaming (`POST /agent/stream`), verifying that User Alice and User Bob remain strictly isolated even when creating collections and conversation threads with identical names.  
    — *Implemented in [`tests/multi_tenant_api.py`](tests/multi_tenant_api.py) & [`tests/helpers.py`](tests/helpers.py)*
  - **Live End-to-End HTTP & SSE Smoke Scripts (`tests/ingestion.py` & `tests/agent.py`):** Live E2E smoke tests execute against locally running `ingest-api` (port `8000`) and `agent-api` (port `8001`) instances using real PDF files in [`data/`](data/), local ChromaDB persistence, and live Vertex AI embeddings and Gemini tool-calling across both synchronous JSON (`POST /agent`) and Server-Sent Events streaming (`POST /agent/stream`).  
    — *Implemented in [`tests/ingestion.py`](tests/ingestion.py) & [`tests/agent.py`](tests/agent.py)*

- **13.2 Pillar 2 — Test & Policy Gate Automation**
  - **Automated Pre-Deploy Policy & HCL Verification (`make policy-check`):** [`Makefile`](Makefile) automates [`infra/scripts/check_static_policies.py`](infra/scripts/check_static_policies.py) (`make check-static-policy`), [`infra/scripts/check_region_policy.py`](infra/scripts/check_region_policy.py) (`make check-region-policy`), and `terraform validate` (`make validate`) as mandatory pre-flight gates before `make plan` and `make deploy`.  
    — *Implemented in [`infra/scripts/check_static_policies.py`](infra/scripts/check_static_policies.py), [`infra/scripts/check_region_policy.py`](infra/scripts/check_region_policy.py) & [`Makefile`](Makefile)*
  - **Synthetic Repository Mutation Testing (`create_synthetic_repo_copy`):** [`tests/compliance.py`](tests/compliance.py) and [`tests/scalability_elasticity.py`](tests/scalability_elasticity.py) clone `infra/` and `src/core/config.py` into temporary synthetic directories (`create_synthetic_repo_copy` in [`tests/helpers.py`](tests/helpers.py)) and inject deliberate policy violations (such as `"global"` regions, disabled `model_armor_fail_closed`, missing `deletion_protection`, or undersized subnets) to verify that the static policy gate reliably catches regressions.  
    — *Implemented in [`tests/helpers.py`](tests/helpers.py), [`tests/compliance.py`](tests/compliance.py) & [`tests/scalability_elasticity.py`](tests/scalability_elasticity.py)*

- **13.3 Pillar 3 — Test Coverage Across Architectural Layers**
  - **1-to-1 Architectural Layer & Pillar Verification:** Every backend package (`src/core/*`, `src/ingest/*`, `src/manage/*`, `src/query/*`, `src/agent/*`) and Terraform infrastructure module (`infra/terraform/*.tf`) is paired with dedicated positive and negative test cases covering authentication, cross-tenant isolation, AI security guardrails, DLP redaction, GDPR/EU AI Act compliance, observability telemetry, and scaling invariants.  
    — *Implemented in [`tests/auth.py`](tests/auth.py), [`tests/storage.py`](tests/storage.py), [`tests/multi_tenant_api.py`](tests/multi_tenant_api.py), [`tests/ai_security.py`](tests/ai_security.py), [`tests/compliance.py`](tests/compliance.py), [`tests/observability.py`](tests/observability.py) & [`tests/scalability_elasticity.py`](tests/scalability_elasticity.py)*

> **Note on Unimplemented Pillars:**
> - **Automated Load & Performance Benchmarking, Browser UI E2E Tests & Code-Coverage Threshold Gates (*Not Implemented — Relevant*):** While [`tests/scalability_elasticity.py`](tests/scalability_elasticity.py) verifies concurrency ceilings, parallel DLP worker pools, and `/tmp` memory eviction logic, a dedicated load and performance benchmarking suite (e.g., Locust or k6 stress-testing concurrent PDF ingestions and SSE streams against live Cloud Run instances), automated browser E2E tests (e.g., Playwright or Cypress for the React dashboard), and automated code-coverage percentage gates (`coverage.py` / `pytest-cov` in CI) are not yet implemented and remain relevant quality engineering enhancements.

---

## 📁 Repository Structure

```text
InteractiveRAG/
├── src/                                # Unified Python backend packages (4 domain packages + core)
│   ├── core/                           # Shared authentication, storage, AI security, compliance & observability
│   │   ├── ai_security/                # 5-pillar AI security package
│   │   │   ├── armor.py                # Model Armor & Cloud DLP screening/redaction client
│   │   │   ├── guardrails.py           # Model/query/upload guardrails, XML spotlighting & tool-name sanitization
│   │   │   └── safety.py               # Gemini safety settings & refusal detection
│   │   ├── compliance/                 # 4-pillar compliance & governance package
│   │   │   ├── audit.py                # Structured JSON audit logger & SHA-256 query hashing
│   │   │   ├── dsar.py                 # GDPR Art. 15 inventory, Art. 17 versioned erasure & memory thread wiping
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
│   │   │   ├── chroma.py               # Regional Vertex AI embeddings, ChromaDB client factory & hybrid RRF search
│   │   │   ├── gcs_sync.py             # Incremental MD5 delta GCS <-> /tmp sync & PDF archive
│   │   │   ├── locks.py                # Atomic GCS distributed write locks & bounded /tmp LRU eviction
│   │   │   └── paths.py                # Tenant-isolated path helpers & client cache release
│   │   ├── app_factory.py              # Shared FastAPI app factory, CORS, middleware & /health
│   │   ├── auth.py                     # Identity Platform / Firebase Bearer JWT verification
│   │   ├── config.py                   # Single source of truth for all env vars, model catalogs, /tmp caps & limits
│   │   ├── context.py                  # Shared RequestContext & RequestCorrelationMiddleware
│   │   └── logger.py                   # Application logging setup
│   ├── ingest/                         # Package 1: Dedicated Document Ingestion Service (Port 8000, `ingest-api`)
│   │   ├── app.py                      # FastAPI service mounting ONLY ingest_router & GET /health
│   │   ├── routes.py                   # ingest_router (`POST /ingest`)
│   │   ├── schemas.py                  # Validated IngestRequest model
│   │   ├── service.py                  # Batched ChromaDB indexing inside distributed GCS write lock
│   │   ├── splitter.py                 # PDF page loading & overlapping text chunking
│   │   └── upload_parser.py            # Multipart PDF upload staging, quota checks & guardrail delegation
│   ├── manage/                         # Package 2: Collection Management & GDPR Self-Service (`agent-api`)
│   │   ├── collections.py              # collections_router (`GET /collections` & `DELETE /collections/{collection_name}`)
│   │   ├── compliance.py               # compliance_router (`GET /compliance/my-data` & `DELETE /compliance/my-data`)
│   │   └── routes.py                   # manage_router combining collections_router & compliance_router
│   ├── query/                          # Package 3: Direct Vector Similarity Search (`agent-api`)
│   │   ├── routes.py                   # query_router (`POST /query`)
│   │   ├── schemas.py                  # Validated QueryRequest model
│   │   └── service.py                  # DLP-sanitized top-k Chroma hybrid search
│   ├── agent/                          # Package 4: Unified Interactive Agent, Query & Manage API (Port 8001, `agent-api`)
│   │   ├── app.py                      # FastAPI service mounting agent_router, query_router & manage_router
│   │   ├── display.py                  # Terminal message formatting helpers
│   │   ├── graph.py                    # Hardened StateGraph (input_guard -> model_call <-> tools -> output_guard)
│   │   ├── memory.py                   # Tenant-namespaced InMemorySaver checkpointer (`MEMORY_SAVER`)
│   │   ├── routes.py                   # agent_router (`POST /agent` & `POST /agent/stream`)
│   │   ├── schemas.py                  # Validated AgentRequest, AgentResponse & AgentState schemas
│   │   ├── service.py                  # Bounded recursion runner & live SSE event generator
│   │   └── tools.py                    # DLP-masked Chroma retriever tool delegating XML spotlighting to guardrails
│   └── README.md                       # Backend package reference
├── frontend/                           # React + Vite web dashboard (Port 3000)
│   ├── index.html                      # Loads runtime /env-config.js before the React bundle
│   └── src/                            # App.jsx, AuthGate.jsx (Sign-In UI), api.js, firebase.js
├── infra/                              # Consolidated Cloud Infrastructure, Containers, Pipelines & Policy Gates
│   ├── cloudbuild/                     # Google Cloud Build CI/CD pipelines
│   │   ├── build-images.yaml           # Parallel Cloud Build pipeline for backend & frontend Docker images
│   │   └── run-terraform.yaml          # Remote Cloud Build runner for Terraform lifecycle execution
│   ├── docker/                         # Production container definitions & Nginx startup scripts
│   │   ├── Dockerfile.backend          # Shared non-root Python 3.12 image for ingest-api & agent-api
│   │   ├── Dockerfile.frontend         # Multi-stage Node 22 + Nginx 1.27 SPA image
│   │   ├── entrypoint-frontend.sh      # Generates runtime /env-config.js on container startup
│   │   └── nginx.conf.template         # Nginx SPA routing, asset caching & /healthz probe config
│   ├── scripts/                        # Automated pre-deployment compliance & policy gate scripts
│   │   ├── check_region_policy.py      # Verifies target region against live GCP Org Policy
│   │   └── check_static_policies.py    # Offline verification of Terraform & runtime security/scaling policies
│   └── terraform/                      # Reproducible Terraform Infrastructure as Code (HCL)
│       ├── versions.tf                 # Terraform & Google provider versions + GCS state backend
│       ├── apis.tf                     # Required GCP service APIs & Artifact Registry repository
│       ├── iam.tf                      # Dedicated backend service account & least-privilege roles
│       ├── storage.tf                  # Private versioned user data bucket & lifecycle rules
│       ├── auth.tf                     # Identity Platform configuration & OAuth/Email sign-in
│       ├── network.tf                  # Zero-internet VPC, /24 subnets, Private Google Access & PSC
│       ├── load_balancer.tf            # Global HTTPS Load Balancer, NEGs & managed sslip.io cert
│       ├── ai_security.tf              # Cloud DLP inspect/de-identify templates & Model Armor
│       ├── cloud_run.tf                # Frontend, Ingestion API & Agent API Cloud Run services
│       ├── compliance.tf               # Cloud Audit Logs, 365-day regional audit bucket & log sink
│       ├── observability.tf            # Regional telemetry log bucket, sink, log metrics, dashboard & alerts
│       ├── variables.tf                # Parameterized infrastructure, scaling, security & compliance settings
│       ├── outputs.tf                  # Deployed URLs, IPs, bucket names & template identifiers
│       └── terraform.tfvars.example    # Template for environment-specific variable values
├── docs/                               # Architecture, security, observability & scaling blueprints
│   ├── GCP_DEPLOYMENT_PLAN.md          # Cloud Deployment, IaC, CI/CD, Config, Auth & Multi-Tenant Persistence
│   ├── NETWORK_SECURITY_PLAN.md        # Zero-Internet VPC & Network Security
│   ├── AI_SECURITY_PLAN.md             # AI-Specific Security & Cloud DLP (5 Pillars)
│   ├── COMPLIANCE_GOVERNANCE_PLAN.md   # Compliance, Data Privacy & Governance
│   ├── OBSERVABILITY_PLAN.md           # Observability & AI Monitoring (4 Pillars)
│   └── SCALABILITY_ELASTICITY_PLAN.md  # Scalability, Elasticity & Resource Efficiency (6 Pillars)
├── tests/                              # Automated unit suites & live smoke scripts
│   ├── helpers.py                      # Shared offline test doubles, fake GCS/LLM/embeddings & lifecycle utilities
│   ├── auth.py                         # Token verification, tenant UID validation & bypass tests
│   ├── storage.py                      # Per-user GCS sync, distributed lock, LRU cache & hybrid RRF search tests
│   ├── multi_tenant_api.py             # Cross-tenant isolation tests across REST & SSE APIs
│   ├── ai_security.py                  # Unit tests for all 5 AI security pillars
│   ├── compliance.py                   # Unit tests for residency, audit logs, DSAR, provenance & quotas
│   ├── observability.py                # Unit tests for structured logs, spans, tokens, TTFT & telemetry
│   ├── scalability_elasticity.py       # Unit tests for 4-package split, concurrency, /tmp caps, MD5 delta sync & alerts
│   ├── ingestion.py                    # Live local PDF ingestion & similarity search smoke test
│   └── agent.py                        # Live local JSON & SSE agent streaming smoke test
├── data/                               # Sample PDF documents for local testing
├── Makefile                            # Automated bootstrap, policy checks, builds & deployment
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
# GCP Project, Quota, Firebase & Single-Region Vertex AI Configuration
GOOGLE_CLOUD_PROJECT="your-gcp-project-id"
GOOGLE_CLOUD_QUOTA_PROJECT="your-gcp-project-id"
GOOGLE_CLOUD_LOCATION="us-central1"
FIREBASE_PROJECT_ID="your-gcp-project-id"

# Storage, Distributed Locks, /tmp Cache Quotas & CORS Configuration
# GCS_BUCKET_NAME=""
LOCAL_CHROMA_ROOT="/tmp/chroma_db"
UPLOAD_TMP_ROOT="/tmp/uploads"
ARCHIVE_UPLOADS_TO_GCS="true"
CHROMA_LOCK_TTL_SECONDS="300"
CHROMA_LOCK_TIMEOUT_SECONDS="60"
CHROMA_TMP_MAX_BYTES="536870912"
CHROMA_TMP_MIN_FREE_BYTES="268435456"
# ALLOWED_ORIGINS="http://localhost:3000,http://127.0.0.1:3000,http://localhost:5173"
ANONYMIZED_TELEMETRY="FALSE"

# AI Security: Model Armor, Cloud DLP & Gemini Safety Settings
# MODEL_ARMOR_TEMPLATE_ID=""
MODEL_ARMOR_LOCATION="us-central1"
MODEL_ARMOR_FAIL_CLOSED="false"
GEMINI_SAFETY_THRESHOLD="BLOCK_LOW_AND_ABOVE"

# Approved Model Catalogs & Guardrail Ceilings
ALLOWED_LLM_MODELS="gemini-2.5-flash,gemini-2.5-pro,gemini-2.5-flash-lite"
ALLOWED_EMBEDDING_MODELS="gemini-embedding-001,text-embedding-004,text-embedding-005"
MAX_UPLOAD_MB="20"
MAX_FILES_PER_REQUEST="10"
MAX_CHUNKS_PER_INGEST="1000"
MAX_QUERY_CHARS="4000"
AGENT_RECURSION_LIMIT="6"

# Compliance & Governance: Structured Audit Logging, Rate Limits & Quotas
AUDIT_LOG_NAME="interactiverag-audit"
# AUDIT_CLOUD_LOGGING_ENABLED="true"
RATE_LIMIT_INGEST_PER_MINUTE="10"
RATE_LIMIT_QUERY_PER_MINUTE="30"
MAX_COLLECTIONS_PER_USER="20"

# Observability & AI Monitoring: Telemetry, Structured Logging & Cloud Trace
TELEMETRY_LOG_NAME="interactiverag-telemetry"
# TELEMETRY_CLOUD_LOGGING_ENABLED="true"
# STRUCTURED_LOGGING="false"
# CLOUD_TRACE_ENABLED="false"

# Local Development, CLI & Auth Bypass Options (automatically ignored on Cloud Run when K_SERVICE is set):
AGENT_CONSOLE_TRACE="false"
UVICORN_RELOAD="false"
# PORT="8000"
AUTH_DISABLED="true"
DEV_USER_ID="local-dev"
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

The root [`Makefile`](Makefile) automates the complete deployment lifecycle. It automatically uses your local `terraform` binary if installed (`TF_MODE=local`) or falls back to running Terraform inside Google Cloud Build (`TF_MODE=cloudbuild`, via [`infra/cloudbuild/run-terraform.yaml`](infra/cloudbuild/run-terraform.yaml)) using a dedicated least-privilege deployer service account.

1. **Configure Terraform Variables**
   ```bash
   cp infra/terraform/terraform.tfvars.example infra/terraform/terraform.tfvars
   # Edit infra/terraform/terraform.tfvars to set your project_id, region, and optional settings
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
2. **Ingest PDFs**: Upload one or more PDF files into a named collection and choose an approved embedding model (`gemini-embedding-001`, `text-embedding-004`, or `text-embedding-005`) when creating a new collection. When adding PDFs to an existing collection, the dashboard automatically locks to that collection's bound embedding model.
3. **Manage Collections, Search Chunks & Privacy**: View your isolated collections, chunk counts, and bound embedding models, run direct top-$k$ chunk similarity searches (using the selected collection's bound embedding model), delete collections you no longer need, or open **"My Data & Privacy"** to inspect your full data footprint or erase all account data.
4. **Chat & Verify Provenance Badges**: Select an approved Gemini LLM model (`gemini-2.5-flash`, `gemini-2.5-pro`, or `gemini-2.5-flash-lite`) from the configured model catalog and ask questions in the chat panel (which automatically retrieves using the active collection's bound embedding model) to watch real-time retriever tool calls, grounded `[filename.pdf, p.X]` citations, DLP-filtered responses, and EU AI Act transparency badges.
