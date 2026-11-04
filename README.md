# InteractiveRAG

**InteractiveRAG** is a production-hardened, multi-tenant **Agentic Retrieval-Augmented Generation (RAG)** platform built with **LangGraph**, **Google Cloud Vertex AI**, **ChromaDB**, **FastAPI**, and **React**.

Instead of combining unrelated documents into a single shared database, InteractiveRAG gives every signed-in user an isolated workspace where they can organize PDFs into domain-specific vector collections (for example, separating annual financial reports from clinical research). Through the web dashboard or REST APIs, users can:

- **Ingest PDF Documents (`POST /ingest`)**: Upload PDF documents that are parsed page-by-page, split into overlapping text chunks, converted into semantic vector embeddings using Vertex AI, and indexed into a named vector collection bound to the chosen embedding model.
- **Run Direct Hybrid Search (`POST /query`)**: Perform hybrid searches combining dense semantic vector similarity with keyword matching via Reciprocal Rank Fusion (RRF), scoped strictly to a selected collection to inspect relevant document chunks, similarity scores, and page numbers directly.
- **Chat with a Grounded AI Agent (`POST /agent`, `POST /agent/stream`)**: Ask questions in a multi-turn conversation with a stateful LangGraph agent that dynamically searches the user's chosen collection, grounds every factual statement in retrieved evidence with inline `[filename.pdf, p.X]` citations, and streams each workflow step in real time over Server-Sent Events (SSE).
- **Manage Document Collections (`GET /collections`, `DELETE /collections/{collection_name}`)**: List all vector collections in the user's workspace along with their indexed chunk counts, bound embedding model, and server-approved model catalogs, or delete a collection when it is no longer needed.
- **Exercise Self-Service Privacy Controls (`GET /compliance/my-data`, `DELETE /compliance/my-data`)**: Export a complete inventory of stored collections, uploaded PDFs, storage bytes, and active conversation threads, or permanently erase all personal data—including live and archived Cloud Storage object versions, local vector caches, and conversation memory—in a single action.

---

## 📑 Outline

- [**Core Agentic RAG Architecture & Retrieval Pipeline**](#-core-agentic-rag-architecture--retrieval-pipeline)
  - [1. PDF Document Parsing, Chunking & Indexing Strategy](#1-pdf-document-parsing-chunking--indexing-strategy)
  - [2. Embedding Models, Hybrid Search & Domain-Scoped Vector Databases](#2-embedding-models-hybrid-search--domain-scoped-vector-databases)
  - [3. Agentic RAG Architecture Pattern (Stateful LangGraph Pipeline)](#3-agentic-rag-architecture-pattern-stateful-langgraph-pipeline)
  - [4. Document Grounding & Inline Page Citations](#4-document-grounding--inline-page-citations)
- [**Cloud Architecture & Production Engineering Layers**](#-cloud-architecture--production-engineering-layers)
  - [**I/ Cloud Architecture & Multi-Tenant Foundation**](#i-cloud-architecture--multi-tenant-foundation)
    - [1. Cloud Deployment (Cloud Architecture & Google Cloud Services Used)](#1-cloud-deployment-cloud-architecture--google-cloud-services-used)
    - [2. Multi-Tenant Persistence](#2-multi-tenant-persistence)
  - [**II/ Operational Excellence**](#ii-operational-excellence)
    - [1. Infrastructure as Code (IaC)](#1-infrastructure-as-code-iac)
    - [2. CI/CD & Deployment](#2-cicd--deployment)
    - [3. Testing & Quality Engineering](#3-testing--quality-engineering)
  - [**III/ Designing for Change**](#iii-designing-for-change)
    - [1. Configuration Management](#1-configuration-management)
    - [2. Modularity & Abstraction](#2-modularity--abstraction)
  - [**IV/ Performance & Cost Optimization**](#iv-performance--cost-optimization)
    - [1. Scalability & Elasticity](#1-scalability--elasticity)
    - [2. Resource Efficiency](#2-resource-efficiency)
  - [**V/ Security, Privacy, and Compliance**](#v-security-privacy-and-compliance)
    - [1. Authentication & Authorization](#1-authentication--authorization)
    - [2. Infrastructure & Network Security](#2-infrastructure--network-security)
    - [3. Data Protection & Privacy](#3-data-protection--privacy)
    - [4. AI-Specific Security](#4-ai-specific-security)
    - [5. Compliance & Governance](#5-compliance--governance)
  - [**VI/ Reliability & Resilience**](#vi-reliability--resilience)
    - [1. Observability](#1-observability)
    - [2. Graceful Degradation](#2-graceful-degradation)
- [**Repository Structure**](#-repository-structure)
- [**Local Setup & Prerequisites**](#-local-setup--prerequisites)
  - [1. Authenticate with Google Cloud & Configure `.env`](#1-authenticate-with-google-cloud--configure-env)
  - [2. Install Python Dependencies](#2-install-python-dependencies)
  - [3. Install Frontend Dependencies](#3-install-frontend-dependencies)
- [**Running Locally**](#-running-locally)
- [**Deploying to Google Cloud (Terraform + Cloud Build)**](#-deploying-to-google-cloud-terraform--cloud-build)
- [**Testing & Verification**](#-testing--verification)
  - [1. Offline Unit, Multi-Tenant, AI Security, Compliance, Observability & Scalability Test Suites](#1-offline-unit-multi-tenant-ai-security-compliance-observability--scalability-test-suites)
  - [2. Live Local Smoke Scripts](#2-live-local-smoke-scripts)
  - [3. Manual API Testing via `curl`](#3-manual-api-testing-via-curl)
  - [4. Interactive Web Dashboard Workflow](#4-interactive-web-dashboard-workflow)

---

## 🧠 Core Agentic RAG Architecture & Retrieval Pipeline

### 1. PDF Document Parsing, Chunking & Indexing Strategy
- **Page-Aware PDF Text Extraction:** Uploaded PDFs are read page-by-page, preserving the original document filename and page number on every extracted page so downstream answers can cite exact pages.  
  — *Implemented in [`src/ingest/splitter.py`](src/ingest/splitter.py)*  
  — *Called in [`src/ingest/service.py`](src/ingest/service.py)*
- **Overlapping Text Chunking:** Extracted pages are split into 1,000-character chunks with a 200-character sliding overlap along natural paragraph and sentence boundaries so key facts or tables near a split point are never cut in half.  
  — *Implemented in [`src/ingest/splitter.py`](src/ingest/splitter.py)*  
  — *Called in [`src/ingest/service.py`](src/ingest/service.py)*
- **Sequential Batched Vector Indexing:** Extracted text chunks are converted into embeddings and written into the target ChromaDB collection sequentially in batches of 100 chunks per operation so multi-PDF uploads are indexed reliably.  
  — *Implemented in [`src/ingest/service.py`](src/ingest/service.py)*  
  — *Called in [`src/ingest/routes.py`](src/ingest/routes.py)*

### 2. Embedding Models, Hybrid Search & Domain-Scoped Vector Databases
- **Vertex AI Dense Vector Embeddings:** Document chunks (during upload) and user questions (during search) are converted into numerical semantic vectors using Google Cloud Vertex AI embedding models (`gemini-embedding-001` by default, `text-embedding-004`, or `text-embedding-005`).  
  — *Implemented in [`src/core/storage/chroma.py`](src/core/storage/chroma.py)*  
  — *Called in [`src/ingest/service.py`](src/ingest/service.py), [`src/query/service.py`](src/query/service.py) & [`src/agent/tools.py`](src/agent/tools.py)*
- **Collection-Bound Embedding Consistency:** When a collection is first created, its chosen embedding model is saved in the collection's metadata. All future document uploads, direct similarity searches, and agent searches on that collection automatically look up and use that same embedding model so vectors in a collection never mix incompatible formats.  
  — *Implemented in [`src/core/storage/chroma.py`](src/core/storage/chroma.py)*  
  — *Called in [`src/ingest/service.py`](src/ingest/service.py), [`src/query/service.py`](src/query/service.py), [`src/agent/tools.py`](src/agent/tools.py) & [`src/manage/collections.py`](src/manage/collections.py)*
- **Domain-Scoped Named Collections:** Instead of pooling all uploaded files into a single flat index (which lowers accuracy when documents cover different topics), documents are organized into separate named ChromaDB collections that can be queried, listed, or deleted independently.  
  — *Implemented in [`src/core/storage/chroma.py`](src/core/storage/chroma.py)*  
  — *Called in [`src/ingest/service.py`](src/ingest/service.py), [`src/query/service.py`](src/query/service.py) & [`src/manage/collections.py`](src/manage/collections.py)*
- **SQLite Metadata & HNSW Vector Index Storage:** Each collection stores its chunk text and page metadata in a local SQLite catalog paired with a Hierarchical Navigable Small World (HNSW) vector index for fast nearest-neighbor similarity lookups.  
  — *Implemented in [`src/core/storage/chroma.py`](src/core/storage/chroma.py)*  
  — *Called in [`src/ingest/service.py`](src/ingest/service.py) & [`src/query/service.py`](src/query/service.py)*
- **Hybrid Search via Reciprocal Rank Fusion (Semantic + Keyword Matching):** Every search combines dense semantic vector similarity with BM25 keyword matching using Reciprocal Rank Fusion (RRF), filtering out zero-score keyword results and normalizing final scores between 0.0 and 1.0 so both conceptual meaning and exact terms (such as product codes or acronyms) are matched accurately.  
  — *Implemented in [`src/core/storage/chroma.py`](src/core/storage/chroma.py)*  
  — *Called in [`src/query/service.py`](src/query/service.py) & [`src/agent/tools.py`](src/agent/tools.py)*
- **Single-Collection Targeted Retrieval:** Every direct search and agent conversation targets one explicitly chosen collection at a time to keep retrieval focused and free of cross-topic noise.  
  — *Implemented in [`src/query/service.py`](src/query/service.py) & [`src/agent/tools.py`](src/agent/tools.py)*  
  — *Called in [`src/query/routes.py`](src/query/routes.py), [`src/agent/graph.py`](src/agent/graph.py) & [`src/agent/routes.py`](src/agent/routes.py)*

### 3. Agentic RAG Architecture Pattern (Stateful LangGraph Pipeline)
- **Stateful Agentic Workflow vs. Fixed Single-Shot Retrieval:** Traditional RAG pipelines run a single search using the user's raw prompt, which often fails on conversational follow-ups or multi-part questions. InteractiveRAG uses **LangGraph** and **Vertex AI Gemini** (configured with deterministic temperature zero) to orchestrate a multi-step reasoning and tool-calling workflow.  
  — *Implemented in [`src/agent/graph.py`](src/agent/graph.py)*  
  — *Called in [`src/agent/routes.py`](src/agent/routes.py) & [`src/agent/service.py`](src/agent/service.py)*
- **Dynamic Collection-Scoped Search Tool:** On each request, the backend constructs a dedicated search tool bound strictly to the user's selected collection that fetches the top 5 most relevant document chunks per call.  
  — *Implemented in [`src/agent/tools.py`](src/agent/tools.py)*  
  — *Called in [`src/agent/graph.py`](src/agent/graph.py)*
- **Iterative Query Formulation & Multi-Step Search:** The Gemini agent autonomously decides when to search, writes targeted search queries based on the conversation history, evaluates the returned chunks, and can run additional searches with refined terms if the initial results are incomplete before writing its final answer.  
  — *Implemented in [`src/agent/graph.py`](src/agent/graph.py)*  
  — *Called in [`src/agent/routes.py`](src/agent/routes.py) & [`src/agent/service.py`](src/agent/service.py)*
- **Multi-Turn Conversation Memory:** Conversation history is preserved across turns in an in-memory checkpointer keyed by conversation thread so users can ask natural follow-up questions without repeating earlier context.  
  — *Implemented in [`src/agent/memory.py`](src/agent/memory.py)*  
  — *Called in [`src/agent/graph.py`](src/agent/graph.py), [`src/agent/routes.py`](src/agent/routes.py) & [`src/agent/service.py`](src/agent/service.py)*
- **Real-Time Step-by-Step Event Streaming:** During execution, the streaming engine streams each completed workflow step—search tool calls, retrieved evidence chunks, and the final AI answer—to the browser in real time over Server-Sent Events (SSE).  
  — *Implemented in [`src/agent/service.py`](src/agent/service.py)*  
  — *Called in [`src/agent/routes.py`](src/agent/routes.py)*

### 4. Document Grounding & Inline Page Citations
- **Human-Readable Page Number Normalization:** PDF readers internally number pages starting at 0, which causes off-by-one confusion when cited directly. Before chunks are shown to the AI model, page numbers are converted into human-readable 1-based page numbers (`p.1`, `p.2`, ...) paired with the source filename.  
  — *Implemented in [`src/core/ai_security/guardrails.py`](src/core/ai_security/guardrails.py)*  
  — *Called in [`src/agent/tools.py`](src/agent/tools.py)*
- **Mandatory Document Grounding:** The agent's system instructions require it to search the user's collection before answering factual questions and base every factual claim exclusively on the retrieved document chunks rather than outside knowledge or assumptions.  
  — *Implemented in [`src/agent/graph.py`](src/agent/graph.py)*  
  — *Called in [`src/agent/routes.py`](src/agent/routes.py) & [`src/agent/service.py`](src/agent/service.py)*
- **Explicit Abstention When Evidence Is Missing:** Whenever a search returns no matching chunks or the retrieved text lacks enough information to answer the question, the agent clearly states that the answer cannot be found in the uploaded documents rather than guessing.  
  — *Implemented in [`src/core/ai_security/guardrails.py`](src/core/ai_security/guardrails.py) & [`src/agent/graph.py`](src/agent/graph.py)*  
  — *Called in [`src/agent/tools.py`](src/agent/tools.py), [`src/agent/routes.py`](src/agent/routes.py) & [`src/agent/service.py`](src/agent/service.py)*
- **Inline Source and Page Citations:** Every factual statement in the agent's response includes an inline citation showing the exact PDF filename and page number of the supporting chunk (for example, `[annual_report.pdf, p.4]`) so users can verify every claim against the original document.  
  — *Implemented in [`src/core/ai_security/guardrails.py`](src/core/ai_security/guardrails.py) & [`src/agent/graph.py`](src/agent/graph.py)*  
  — *Called in [`src/agent/tools.py`](src/agent/tools.py), [`src/core/compliance/provenance.py`](src/core/compliance/provenance.py) & [`src/core/observability/telemetry.py`](src/core/observability/telemetry.py)*

---

## 🏗 Cloud Architecture & Production Engineering Layers

InteractiveRAG is engineered for reproducible deployment on **Google Cloud Platform (GCP)** across **6 architectural domains (`I`–`VI`) and 16 defense-in-depth engineering layers**, verified by the automated test and policy suites in [`tests/`](tests/) and [`infra/scripts/`](infra/scripts/):

| Domain | Layer | Pillars & Focus Areas |
| :--- | :--- | :--- |
| [**I. Cloud Architecture & Multi-Tenant Foundation**](#i-cloud-architecture--multi-tenant-foundation) | [**1. Cloud Deployment**](#1-cloud-deployment-cloud-architecture--google-cloud-services-used) | End-to-end Google Cloud request routing topology and inventory of managed Google Cloud compute, AI, storage, identity, security, networking, CI/CD & observability services |
| [**I. Cloud Architecture & Multi-Tenant Foundation**](#i-cloud-architecture--multi-tenant-foundation) | [**2. Multi-Tenant Persistence**](#2-multi-tenant-persistence) | Per-user vector database, PDF archive & conversation memory isolation, authoritative Cloud Storage snapshot synchronization, and distributed per-user Cloud Storage write locks |
| [**II. Operational Excellence**](#ii-operational-excellence) | [**1. Infrastructure as Code (IaC)**](#1-infrastructure-as-code-iac) | Declarative Terraform stack, remote versioned Cloud Storage state, remote Cloud Build Terraform runner, reproducible single-pass provisioning, and cross-environment parity |
| [**II. Operational Excellence**](#ii-operational-excellence) | [**2. CI/CD & Deployment**](#2-cicd--deployment) | Four-stage automated bootstrap/build/policy-gate/deploy pipeline, health-gated zero-downtime Cloud Run revision rollouts, and Git-commit + versioned storage rollback support |
| [**II. Operational Excellence**](#ii-operational-excellence) | [**3. Testing & Quality Engineering**](#3-testing--quality-engineering) | Offline unit test suites, multi-tenant API integration tests, live end-to-end smoke scripts, automated static & Terraform schema gates, mutation testing, and cross-layer coverage |
| [**III. Designing for Change**](#iii-designing-for-change) | [**1. Configuration Management**](#1-configuration-management) | Externalized backend & Terraform configuration, startup frontend runtime config generation, automatic cloud vs. local environment separation, and runtime/infrastructure feature flags |
| [**III. Designing for Change**](#iii-designing-for-change) | [**2. Modularity & Abstraction**](#2-modularity--abstraction) | Loose coupling across 4 domain packages + shared core foundation, strict request/response interface schemas, dynamic chat & per-collection embedding model swappability, and dependency injection |
| [**IV. Performance & Cost Optimization**](#iv-performance--cost-optimization) | [**1. Scalability & Elasticity**](#1-scalability--elasticity) | Workload-isolated microservice packaging (`ingest-api` vs. `agent-api`), horizontal autoscaling & per-container concurrency ceilings, `/24` private VPC subnet IP headroom, and parallel retrieval throughput |
| [**IV. Performance & Cost Optimization**](#iv-performance--cost-optimization) | [**2. Resource Efficiency**](#2-resource-efficiency) | Per-service CPU & memory right-sizing, scale-to-zero & request-scoped CPU billing, session affinity & version-checked warm cache reuse, bounded `/tmp` cache eviction, and MD5 delta & lifecycle storage efficiency |
| [**V. Security, Privacy, and Compliance**](#v-security-privacy-and-compliance) | [**1. Authentication & Authorization**](#1-authentication--authorization) | Dedicated runtime & deployer service accounts, declarative IAM, Principle of Least Privilege, Google Sign-In & Email/Password authentication with token-derived tenant identity, and keyless service credentials |
| [**V. Security, Privacy, and Compliance**](#v-security-privacy-and-compliance) | [**2. Infrastructure & Network Security**](#2-infrastructure--network-security) | Disabled CDN edge caching for private traffic, locked-down Cloud Run ingress, zero-internet private VPC with segmented subnets, Private Google Access, and Regional Private Service Connect |
| [**V. Security, Privacy, and Compliance**](#v-security-privacy-and-compliance) | [**3. Data Protection & Privacy**](#3-data-protection--privacy) | Encryption in transit (managed TLS & HTTP-to-HTTPS redirect) and at rest (AES-256 & ephemeral RAM-backed working directories), 4-tier data classification & storage segregation, and Secret Manager integration |
| [**V. Security, Privacy, and Compliance**](#v-security-privacy-and-compliance) | [**4. AI-Specific Security**](#4-ai-specific-security) | Prompt injection mitigation (Model Armor & XML spotlighting), approved model allowlists, query & PDF upload guardrails, Cloud Sensitive Data Protection (Cloud DLP) two-stage redaction, and dual-layer content safety |
| [**V. Security, Privacy, and Compliance**](#v-security-privacy-and-compliance) | [**5. Compliance & Governance**](#5-compliance--governance) | Single-region data residency & live Org Policy verification, privacy-safe SHA-256 hashed audit & Data Access logging, GDPR self-service access & erasure, EU AI Act provenance badges, fail-closed screening, and rate/quota enforcement |
| [**VI. Reliability & Resilience**](#vi-reliability--resilience) | [**1. Observability**](#1-observability) | Structured JSON operational logging & request correlation, Cloud Monitoring metrics, dashboards & automated alerts, distributed per-stage Cloud Trace waterfall spans, and AI latency, token & retrieval quality telemetry |
| [**VI. Reliability & Resilience**](#vi-reliability--resilience) | [**2. Graceful Degradation**](#2-graceful-degradation) | Hybrid-to-dense search & bounded-agent step-limit fallbacks, mid-stream SSE error recovery, exponential backoff with random jitter on storage locks, and bounded timeouts with stale-lock crash recovery |

---

## I/ Cloud Architecture & Multi-Tenant Foundation

### 1. Cloud Deployment (Cloud Architecture & Google Cloud Services Used)

InteractiveRAG is deployed as a serverless, single-region, private-networked architecture on **Google Cloud Platform (GCP)**. External browser and API requests enter through a single global front door and are routed by URL path to serverless Cloud Run microservices that communicate with managed Google Cloud AI, storage, identity, security, and observability services over Google's private internal network.

- **1.1 End-to-End Cloud Architecture & Path-Based Request Routing**
  - **Path-Based Routing to Serverless Microservices:** A single Global External Application Load Balancer routes incoming requests by URL path across three serverless backend groups without exposing individual container URLs:
    - `/*` routes to the **Web Frontend Service (`frontend`)** serving the React web application.
    - `/api/ingest/*` routes to the **Document Ingestion Service (`ingest-api`)** for PDF uploads and vector indexing.
    - `/api/agent/*` routes to the **Interactive Agent, Search & Management Service (`agent-api`)** for collection management, direct search, and live AI chat.  
    — *Provisioned in [`infra/terraform/load_balancer.tf`](infra/terraform/load_balancer.tf) & [`infra/terraform/cloud_run.tf`](infra/terraform/cloud_run.tf)*

- **1.2 Managed Google Cloud Services Used**
  - **Google Cloud Run (Gen2 Serverless Compute):** Hosts the three containerized microservices (`frontend`, `ingest-api`, and `agent-api`).  
    — *Provisioned in [`infra/terraform/cloud_run.tf`](infra/terraform/cloud_run.tf)*
  - **Global External Application Load Balancer:** Provides the static public IPv4 entry point and Serverless Network Endpoint Group (NEG) routing to Cloud Run.  
    — *Provisioned in [`infra/terraform/load_balancer.tf`](infra/terraform/load_balancer.tf)*
  - **Google-Managed SSL Certificates:** Automatically provisions and renews the public TLS certificate for the Load Balancer's `<IP-with-dashes>.sslip.io` domain.  
    — *Provisioned in [`infra/terraform/load_balancer.tf`](infra/terraform/load_balancer.tf)*
  - **Google Cloud Vertex AI (Gemini Generative Models):** Provides regionally bound `gemini-2.5-flash`, `gemini-2.5-pro`, and `gemini-2.5-flash-lite` language models for agent reasoning, tool calling, and grounded answer generation.  
    — *Provisioned in [`infra/terraform/apis.tf`](infra/terraform/apis.tf), [`infra/terraform/iam.tf`](infra/terraform/iam.tf) & [`infra/terraform/cloud_run.tf`](infra/terraform/cloud_run.tf)*  
    — *Implemented in [`src/agent/graph.py`](src/agent/graph.py)*
  - **Google Cloud Vertex AI (Text Embedding Models):** Provides regionally bound `gemini-embedding-001`, `text-embedding-004`, and `text-embedding-005` models for dense semantic vector generation.  
    — *Provisioned in [`infra/terraform/apis.tf`](infra/terraform/apis.tf), [`infra/terraform/iam.tf`](infra/terraform/iam.tf) & [`infra/terraform/cloud_run.tf`](infra/terraform/cloud_run.tf)*  
    — *Implemented in [`src/core/storage/chroma.py`](src/core/storage/chroma.py)*
  - **Google Cloud Model Armor:** Screens incoming user prompts and outgoing AI responses against a regional security policy template.  
    — *Provisioned in [`infra/terraform/ai_security.tf`](infra/terraform/ai_security.tf)*  
    — *Implemented in [`src/core/ai_security/armor.py`](src/core/ai_security/armor.py)*
  - **Google Cloud Sensitive Data Protection (Cloud DLP):** Provides regional inspection and de-identification templates invoked during retrieval and response generation.  
    — *Provisioned in [`infra/terraform/ai_security.tf`](infra/terraform/ai_security.tf)*  
    — *Implemented in [`src/core/ai_security/armor.py`](src/core/ai_security/armor.py)*
  - **Google Cloud Storage (Tenant Persistence Bucket):** Stores each user's authoritative ChromaDB vector database snapshots, archived original PDF uploads, and distributed per-user write-lock files.  
    — *Provisioned in [`infra/terraform/storage.tf`](infra/terraform/storage.tf)*  
    — *Implemented in [`src/core/storage/gcs_sync.py`](src/core/storage/gcs_sync.py) & [`src/core/storage/locks.py`](src/core/storage/locks.py)*
  - **Google Cloud Storage (Terraform State & Build Logs Bucket):** Stores remote versioned Terraform infrastructure state files and archived Cloud Build container build logs.  
    — *Provisioned in [`infra/terraform/versions.tf`](infra/terraform/versions.tf)*  
    — *Called in [`Makefile`](Makefile) & [`infra/cloudbuild/build-images.yaml`](infra/cloudbuild/build-images.yaml)*
  - **Google Cloud Identity Platform (Firebase Authentication):** Provides managed user authentication via Google Sign-In (OAuth 2.0 / OIDC) and Email/Password sign-in.  
    — *Provisioned in [`infra/terraform/auth.tf`](infra/terraform/auth.tf)*  
    — *Implemented in [`src/core/auth.py`](src/core/auth.py) & [`frontend/src/firebase.js`](frontend/src/firebase.js)*
  - **Google Cloud IAM:** Governs machine-to-machine permissions across dedicated service accounts for the backend, frontend, deployment pipeline, and Model Armor service agent.  
    — *Provisioned in [`infra/terraform/iam.tf`](infra/terraform/iam.tf) & [`infra/terraform/ai_security.tf`](infra/terraform/ai_security.tf)*
  - **Google Cloud Secret Manager:** Stores sensitive OAuth 2.0 client credentials outside of source code and Terraform variable files.  
    — *Provisioned in [`infra/terraform/auth.tf`](infra/terraform/auth.tf) & [`infra/terraform/variables.tf`](infra/terraform/variables.tf)*
  - **Google Cloud Virtual Private Cloud (VPC):** Provides the isolated private network and dedicated `/24` subnets for backend services.  
    — *Provisioned in [`infra/terraform/network.tf`](infra/terraform/network.tf)*
  - **Cloud Run Direct VPC Egress:** Routes outbound traffic from backend Cloud Run containers directly into the private VPC subnets.  
    — *Provisioned in [`infra/terraform/cloud_run.tf`](infra/terraform/cloud_run.tf)*
  - **Private Google Access & Cloud DNS:** Routes backend requests to standard Google Cloud APIs (`googleapis.com`) over Google's private internal network (`199.36.153.8/30`).  
    — *Provisioned in [`infra/terraform/network.tf`](infra/terraform/network.tf)*
  - **Regional Private Service Connect (PSC):** Provisions a private internal IP endpoint and private DNS zone inside the VPC for regional Model Armor calls (`modelarmor.<region>.rep.googleapis.com`).  
    — *Provisioned in [`infra/terraform/network.tf`](infra/terraform/network.tf)*
  - **Google Artifact Registry:** Hosts the private regional Docker container repository for backend and frontend images.  
    — *Provisioned in [`infra/terraform/apis.tf`](infra/terraform/apis.tf)*
  - **Google Cloud Build:** Executes parallel container image builds and remote Terraform infrastructure deployments inside a cloud runner.  
    — *Provisioned in [`infra/terraform/apis.tf`](infra/terraform/apis.tf)*  
    — *Implemented in [`infra/cloudbuild/build-images.yaml`](infra/cloudbuild/build-images.yaml) & [`infra/cloudbuild/run-terraform.yaml`](infra/cloudbuild/run-terraform.yaml)*
  - **Google Cloud Logging:** Collects structured JSON logs and routes them through dedicated sinks into separate regional compliance and operational log buckets.  
    — *Provisioned in [`infra/terraform/compliance.tf`](infra/terraform/compliance.tf) & [`infra/terraform/observability.tf`](infra/terraform/observability.tf)*  
    — *Implemented in [`src/core/compliance/audit.py`](src/core/compliance/audit.py) & [`src/core/observability/telemetry.py`](src/core/observability/telemetry.py)*
  - **Google Cloud Audit Logs:** Records administrative actions, data reads, and data writes across Cloud Storage, Vertex AI, Cloud DLP, and Model Armor.  
    — *Provisioned in [`infra/terraform/compliance.tf`](infra/terraform/compliance.tf)*
  - **Google Cloud Monitoring:** Extracts custom log-based metrics, powers the operations dashboard, and triggers automated health alerts.  
    — *Provisioned in [`infra/terraform/observability.tf`](infra/terraform/observability.tf)*
  - **Google Cloud Trace:** Captures distributed per-stage execution timelines across backend requests.  
    — *Provisioned in [`infra/terraform/iam.tf`](infra/terraform/iam.tf) & [`infra/terraform/cloud_run.tf`](infra/terraform/cloud_run.tf)*  
    — *Implemented in [`src/core/observability/tracing.py`](src/core/observability/tracing.py)*
  - **Google Cloud Organization Policy Service:** Enforces project-wide geographic resource location restrictions (`constraints/gcp.resourceLocations`).  
    — *Implemented in [`infra/scripts/check_region_policy.py`](infra/scripts/check_region_policy.py)*  
    — *Called in [`Makefile`](Makefile)*

---

### 2. Multi-Tenant Persistence

Multiple users can sign in and use the cloud platform simultaneously—each person gets their own strictly isolated document collections, vector database snapshots, and chat history, backed by durable Cloud Storage synchronization.

- **2.1 Multi-Tenant Data & Conversation Isolation**
  - **Per-User Vector Database Isolation:** Each user's ChromaDB files are stored under a dedicated Cloud Storage prefix (`users/{uid}/chroma_db/`) and an isolated local container folder (`/tmp/chroma_db/{uid}`). Two users can create, query, or delete collections with the exact same name without ever seeing or overwriting each other's documents.  
    — *Implemented in [`src/core/storage/paths.py`](src/core/storage/paths.py) & [`src/core/storage/chroma.py`](src/core/storage/chroma.py)*  
    — *Called in [`src/ingest/service.py`](src/ingest/service.py), [`src/query/service.py`](src/query/service.py) & [`src/manage/collections.py`](src/manage/collections.py)*
  - **Per-Request PDF Upload Staging & Private Archiving:** Uploaded PDFs are staged in an isolated temporary directory unique to that user and request (`/tmp/uploads/{uid}/{request_id}`), archived to the user's private Cloud Storage uploads folder (`users/{uid}/uploads/`), and wiped from local container memory as soon as indexing finishes.  
    — *Implemented in [`src/ingest/upload_parser.py`](src/ingest/upload_parser.py) & [`src/core/storage/gcs_sync.py`](src/core/storage/gcs_sync.py)*  
    — *Called in [`src/ingest/routes.py`](src/ingest/routes.py)*
  - **Tenant-Namespaced Conversation Memory:** Every multi-turn chat thread is prefixed with the user's unique ID (`{uid}:{thread_id}`) inside the conversation memory store so one user can never read, resume, or erase another user's conversation history.  
    — *Implemented in [`src/agent/memory.py`](src/agent/memory.py) & [`src/core/compliance/dsar.py`](src/core/compliance/dsar.py)*  
    — *Called in [`src/agent/routes.py`](src/agent/routes.py), [`src/agent/service.py`](src/agent/service.py) & [`src/manage/compliance.py`](src/manage/compliance.py)*

- **2.2 Durable Cloud Storage & Container Memory Synchronization**
  - **Why Snapshots Are Synchronized Instead of Directly Mounted:** ChromaDB relies on SQLite and memory-mapped vector index files, whereas Cloud Storage object buckets do not support the byte-range file locking that SQLite requires. Synchronizing snapshots between Cloud Storage and fast local container memory avoids database corruption across concurrent containers while ensuring data survives when serverless instances scale down to zero.
  - **Authoritative Cloud Storage Snapshot Persistence:** Each user's vector database snapshots and uploaded PDF archives are persisted in a dedicated regional Google Cloud Storage bucket (`users/{uid}/`) so state is durable across ephemeral container lifecycles.  
    — *Provisioned in [`infra/terraform/storage.tf`](infra/terraform/storage.tf)*  
    — *Called in [`infra/terraform/cloud_run.tf`](infra/terraform/cloud_run.tf)*
  - **Atomic Local Staging & Snapshot Swap on Read:** When synchronizing a user's vector database from Cloud Storage into a container, files are downloaded into a temporary local staging directory and atomically swapped into `/tmp/chroma_db/{uid}` so readers never observe a partially downloaded SQLite index.  
    — *Implemented in [`src/core/storage/gcs_sync.py`](src/core/storage/gcs_sync.py)*  
    — *Called in [`src/core/storage/chroma.py`](src/core/storage/chroma.py)*
  - **Distributed Per-User Write Locks:** Before modifying a user's vector database, the backend acquires an exclusive per-user lock file in Cloud Storage (`users/{uid}/locks/chroma.lock`) using atomic generation preconditions (`if_generation_match=0`) paired with an in-process thread lock and active-user reference pinning so two simultaneous uploads or deletions for the same user cannot overwrite each other.  
    — *Implemented in [`src/core/storage/locks.py`](src/core/storage/locks.py)*  
    — *Called in [`src/core/storage/gcs_sync.py`](src/core/storage/gcs_sync.py), [`src/ingest/service.py`](src/ingest/service.py) & [`src/manage/collections.py`](src/manage/collections.py)*

---

## II/ [OPERATIONAL EXCELLENCE]

### 1. Infrastructure as Code (IaC)

All cloud infrastructure, security policies, networking, and monitoring resources are managed declaratively as version-controlled code rather than through manual Google Cloud Console changes:

- **1.1 Pillar 1 — Declarative Terraform Stack & Remote State**
  - **Full-Stack Declarative Terraform Modules:** Modular Terraform configuration files provision all 19 required Google Cloud APIs, Artifact Registry, dedicated IAM service accounts, the versioned Cloud Storage bucket, Identity Platform authentication, the zero-internet private VPC and subnets, Private Google Access DNS, Regional Private Service Connect for Model Armor, Cloud DLP templates, Model Armor templates, all three Cloud Run microservices, the Global External HTTPS Load Balancer, Cloud Audit Logs, regional Cloud Logging buckets and sinks, log-based metrics, and Cloud Monitoring dashboards and alerts.  
    — *Provisioned in [`infra/terraform/versions.tf`](infra/terraform/versions.tf), [`infra/terraform/apis.tf`](infra/terraform/apis.tf), [`infra/terraform/iam.tf`](infra/terraform/iam.tf), [`infra/terraform/storage.tf`](infra/terraform/storage.tf), [`infra/terraform/auth.tf`](infra/terraform/auth.tf), [`infra/terraform/network.tf`](infra/terraform/network.tf), [`infra/terraform/load_balancer.tf`](infra/terraform/load_balancer.tf), [`infra/terraform/ai_security.tf`](infra/terraform/ai_security.tf), [`infra/terraform/cloud_run.tf`](infra/terraform/cloud_run.tf), [`infra/terraform/compliance.tf`](infra/terraform/compliance.tf), [`infra/terraform/observability.tf`](infra/terraform/observability.tf) & [`infra/terraform/outputs.tf`](infra/terraform/outputs.tf)*
  - **Remote Versioned Cloud Storage State Backend:** Terraform stores its infrastructure state in a dedicated, versioned Google Cloud Storage bucket. Bucket and prefix parameters are passed dynamically at initialization time so the Terraform files remain portable across projects and environments without hardcoded bucket names.  
    — *Provisioned in [`infra/terraform/versions.tf`](infra/terraform/versions.tf)*  
    — *Called in [`Makefile`](Makefile) & [`infra/cloudbuild/run-terraform.yaml`](infra/cloudbuild/run-terraform.yaml)*

- **1.2 Pillar 2 — Cloud-Native Terraform Execution (`Cloud Build`)**
  - **Hybrid Local and Cloud Build Terraform Runner:** The project automation automatically detects whether Terraform is installed locally and, if not, executes Terraform initialization, validation, planning, deployment, output inspection, and teardown remotely inside Google Cloud Build using a pinned Terraform runner image.  
    — *Implemented in [`infra/cloudbuild/run-terraform.yaml`](infra/cloudbuild/run-terraform.yaml)*  
    — *Called in [`Makefile`](Makefile)*

- **1.3 Pillar 3 — Reproducible Environments**
  - **Upfront Hostname Calculation for Single-Pass Deployment:** Terraform calculates the Cloud Run service URLs and the Load Balancer's `<IP-with-dashes>.sslip.io` domain upfront, avoiding circular dependencies between Cloud Run, Identity Platform authorized domains, and backend CORS origin lists so the entire stack deploys cleanly in a single pass.  
    — *Provisioned in [`infra/terraform/cloud_run.tf`](infra/terraform/cloud_run.tf), [`infra/terraform/load_balancer.tf`](infra/terraform/load_balancer.tf) & [`infra/terraform/auth.tf`](infra/terraform/auth.tf)*
  - **Pinned Container & Terraform Versions:** All Dockerfiles pin exact base operating system and runtime versions (`python:3.12-slim`, `node:22-slim`, `nginx:1.27-alpine`), and the remote runner pins an exact Terraform version (`1.9.8`) so builds behave identically over time.  
    — *Implemented in [`infra/docker/Dockerfile.backend`](infra/docker/Dockerfile.backend), [`infra/docker/Dockerfile.frontend`](infra/docker/Dockerfile.frontend) & [`infra/cloudbuild/run-terraform.yaml`](infra/cloudbuild/run-terraform.yaml)*
  - **Pre-Plan Terraform Input Validation Rules:** Built-in Terraform variable validation blocks reject out-of-range scaling, concurrency, subnet CIDR, or retention settings during the planning phase before any cloud resources are modified.  
    — *Provisioned in [`infra/terraform/variables.tf`](infra/terraform/variables.tf), [`infra/terraform/compliance.tf`](infra/terraform/compliance.tf) & [`infra/terraform/observability.tf`](infra/terraform/observability.tf)*

- **1.4 Pillar 4 — Environment Parity**
  - **Single Parameterized Terraform Codebase Across Environments:** Development, staging, and production environments share the exact same Terraform files, using the `environment` parameter (`dev`, `staging`, `prod`) to namespace resource names, isolate remote state folders (`terraform/state/<environment>`), and automatically enable production-only safeguards such as Cloud Run accidental deletion protection (`deletion_protection = true` when `environment == "prod"`).  
    — *Provisioned in [`infra/terraform/variables.tf`](infra/terraform/variables.tf) & [`infra/terraform/cloud_run.tf`](infra/terraform/cloud_run.tf)*  
    — *Called in [`Makefile`](Makefile)*
  - **Shared Backend Container Image:** A single compiled backend container image runs both `ingest-api` and `agent-api` by selecting which service entrypoint (`APP_MODULE`) to launch via an environment variable, guaranteeing identical Python libraries and security patches across both services.  
    — *Provisioned in [`infra/terraform/cloud_run.tf`](infra/terraform/cloud_run.tf)*  
    — *Implemented in [`infra/docker/Dockerfile.backend`](infra/docker/Dockerfile.backend)*
  - **Build-Once, Run-Anywhere Frontend Image:** The compiled frontend image contains no hardcoded environment URLs and can be promoted unchanged across development, staging, and production environments.  
    — *Provisioned in [`infra/terraform/cloud_run.tf`](infra/terraform/cloud_run.tf)*  
    — *Implemented in [`infra/docker/Dockerfile.frontend`](infra/docker/Dockerfile.frontend)*

> **Note on Unimplemented / Non-Relevant Pillars:**
> - **Pulumi (*Not Implemented — Not Relevant*):** Pulumi is not used because 100% of our Google Cloud infrastructure is already declaratively defined and managed with **Terraform** ([`infra/terraform/*.tf`](infra/terraform/)), which provides first-party Google Cloud support and integrates directly with our remote Cloud Storage state backend and Cloud Build runner.

---

### 2. CI/CD & Deployment

Container builds, compliance verification, and infrastructure rollouts are orchestrated through an automated, policy-gated deployment pipeline:

- **2.1 Pillar 1 — Pipeline Design**
  - **Stage 1 — Project & State Bootstrap (`make bootstrap`):** Enables initial Google Cloud APIs, creates the versioned Terraform state bucket, provisions the least-privilege deployer service account, and initializes the Artifact Registry container repository so a brand-new Google Cloud project can be bootstrapped in one command.  
    — *Implemented in [`Makefile`](Makefile) & [`infra/terraform/apis.tf`](infra/terraform/apis.tf)*
  - **Stage 2 — Parallel Container Build & Push (`make build-push`):** Compiles the backend and frontend container images in parallel on Google Cloud Build (or locally via Docker Buildx), tags each image with both the current Git commit hash and `latest`, pushes them to Artifact Registry, and archives build logs in the state bucket.  
    — *Implemented in [`Makefile`](Makefile), [`infra/cloudbuild/build-images.yaml`](infra/cloudbuild/build-images.yaml), [`infra/docker/Dockerfile.backend`](infra/docker/Dockerfile.backend) & [`infra/docker/Dockerfile.frontend`](infra/docker/Dockerfile.frontend)*
  - **Stage 3 — Mandatory Pre-Deployment Policy Gate (`make policy-check`):** Automatically chains active Google Cloud credential verification, live Organization Policy residency verification, offline static policy verification, and Terraform schema validation as a required prerequisite before `make plan` or `make deploy` can execute.  
    — *Implemented in [`Makefile`](Makefile), [`infra/scripts/check_region_policy.py`](infra/scripts/check_region_policy.py) & [`infra/scripts/check_static_policies.py`](infra/scripts/check_static_policies.py)*
  - **Stage 4 — Infrastructure Plan & Rollout (`make plan` & `make deploy`):** Previews and applies the Terraform infrastructure stack locally or inside Google Cloud Build, automatically importing existing project-singleton authentication resources so repeated deployments never fail on state conflicts.  
    — *Implemented in [`Makefile`](Makefile) & [`infra/cloudbuild/run-terraform.yaml`](infra/cloudbuild/run-terraform.yaml)*

- **2.2 Pillar 2 — Deployment Strategies**
  - **Immutable Cloud Run Revision Rollouts:** Deploying a new container image tag creates a brand-new, immutable Cloud Run revision for `ingest-api`, `agent-api`, and `frontend` without modifying running containers in place.  
    — *Provisioned in [`infra/terraform/cloud_run.tf`](infra/terraform/cloud_run.tf)*
  - **Health-Gated Zero-Downtime Traffic Shifting:** Cloud Run waits for the new container revision to pass its HTTP startup and liveness health checks (`/health` on the backend APIs and `/healthz` on the Nginx frontend) before atomically shifting live user traffic over from the previous revision.  
    — *Provisioned in [`infra/terraform/cloud_run.tf`](infra/terraform/cloud_run.tf)*  
    — *Implemented in [`src/core/app_factory.py`](src/core/app_factory.py) & [`infra/docker/nginx.conf.template`](infra/docker/nginx.conf.template)*
  - **Zero-Downtime SSL Certificate Replacement:** The Load Balancer's Google-managed SSL certificate is configured with `create_before_destroy = true` to provision and attach any replacement certificate before removing the old one so HTTPS traffic is never interrupted during domain or certificate updates.  
    — *Provisioned in [`infra/terraform/load_balancer.tf`](infra/terraform/load_balancer.tf)*

- **2.3 Pillar 3 — Rollback Support**
  - **Git-Commit Image Tag Rollbacks:** Because every container build tags images in Artifact Registry with the exact Git commit hash alongside `latest`, operators can roll back any service to a previously tested release in one command (`make deploy IMAGE_TAG=<previous-git-sha>`).  
    — *Provisioned in [`infra/terraform/cloud_run.tf`](infra/terraform/cloud_run.tf)*  
    — *Called in [`Makefile`](Makefile) & [`infra/cloudbuild/build-images.yaml`](infra/cloudbuild/build-images.yaml)*
  - **Versioned Infrastructure State & Tenant Data Recovery:** Object versioning is enabled on both the Terraform state bucket and the tenant vector database bucket, retaining previous file versions so operators can recover from an unintended infrastructure change or data write.  
    — *Provisioned in [`infra/terraform/storage.tf`](infra/terraform/storage.tf) & [`infra/terraform/versions.tf`](infra/terraform/versions.tf)*  
    — *Called in [`Makefile`](Makefile)*

> **Note on Unimplemented Pillars:**
> - **Automated Git-Webhook CI Triggers, Progressive Traffic Splitting (Blue/Green & Canary) & Automated Rollback Triggers (*Not Implemented — Relevant*):** While Cloud Run performs zero-downtime revision swaps and supports manual Git-commit rollbacks, automated Git-push CI triggers (such as GitHub Actions or Cloud Build Triggers running unit tests on pull requests), percentage-based blue/green or canary traffic splitting across revisions, and automated rollback triggers tied to Cloud Monitoring error or latency alerts ([`infra/terraform/observability.tf`](infra/terraform/observability.tf)) are not yet implemented and remain relevant future production enhancements.

---

### 3. Testing & Quality Engineering

Quality, security, and multi-tenant isolation are verified across unit, integration, end-to-end, and static infrastructure policy suites before code or Terraform changes are deployed:

- **3.1 Pillar 1 — Unit, Integration & End-to-End (E2E) Testing**
  - **Authentication & Tenant Identity Unit Tests:** Verifies cryptographic token validation, expired or forged token rejection, user ID format validation against directory traversal, local developer bypass behavior, and cloud production authentication enforcement.  
    — *Implemented in [`tests/auth.py`](tests/auth.py) & [`tests/helpers.py`](tests/helpers.py)*
  - **Storage Synchronization, Locking & Hybrid Search Unit Tests:** Verifies per-user Cloud Storage snapshot downloads, version tracking, incremental file uploads, distributed Cloud Storage write locks with expired-lock recovery, local memory cache eviction, and hybrid semantic + keyword search ranking.  
    — *Implemented in [`tests/storage.py`](tests/storage.py) & [`tests/helpers.py`](tests/helpers.py)*
  - **AI Security & Privacy Redaction Unit Tests:** Verifies prompt injection screening, XML quotation tags on retrieved chunks, approved model catalogs, collection-bound embedding consistency, PDF file-signature and upload size limits, two-stage personal data redaction, safe error messages, and content safety filters.  
    — *Implemented in [`tests/ai_security.py`](tests/ai_security.py) & [`tests/helpers.py`](tests/helpers.py)*
  - **Compliance, Residency & Quota Unit Tests:** Verifies single-region location checks, Organization Policy verification, privacy-safe audit logging, self-service GDPR data export and erasure, EU AI Act provenance badges, fail-closed security screening, per-user rate limiters, and collection count quotas.  
    — *Implemented in [`tests/compliance.py`](tests/compliance.py) & [`tests/helpers.py`](tests/helpers.py)*
  - **Observability & AI Telemetry Unit Tests:** Verifies structured JSON log formatting, automatic scrubbing of raw user questions from logs, request and trace correlation, per-stage execution timing spans, time-to-first-token tracking, multi-step AI token counting, and grounding quality scores.  
    — *Implemented in [`tests/observability.py`](tests/observability.py) & [`tests/helpers.py`](tests/helpers.py)*
  - **Scalability & Resource Efficiency Unit Tests:** Verifies route isolation between the ingestion and agent services, Cloud Run concurrency and resource settings, private subnet IP capacity, memory-pressure cache eviction, parallel privacy screening threads, and Cloud Monitoring scaling alerts.  
    — *Implemented in [`tests/scalability_elasticity.py`](tests/scalability_elasticity.py) & [`tests/helpers.py`](tests/helpers.py)*
  - **Multi-Tenant API Integration Test Suite:** Exercises the full ingestion and agent microservices end-to-end across multi-user PDF uploads, Cloud Storage archiving, similarity searches, collection management, GDPR data erasure, and live Server-Sent Events chat streaming—confirming that two concurrent users (Alice and Bob) remain completely isolated even when using identical collection and thread names.  
    — *Implemented in [`tests/multi_tenant_api.py`](tests/multi_tenant_api.py) & [`tests/helpers.py`](tests/helpers.py)*
  - **Live End-to-End Ingestion & Search Smoke Test:** Runs against a locally running ingestion service using real sample PDF documents, local ChromaDB storage, and live Google Cloud Vertex AI embedding generation to verify document upload and similarity search end-to-end.  
    — *Implemented in [`tests/ingestion.py`](tests/ingestion.py)*
  - **Live End-to-End Agent & Streaming Smoke Test:** Runs against a locally running agent service using live Vertex AI Gemini tool-calling to verify both standard JSON chat responses and real-time Server-Sent Events streaming.  
    — *Implemented in [`tests/agent.py`](tests/agent.py)*

- **3.2 Pillar 2 — Test & Policy Gate Automation**
  - **Offline Static Security & Infrastructure Policy Gate:** Automatically inspects Terraform files and Python configuration defaults before deployment to verify single-region defaults, fail-closed AI safety screening, Cloud Run concurrency and deletion protection, private subnet sizing, and observability sinks and alerts.  
    — *Implemented in [`infra/scripts/check_static_policies.py`](infra/scripts/check_static_policies.py) & [`Makefile`](Makefile)*
  - **Automated Terraform Syntax & Schema Gate:** Runs `terraform validate` automatically as part of the pre-deployment policy check before any infrastructure changes are planned or applied.  
    — *Implemented in [`Makefile`](Makefile) & [`infra/cloudbuild/run-terraform.yaml`](infra/cloudbuild/run-terraform.yaml)*
  - **Synthetic Policy Violation Mutation Testing:** The test suite creates temporary copies of the repository and injects deliberate misconfigurations (such as global regions, disabled fail-closed screening, missing deletion protection, or undersized subnets) to prove that the automated static policy gate reliably catches regressions.  
    — *Implemented in [`tests/helpers.py`](tests/helpers.py), [`tests/compliance.py`](tests/compliance.py) & [`tests/scalability_elasticity.py`](tests/scalability_elasticity.py)*

- **3.3 Pillar 3 — Test Coverage Across Architectural Layers**
  - **1-to-1 Architectural Layer & Pillar Verification:** Every backend package (`src/core`, `src/ingest`, `src/manage`, `src/query`, `src/agent`) and Terraform infrastructure module (`infra/terraform/*.tf`) is paired with dedicated positive and negative tests covering normal operation, invalid inputs, security blocks, and failure recovery.  
    — *Implemented in [`tests/auth.py`](tests/auth.py), [`tests/storage.py`](tests/storage.py), [`tests/multi_tenant_api.py`](tests/multi_tenant_api.py), [`tests/ai_security.py`](tests/ai_security.py), [`tests/compliance.py`](tests/compliance.py), [`tests/observability.py`](tests/observability.py) & [`tests/scalability_elasticity.py`](tests/scalability_elasticity.py)*

> **Note on Unimplemented Pillars:**
> - **Automated Load & Performance Benchmarking, Browser UI E2E Tests & Code-Coverage Threshold Gates (*Not Implemented — Relevant*):** While our test suite verifies concurrency ceilings, parallel privacy screening worker pools, and memory eviction logic, a dedicated load-testing tool (such as Locust or k6 stress-testing concurrent PDF uploads and chat streams against live Cloud Run instances), automated browser UI tests (such as Playwright or Cypress for the React dashboard), and automated code-coverage percentage gates in CI are not yet implemented and remain relevant future enhancements.

---

## III/ [DESIGNING FOR CHANGE]

### 1. Configuration Management

All runtime, security, scaling, and frontend settings are externalized from application code and automatically adapt across local development and cloud environments:

- **1.1 Pillar 1 — Externalized Configuration**
  - **Centralized Backend Configuration Module:** All backend settings—including Google Cloud project and region, approved chat and embedding model lists, upload size and chunk limits, AI safety templates, per-user rate limits, collection quotas, and local memory cache caps—are loaded from environment variables through a single validated configuration module rather than hardcoded across files.  
    — *Implemented in [`src/core/config.py`](src/core/config.py)*
  - **Parameterized Terraform Infrastructure Configuration:** Over 40 validated Terraform input variables (with a documented example template) control per-service CPU and memory limits, concurrency ceilings, minimum and maximum container counts, private subnet ranges, log retention windows, and alert thresholds without editing infrastructure code.  
    — *Provisioned in [`infra/terraform/variables.tf`](infra/terraform/variables.tf), [`infra/terraform/terraform.tfvars.example`](infra/terraform/terraform.tfvars.example) & [`infra/terraform/cloud_run.tf`](infra/terraform/cloud_run.tf)*
  - **Startup Runtime Configuration for the Web Frontend:** Single-page web apps normally bake backend URLs and authentication keys into static JavaScript files at image build time—before cloud resources even exist. Instead, when the frontend container starts on Cloud Run, a startup script reads the environment variables injected by Terraform and writes a runtime configuration file (`/env-config.js`) that the browser loads before starting React.  
    — *Provisioned in [`infra/terraform/cloud_run.tf`](infra/terraform/cloud_run.tf)*  
    — *Implemented in [`infra/docker/entrypoint-frontend.sh`](infra/docker/entrypoint-frontend.sh) & [`frontend/vite.config.js`](frontend/vite.config.js)*  
    — *Called in [`frontend/index.html`](frontend/index.html) & [`frontend/src/firebase.js`](frontend/src/firebase.js)*

- **1.2 Pillar 2 — Environment Separation**
  - **Automatic Cloud Production Runtime Mode:** When running on Google Cloud Run (`K_SERVICE` environment detection), the backend automatically switches to cloud production defaults—disabling local authentication bypasses, rejecting requests that reference local server file paths, requiring explicit CORS origin allowlists, and activating Cloud Storage synchronization.  
    — *Implemented in [`src/core/config.py`](src/core/config.py), [`src/core/auth.py`](src/core/auth.py), [`src/ingest/routes.py`](src/ingest/routes.py) & [`src/core/storage/paths.py`](src/core/storage/paths.py)*
  - **Developer-Friendly Local Execution Mode:** When running on a developer's laptop, the backend loads local `.env` settings, supports an optional local test-user identity (`DEV_USER_ID`), permits localhost browser origins, and stores vector collections directly on local disk when no Cloud Storage bucket is configured.  
    — *Implemented in [`src/core/config.py`](src/core/config.py), [`src/core/auth.py`](src/core/auth.py) & [`src/core/storage/paths.py`](src/core/storage/paths.py)*

- **1.3 Pillar 3 — Feature Flags**
  - **Runtime Operational & Developer Feature Toggles:** Boolean environment flags in the configuration module allow operators to toggle optional runtime capabilities—such as PDF upload archiving (`ARCHIVE_UPLOADS_TO_GCS`), direct Cloud Logging/Trace export (`AUDIT_CLOUD_LOGGING_ENABLED`, `TELEMETRY_CLOUD_LOGGING_ENABLED`, `STRUCTURED_LOGGING`, `CLOUD_TRACE_ENABLED`), fail-closed screening mode (`MODEL_ARMOR_FAIL_CLOSED`), and local developer debugging (`AUTH_DISABLED`, `AGENT_CONSOLE_TRACE`, `UVICORN_RELOAD`)—without changing application code.  
    — *Provisioned in [`infra/terraform/cloud_run.tf`](infra/terraform/cloud_run.tf)*  
    — *Implemented in [`src/core/config.py`](src/core/config.py)*
  - **Conditional Infrastructure Capability Toggles:** Boolean Terraform variables control whether optional cloud infrastructure capabilities—such as federated Google Sign-In (`enable_google_oauth`) or a permanent Write-Once-Read-Many (WORM) retention lock on the compliance log bucket (`audit_log_retention_locked`)—are provisioned.  
    — *Provisioned in [`infra/terraform/variables.tf`](infra/terraform/variables.tf), [`infra/terraform/auth.tf`](infra/terraform/auth.tf) & [`infra/terraform/compliance.tf`](infra/terraform/compliance.tf)*

> **Note on Unimplemented Pillars:**
> - **Dynamic Remote Feature-Flag Evaluation Service (*Not Implemented — Relevant*):** Feature flags are currently evaluated from environment variables injected into Cloud Run revisions (requiring a revision update to toggle in production). Integrating a dynamic remote feature-flag service (such as Firebase Remote Config or OpenFeature for live per-tenant or percentage-based rollouts without redeploying containers) is not yet implemented and is a relevant future enhancement.

---

### 2. Modularity & Abstraction

The backend is architected around clean package boundaries, typed schema contracts, swappable AI models, and dependency injection so domain logic, security guardrails, and infrastructure adapters evolve independently:

- **2.1 Pillar 1 — Loose Coupling**
  - **Shared Platform Core Package (`src/core`):** Centralizes cross-cutting platform capabilities—authentication, configuration, Cloud Storage synchronization, AI security guardrails, regulatory compliance, and observability—so domain packages never duplicate infrastructure logic.  
    — *Implemented in [`src/core/`](src/core/)*
  - **Dedicated Document Ingestion Package (`src/ingest`):** Encapsulates multipart PDF upload staging, page-by-page extraction, overlapping text chunking, and batched ChromaDB indexing without importing search or chat routes.  
    — *Implemented in [`src/ingest/`](src/ingest/)*
  - **Dedicated Collection & Privacy Management Package (`src/manage`):** Encapsulates vector collection listing, collection deletion, and self-service GDPR data inventory and erasure endpoints.  
    — *Implemented in [`src/manage/`](src/manage/)*
  - **Dedicated Direct Search Package (`src/query`):** Encapsulates hybrid semantic and keyword similarity search with sensitive-data redaction.  
    — *Implemented in [`src/query/`](src/query/)*
  - **Dedicated Conversational Agent Package (`src/agent`):** Encapsulates the stateful LangGraph workflow, dynamic search tool construction, multi-turn conversation memory, and real-time Server-Sent Events streaming.  
    — *Implemented in [`src/agent/`](src/agent/)*
  - **Composable Microservice Application Factory:** A shared FastAPI application factory configures CORS headers, request correlation middleware, and health check endpoints in one place, allowing `ingest-api` and `agent-api` to assemble independent microservices simply by registering their respective routers.  
    — *Implemented in [`src/core/app_factory.py`](src/core/app_factory.py), [`src/ingest/app.py`](src/ingest/app.py) & [`src/agent/app.py`](src/agent/app.py)*
  - **Decoupled Context-Variable State Propagation:** Per-request state travels across modules through thread-safe Python `contextvars` rather than passing logging, compliance, or telemetry objects through every domain function signature.  
    — *Implemented in [`src/core/context.py`](src/core/context.py), [`src/core/compliance/state.py`](src/core/compliance/state.py) & [`src/core/observability/state.py`](src/core/observability/state.py)*

- **2.2 Pillar 2 — Interface Contracts**
  - **Strict Request & Response Validation Schemas:** Every API endpoint and agent state transition is governed by explicit Pydantic and typed data schemas—validating collection names, query lengths, model choices, and compliance metadata upfront and rejecting malformed payloads (`HTTP 422`) before business logic runs.  
    — *Implemented in [`src/ingest/schemas.py`](src/ingest/schemas.py), [`src/query/schemas.py`](src/query/schemas.py), [`src/agent/schemas.py`](src/agent/schemas.py), [`src/core/auth.py`](src/core/auth.py) & [`src/core/ai_security/armor.py`](src/core/ai_security/armor.py)*
  - **Standardized AI, Embedding & Retriever Interfaces:** Storage, search, and agent modules program against standard LangChain document, embedding, message, and tool interfaces so the retrieval engine works seamlessly with both full ChromaDB hybrid search and lightweight test implementations.  
    — *Implemented in [`src/core/storage/chroma.py`](src/core/storage/chroma.py), [`src/agent/tools.py`](src/agent/tools.py) & [`src/agent/graph.py`](src/agent/graph.py)*

- **2.3 Pillar 3 — Model Swappability**
  - **Dynamic Per-Request Chat Model Switching:** Users and API callers can switch between any approved Vertex AI generative model (`gemini-2.5-flash`, `gemini-2.5-pro`, or `gemini-2.5-flash-lite`) on individual chat requests or expand the model catalog via environment variables without changing application code.  
    — *Implemented in [`src/core/config.py`](src/core/config.py) & [`src/agent/graph.py`](src/agent/graph.py)*  
    — *Called in [`src/agent/schemas.py`](src/agent/schemas.py), [`src/agent/routes.py`](src/agent/routes.py) & [`frontend/src/App.jsx`](frontend/src/App.jsx)*
  - **Per-Collection Embedding Model Swappability:** Users can choose between multiple Vertex AI embedding architectures (`gemini-embedding-001`, `text-embedding-004`, or `text-embedding-005`) when creating a new collection without modifying storage or retrieval code.  
    — *Implemented in [`src/core/config.py`](src/core/config.py) & [`src/core/storage/chroma.py`](src/core/storage/chroma.py)*  
    — *Called in [`src/ingest/service.py`](src/ingest/service.py), [`src/query/service.py`](src/query/service.py), [`src/agent/tools.py`](src/agent/tools.py) & [`src/manage/collections.py`](src/manage/collections.py)*

- **2.4 Pillar 4 — Dependency Injection**
  - **Request-Scoped Authentication Injection:** Token verification and user identity extraction are injected into FastAPI route handlers as declarative request dependencies (`Depends(get_current_user)`) rather than called manually inside endpoint bodies.  
    — *Implemented in [`src/core/auth.py`](src/core/auth.py)*  
    — *Called in [`src/ingest/routes.py`](src/ingest/routes.py), [`src/query/routes.py`](src/query/routes.py), [`src/agent/routes.py`](src/agent/routes.py), [`src/manage/collections.py`](src/manage/collections.py) & [`src/manage/compliance.py`](src/manage/compliance.py)*
  - **Pluggable Service Factories for Offline Testing:** Core builders—including the FastAPI app factory, the LangGraph agent builder, the sliding-window rate limiter, and cloud storage and AI client factories—accept injected dependencies so the test suite can substitute deterministic in-memory fakes for Cloud Storage, Vertex AI embeddings, Gemini chat models, and Model Armor completely offline.  
    — *Implemented in [`src/core/app_factory.py`](src/core/app_factory.py), [`src/agent/graph.py`](src/agent/graph.py), [`src/core/compliance/policy.py`](src/core/compliance/policy.py), [`src/core/storage/chroma.py`](src/core/storage/chroma.py), [`src/core/storage/gcs_sync.py`](src/core/storage/gcs_sync.py) & [`tests/helpers.py`](tests/helpers.py)*

> **Note on Unimplemented / Non-Relevant Pillars:**
> - **Multi-Cloud LLM / External Vector Database Adapter Plugins (*Not Implemented — Not Relevant*):** Swapping to non-GCP language model providers or external vector database servers is not relevant to this project's architecture, which is specifically designed around single-region Google Cloud data residency (**Vertex AI** accessed over Private Google Access) and serverless per-tenant **ChromaDB** snapshots synchronized with **Google Cloud Storage**.

---

## IV/ [PERFORMANCE & COST OPTIMIZATION]

> *Splitting Performance & Cost Optimization into two dedicated layers—**1. Scalability & Elasticity** (how the platform isolates workloads, scales horizontally, reserves subnet IP headroom, and parallelizes retrieval I/O) and **2. Resource Efficiency** (how the platform right-sizes compute, scales to zero, reuses warm caches, bounds RAM usage, and minimizes storage/network overhead)—separates capacity scaling concerns from cost and resource optimization.*

### 1. Scalability & Elasticity

InteractiveRAG isolates heavy document indexing from low-latency user queries and scales horizontally on demand across dedicated serverless instances and private `/24` subnets:

- **1.1 Pillar 1 — Workload-Isolated Microservice Packaging (4 Packages Across 2 Backend Services)**
  - **Dedicated Heavy-Write Ingestion Service (`ingest-api`):** Runs only the document ingestion package (`POST /ingest`) on dedicated instances so CPU-intensive PDF parsing, text chunking, and vector index building never compete with or slow down live user chats.  
    — *Provisioned in [`infra/terraform/cloud_run.tf`](infra/terraform/cloud_run.tf)*  
    — *Implemented in [`src/ingest/routes.py`](src/ingest/routes.py)*  
    — *Called in [`src/ingest/app.py`](src/ingest/app.py)*
  - **Unified Interactive Read, Management & Agent Service (`agent-api`):** Co-locates collection management (`src/manage`), direct similarity search (`src/query`), and multi-turn agent chat (`src/agent`) on a single interactive service so searches and follow-up chat turns share the same warm local vector cache, unified query rate limiter, and atomic GDPR data erasure workflow.  
    — *Provisioned in [`infra/terraform/cloud_run.tf`](infra/terraform/cloud_run.tf)*  
    — *Implemented in [`src/agent/routes.py`](src/agent/routes.py), [`src/query/routes.py`](src/query/routes.py) & [`src/manage/routes.py`](src/manage/routes.py)*  
    — *Called in [`src/agent/app.py`](src/agent/app.py)*

- **1.2 Pillar 2 — Horizontal Autoscaling, Concurrency Tuning & VPC Subnet Headroom**
  - **Workload-Tuned Per-Container Concurrency Limits:** Each Cloud Run service enforces a per-container request concurrency ceiling matched to its workload—limiting `ingest-api` to 4 concurrent uploads per container to prevent CPU contention during PDF parsing, allowing 32 concurrent interactive requests per container on `agent-api` (where requests spend most of their time waiting on external AI and privacy API calls), and allowing 80 concurrent requests on the static `frontend`.  
    — *Provisioned in [`infra/terraform/cloud_run.tf`](infra/terraform/cloud_run.tf) & [`infra/terraform/variables.tf`](infra/terraform/variables.tf)*
  - **Service-Specific Horizontal Autoscaling Ceilings:** Cloud Run automatically adds or removes container instances based on incoming traffic up to service-specific caps—capping `ingest-api` at 5 instances (20 concurrent PDF uploads) to protect downstream Vertex AI embedding and Cloud DLP quotas, while scaling `agent-api` up to 10 instances (320 concurrent interactive requests) and `frontend` up to 5 instances (400 concurrent requests).  
    — *Provisioned in [`infra/terraform/cloud_run.tf`](infra/terraform/cloud_run.tf) & [`infra/terraform/variables.tf`](infra/terraform/variables.tf)*
  - **Expanded `/24` Private VPC Subnets for Scale-Out Headroom:** Both private backend subnets (`ingest-subnet` and `agent-subnet`) are sized as `/24` CIDR blocks (251 usable IP addresses each), ensuring serverless Cloud Run Direct VPC Egress never runs out of private IP addresses during traffic spikes or revision rollouts.  
    — *Provisioned in [`infra/terraform/network.tf`](infra/terraform/network.tf) & [`infra/terraform/variables.tf`](infra/terraform/variables.tf)*  
    — *Called in [`infra/terraform/cloud_run.tf`](infra/terraform/cloud_run.tf)*

- **1.3 Pillar 3 — Parallel Retrieval-Time Throughput**
  - **Multi-Threaded Retrieval Privacy Screening:** When a similarity search or agent tool call retrieves top-$k$ document chunks from ChromaDB, all retrieved chunks are screened concurrently across a bounded pool of up to 8 worker threads (1 chunk per thread) instead of scanning them sequentially—keeping retrieval screening latency near the duration of a single chunk scan even as $k$ increases.  
    — *Implemented in [`src/core/ai_security/armor.py`](src/core/ai_security/armor.py)*  
    — *Called in [`src/query/service.py`](src/query/service.py) & [`src/agent/tools.py`](src/agent/tools.py)*

---

### 2. Resource Efficiency

Compute, memory, storage, and network bandwidth are optimized so idle infrastructure costs $0 while active workloads minimize redundant data transfers and memory consumption:

- **2.1 Pillar 1 — Compute Right-Sizing & Scale-to-Zero Billing**
  - **Right-Sized CPU & Memory per Service:** Rather than over-provisioning every container for peak PDF parsing memory, compute resources are tailored to each service—allocating 2 vCPU and 4 GiB of RAM to `ingest-api` (for PDF extraction, batch embeddings, and vector index writes), 2 vCPU and 2 GiB of RAM to `agent-api` (for I/O-bound agent streaming and local vector cache reads), and 1 vCPU and 512 MiB of RAM to `frontend`.  
    — *Provisioned in [`infra/terraform/cloud_run.tf`](infra/terraform/cloud_run.tf) & [`infra/terraform/variables.tf`](infra/terraform/variables.tf)*
  - **Scale-to-Zero Idle Instance Elimination:** All three Cloud Run services are configured with a minimum instance count of zero so container instances spin down completely when no users are active, eliminating idle compute charges.  
    — *Provisioned in [`infra/terraform/cloud_run.tf`](infra/terraform/cloud_run.tf) & [`infra/terraform/variables.tf`](infra/terraform/variables.tf)*
  - **Request-Scoped CPU Billing:** Cloud Run CPU throttling is enabled on all services so CPU is allocated and billed only while a container is actively processing an HTTP request or open chat stream.  
    — *Provisioned in [`infra/terraform/cloud_run.tf`](infra/terraform/cloud_run.tf) & [`infra/terraform/variables.tf`](infra/terraform/variables.tf)*
  - **Temporary Cold-Start CPU Boost:** Cloud Run startup CPU boost is enabled on all services, temporarily allocating extra CPU power during container initialization to speed up Python and LangChain startup times without paying for permanently larger CPUs.  
    — *Provisioned in [`infra/terraform/cloud_run.tf`](infra/terraform/cloud_run.tf) & [`infra/terraform/variables.tf`](infra/terraform/variables.tf)*

- **2.2 Pillar 2 — Session Affinity & Warm In-Memory Cache Reuse**
  - **Session Affinity for Multi-Turn Conversations:** Cloud Run session affinity is enabled on `agent-api` so consecutive requests from the same user are routed to the same warm container instance whenever possible, reusing the user's in-memory conversation history and cached local vector database.  
    — *Provisioned in [`infra/terraform/cloud_run.tf`](infra/terraform/cloud_run.tf)*
  - **Version-Checked Snapshot Downloads (`version.json`):** Before downloading any vector database files from Cloud Storage, the backend checks the user's lightweight remote version marker (`version.json`) and skips the download completely whenever the container's local copy is already up to date.  
    — *Implemented in [`src/core/storage/gcs_sync.py`](src/core/storage/gcs_sync.py)*  
    — *Called in [`src/core/storage/chroma.py`](src/core/storage/chroma.py)*

- **2.3 Pillar 3 — Bounded In-Memory Vector Cache Quotas & Eviction**
  - **Maximum Local Vector Cache Size Cap (512 MiB) & Active-User-Protected LRU Eviction:** Because Cloud Run stores local `/tmp` files directly inside container RAM, both backend services enforce a 512 MiB ceiling on cached user vector databases and automatically evict the oldest inactive user caches in Least-Recently-Used (LRU) order while keeping active readers and writers pinned.  
    — *Provisioned in [`infra/terraform/cloud_run.tf`](infra/terraform/cloud_run.tf) & [`infra/terraform/variables.tf`](infra/terraform/variables.tf)*  
    — *Implemented in [`src/core/storage/locks.py`](src/core/storage/locks.py) & [`src/core/storage/paths.py`](src/core/storage/paths.py)*  
    — *Called in [`src/core/storage/gcs_sync.py`](src/core/storage/gcs_sync.py)*
  - **Minimum Free Container Memory Safety Floor (256 MiB):** Before syncing a user's vector database into local memory, the cache manager verifies that at least 256 MiB of free container memory remains—evicting inactive user caches and closing cached SQLite client connections first to prevent out-of-memory container crashes.  
    — *Provisioned in [`infra/terraform/cloud_run.tf`](infra/terraform/cloud_run.tf) & [`infra/terraform/variables.tf`](infra/terraform/variables.tf)*  
    — *Implemented in [`src/core/storage/locks.py`](src/core/storage/locks.py) & [`src/core/storage/paths.py`](src/core/storage/paths.py)*

- **2.4 Pillar 4 — Storage & Network Cost Efficiency**
  - **Incremental MD5 Checksum File Uploads:** When saving updated vector database files back to Cloud Storage after an upload or collection deletion, the backend compares local file MD5 checksums against Cloud Storage metadata, uploads only new or modified files, and increments `version.json`, avoiding redundant multi-megabyte transfers.  
    — *Implemented in [`src/core/storage/gcs_sync.py`](src/core/storage/gcs_sync.py)*  
    — *Called in [`src/ingest/routes.py`](src/ingest/routes.py) & [`src/manage/collections.py`](src/manage/collections.py)*
  - **Automated 7-Day Archived Object Version Cleanup:** Cloud Storage lifecycle rules automatically delete older archived file versions after 7 days or once 3 newer versions exist, preventing unbounded storage growth while preserving recent rollback history.  
    — *Provisioned in [`infra/terraform/storage.tf`](infra/terraform/storage.tf)*
  - **Frontend Gzip Compression & Long-Lived Static Asset Caching:** The frontend Nginx server compresses JavaScript, CSS, and JSON payloads with Gzip and sets 1-year immutable browser caching headers on fingerprinted static bundles while keeping `/env-config.js` uncached.  
    — *Implemented in [`infra/docker/nginx.conf.template`](infra/docker/nginx.conf.template)*

> **Note on Unimplemented / Non-Relevant Pillars:**
> - **GPU Right-Sizing & Spot / Preemptible Compute Usage (*Not Implemented — Not Relevant*):** Provisioning or right-sizing GPUs and Spot/Preemptible virtual machines is not relevant to InteractiveRAG's serverless architecture because all deep-learning inference (Gemini reasoning, dense vector embedding generation, and Model Armor safety screening) is offloaded to fully managed, pay-per-use **Google Cloud Vertex AI** and **Model Armor** regional APIs, while the application tier runs on serverless **Google Cloud Run** containers that scale to zero when idle.

---

## V/ [SECURITY, PRIVACY, AND COMPLIANCE]

### 1. Authentication & Authorization

Identity and access controls follow a zero-trust model across both cloud infrastructure (dedicated service accounts, scoped access policies, and least privilege) and application requests (authenticated user tokens and token-derived tenant identity):

- **1.1 Dedicated Service Accounts**
  - **Backend Runtime Service Account:** A dedicated service account is attached exclusively to the Ingestion and Agent Cloud Run microservices instead of using Google Cloud's overly broad default compute account.  
    — *Provisioned in [`infra/terraform/iam.tf`](infra/terraform/iam.tf) & [`infra/terraform/cloud_run.tf`](infra/terraform/cloud_run.tf)*
  - **Frontend Runtime Service Account:** A separate dedicated service account is attached exclusively to the Frontend web container so the public web tier never shares credentials with the backend APIs.  
    — *Provisioned in [`infra/terraform/iam.tf`](infra/terraform/iam.tf) & [`infra/terraform/cloud_run.tf`](infra/terraform/cloud_run.tf)*
  - **Deployment & Build Service Account:** A dedicated deployer service account is used exclusively for automated container image builds and infrastructure updates so human operators do not run deployments under personal credentials.  
    — *Provisioned in [`Makefile`](Makefile)*
  - **Model Armor Managed Service Agent:** A Google-managed service identity is provisioned for Google Cloud Model Armor so it can invoke regional Cloud DLP redaction templates on the backend's behalf.  
    — *Provisioned in [`infra/terraform/ai_security.tf`](infra/terraform/ai_security.tf)*

- **1.2 Identity & Access Management (IAM)**
  - **Declarative Role Bindings:** All cloud permissions are defined explicitly in version-controlled infrastructure code rather than granted manually in the cloud console.  
    — *Provisioned in [`infra/terraform/iam.tf`](infra/terraform/iam.tf) & [`infra/terraform/ai_security.tf`](infra/terraform/ai_security.tf)*
  - **Uniform Bucket-Level Access & Public Access Prevention:** The Cloud Storage persistence bucket disables legacy per-file access lists and enforces strict public-access prevention so every storage operation is governed exclusively by Cloud IAM.  
    — *Provisioned in [`infra/terraform/storage.tf`](infra/terraform/storage.tf)*

- **1.3 Principle of Least Privilege**
  - **Zero-Privilege Frontend Service Account:** The Frontend service account is granted zero Google Cloud permissions because the web server only delivers static browser files and runtime configuration and never calls backend cloud APIs.  
    — *Provisioned in [`infra/terraform/iam.tf`](infra/terraform/iam.tf)*
  - **Bucket-Scoped Backend Storage Permissions & Minimal Project Roles:** The Backend service account is granted storage read/write access strictly on the single application data bucket (never across the whole project, preventing access to the infrastructure state bucket), and its project-level permissions are limited to calling Vertex AI, Model Armor, Cloud DLP, Cloud Logging, Cloud Monitoring, and Cloud Trace.  
    — *Provisioned in [`infra/terraform/iam.tf`](infra/terraform/iam.tf)*
  - **Deployer Separation of Duties:** The deployment service account is granted only the permissions required to provision application resources and is intentionally denied permission to modify Google Cloud Organization Policies—meaning it can verify regional compliance rules before deploying, but can never weaken or override them.  
    — *Provisioned in [`Makefile`](Makefile)*  
    — *Called in [`infra/scripts/check_region_policy.py`](infra/scripts/check_region_policy.py)*
  - **Non-Root Container Execution:** Inside the backend Docker image, root privileges are dropped before startup so the web server runs as an unprivileged operating-system user.  
    — *Implemented in [`infra/docker/Dockerfile.backend`](infra/docker/Dockerfile.backend)*

- **1.4 User Authentication & Zero-Trust Authorization**
  - **Managed Google Sign-In & Email/Password Authentication:** Google Cloud Identity Platform (Firebase Authentication) is provisioned to let users sign in with either federated Google Sign-In or an Email and Password account.  
    — *Provisioned in [`infra/terraform/auth.tf`](infra/terraform/auth.tf)*  
    — *Implemented in [`frontend/src/firebase.js`](frontend/src/firebase.js)*  
    — *Called in [`frontend/src/AuthGate.jsx`](frontend/src/AuthGate.jsx)*
  - **Automatic Bearer Token Attachment, Refresh & Sign-Out Cleanup:** The web application attaches the signed-in user's identity token to every API and streaming request, refreshes expired tokens automatically through the Firebase Authentication SDK, and clears all local collection lists, active conversation threads, and chat messages immediately upon sign-out or account switch.  
    — *Implemented in [`frontend/src/firebase.js`](frontend/src/firebase.js), [`frontend/src/api.js`](frontend/src/api.js) & [`frontend/src/App.jsx`](frontend/src/App.jsx)*  
    — *Called in [`frontend/src/AuthGate.jsx`](frontend/src/AuthGate.jsx)*
  - **Cryptographic Backend Token Verification & Token-Derived Tenant Identity:** Every protected backend endpoint verifies the incoming token's cryptographic signature, expiration time, and project audience against Google's public signing certificates, extracts the user's workspace identity (`uid`) exclusively from the verified token claims (never from client-supplied form fields or query parameters), validates it against a strict alphanumeric pattern to prevent directory traversal, and hard-disables the local authentication bypass whenever running in Cloud Run.  
    — *Implemented in [`src/core/auth.py`](src/core/auth.py)*  
    — *Called in [`src/core/storage/paths.py`](src/core/storage/paths.py), [`src/ingest/routes.py`](src/ingest/routes.py), [`src/query/routes.py`](src/query/routes.py), [`src/agent/routes.py`](src/agent/routes.py), [`src/manage/collections.py`](src/manage/collections.py) & [`src/manage/compliance.py`](src/manage/compliance.py)*

- **1.5 API Key Scoping & Keyless Backend Credentials**
  - **Keyless Backend Service-to-Service Authentication:** Backend containers never use static API keys or downloadable JSON key files to call Vertex AI, Cloud Storage, Model Armor, Cloud DLP, or Cloud Logging; instead, they authenticate automatically using short-lived credentials attached to their Cloud Run service account.  
    — *Provisioned in [`infra/terraform/iam.tf`](infra/terraform/iam.tf), [`infra/terraform/cloud_run.tf`](infra/terraform/cloud_run.tf) & [`Makefile`](Makefile)*
  - **Authorized-Domain & Cross-Origin Restrictions for Browser Sign-In:** The public browser authentication key used by the web interface is restricted to an explicit allowlist of authorized domains (the Load Balancer HTTPS domain, the Frontend service domain, and local development hosts) and paired with strict backend cross-origin (CORS) request filtering.  
    — *Provisioned in [`infra/terraform/auth.tf`](infra/terraform/auth.tf) & [`infra/terraform/cloud_run.tf`](infra/terraform/cloud_run.tf)*  
    — *Implemented in [`src/core/config.py`](src/core/config.py) & [`src/core/app_factory.py`](src/core/app_factory.py)*

> **Note on Unimplemented Pillars:**
> - **Fine-Grained Sub-Tenant Role-Based Access Control (*Not Implemented — Relevant*):** Each authenticated user currently owns an isolated personal workspace with full read/write/delete rights over their own collections. Fine-grained sub-tenant roles (such as team administrator versus read-only viewer roles within a shared organization workspace) are not yet implemented and remain a relevant future enhancement.

---

### 2. Infrastructure & Network Security

External traffic is isolated from public edge caches and restricted to the Load Balancer entry point, while backend containers run inside a segmented private network with zero outbound access to the public internet:

- **2.1 Edge Cache Isolation**
  - **Disabled Edge Caching for Private Multi-Tenant Traffic:** Content Delivery Network (CDN) caching is explicitly turned off across all Load Balancer backend services so private multi-tenant API responses and live chat streams are never cached at public network edge locations.  
    — *Provisioned in [`infra/terraform/load_balancer.tf`](infra/terraform/load_balancer.tf)*

- **2.2 Locked-Down Container Ingress**
  - **Blocking Direct Internet Access to Cloud Run URLs:** All three Cloud Run services are configured with `INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER` to accept incoming traffic exclusively from the Load Balancer. Anyone attempting to visit an underlying `*.run.app` service URL directly from the public internet is blocked at Google's network edge before the request ever reaches a container.  
    — *Provisioned in [`infra/terraform/cloud_run.tf`](infra/terraform/cloud_run.tf)*

- **2.3 Segmented Private Network with Zero Outbound Internet**
  - **Custom Private Network Without an Internet Exit:** Backend services run inside a custom Virtual Private Cloud (VPC) network whose default route to the public internet (`0.0.0.0/0`) is deleted upon creation. Even if a malicious file or prompt attempted to trick a container into sending data to an outside server, the network drops the connection immediately.  
    — *Provisioned in [`infra/terraform/network.tf`](infra/terraform/network.tf)*
  - **Workload-Segmented Private Subnets:** The Ingestion service (`ingest-subnet`) and the Agent service (`agent-subnet`) are placed in separate private network subnets to isolate document parsing traffic from AI reasoning traffic at the network layer.  
    — *Provisioned in [`infra/terraform/network.tf`](infra/terraform/network.tf)*  
    — *Called in [`infra/terraform/cloud_run.tf`](infra/terraform/cloud_run.tf)*
  - **Mandatory Private Network Routing for All Outbound Traffic:** Both backend services force 100% of their outbound traffic (`ALL_TRAFFIC`) through the private VPC network using serverless Direct VPC Egress so no container can bypass the private network rules.  
    — *Provisioned in [`infra/terraform/cloud_run.tf`](infra/terraform/cloud_run.tf)*

- **2.4 Private Internal Tunnels to Google Cloud APIs**
  - **Private Google Access for Standard Google APIs:** A private DNS zone (`googleapis.com`) and restricted internal route (`199.36.153.8/30`) direct all backend calls to Cloud Storage, Vertex AI, Identity Platform, Cloud Logging, Cloud Trace, and Cloud DLP over Google's private internal backbone rather than the public internet.  
    — *Provisioned in [`infra/terraform/network.tf`](infra/terraform/network.tf)*
  - **Regional Private Service Connect for Model Armor:** A dedicated regional Private Service Connect endpoint and private DNS zone (`modelarmor.<region>.rep.googleapis.com`) route all AI prompt and response safety screening calls to Google Cloud Model Armor entirely over private internal IP addresses.  
    — *Provisioned in [`infra/terraform/network.tf`](infra/terraform/network.tf)*

---

### 3. Data Protection & Privacy

Data and credentials are protected across their lifecycle through encryption in transit and at rest, four-tier data classification and storage segregation, and managed secrets:

- **3.1 Encryption at Rest & in Transit**
  - **HTTPS/TLS Encryption & Automatic HTTP-to-HTTPS Redirect at the Edge:** All external browser and API traffic is encrypted in transit using a Google-managed SSL/TLS certificate on the global HTTPS Load Balancer (port 443), with an HTTP listener on port 80 that automatically returns a `301 Moved Permanently` redirect to HTTPS.  
    — *Provisioned in [`infra/terraform/load_balancer.tf`](infra/terraform/load_balancer.tf)*  
    — *Called in [`infra/terraform/auth.tf`](infra/terraform/auth.tf) & [`infra/terraform/outputs.tf`](infra/terraform/outputs.tf)*
  - **Encrypted Private Backbone Traffic to Google Cloud APIs:** All internal backend calls to Cloud Storage, Vertex AI, Cloud DLP, Model Armor, and Cloud Logging travel over encrypted HTTPS/TLS channels inside Google's private internal network.  
    — *Provisioned in [`infra/terraform/network.tf`](infra/terraform/network.tf) & [`infra/terraform/cloud_run.tf`](infra/terraform/cloud_run.tf)*
  - **Default AES-256 Encryption at Rest Across All Cloud Storage & Logs:** All persistent cloud data—including per-user vector database snapshots, uploaded PDF archives, remote infrastructure state, compliance and operational log buckets, container images, and stored secrets—is encrypted at rest by default using Google-managed AES-256 encryption.  
    — *Provisioned in [`infra/terraform/storage.tf`](infra/terraform/storage.tf), [`infra/terraform/compliance.tf`](infra/terraform/compliance.tf) & [`infra/terraform/observability.tf`](infra/terraform/observability.tf)*
  - **Ephemeral In-Memory Container Working Directories:** On Cloud Run Gen2, local vector database caches (`/tmp/chroma_db/{uid}`) and temporary PDF upload staging folders (`/tmp/uploads/{uid}/{request_id}`) reside strictly in volatile RAM-backed `tmpfs` memory (never written to unencrypted persistent local disks).  
    — *Provisioned in [`infra/terraform/cloud_run.tf`](infra/terraform/cloud_run.tf)*  
    — *Implemented in [`src/ingest/upload_parser.py`](src/ingest/upload_parser.py), [`src/core/storage/locks.py`](src/core/storage/locks.py) & [`src/core/compliance/dsar.py`](src/core/compliance/dsar.py)*

- **3.2 Data Classification & Storage Segregation**
  - **Tier 1 — Restricted Tenant Content:** Uploaded PDFs, vector indexes, and conversation threads are segregated into per-user Cloud Storage prefixes (`users/{uid}/`) and isolated in-memory namespaces within the private tenant persistence bucket.  
    — *Provisioned in [`infra/terraform/storage.tf`](infra/terraform/storage.tf)*  
    — *Implemented in [`src/core/storage/paths.py`](src/core/storage/paths.py) & [`src/agent/memory.py`](src/agent/memory.py)*
  - **Tier 2 — Confidential Credentials & Identity Material:** Sensitive OAuth client credentials and identity signing material are segregated from application data buckets and container images, residing exclusively in Google Cloud Secret Manager and Identity Platform.  
    — *Provisioned in [`infra/terraform/auth.tf`](infra/terraform/auth.tf) & [`infra/terraform/variables.tf`](infra/terraform/variables.tf)*
  - **Tier 3 — Regulated Compliance Audit Records (365-Day Retention):** Compliance audit trails and Google Cloud Data Access Audit Logs are segregated into a dedicated regional Cloud Logging bucket (`<project>-<env>-audit-logs`) with a 365-day retention policy.  
    — *Provisioned in [`infra/terraform/compliance.tf`](infra/terraform/compliance.tf)*
  - **Tier 4 — Operational & AI Telemetry Diagnostics (30-Day Retention):** High-volume operational diagnostics and AI performance metrics are segregated into a separate regional Cloud Logging bucket (`<project>-<env>-observability-logs`) with a 30-day retention policy so operational noise never pollutes long-term compliance archives.  
    — *Provisioned in [`infra/terraform/observability.tf`](infra/terraform/observability.tf)*

- **3.3 Secrets Management**
  - **Google Cloud Secret Manager for OAuth Credentials:** Sensitive OAuth 2.0 client credentials are never stored in configuration files or source control; Terraform stores only the Secret Manager secret names and retrieves secret payloads directly from Google Cloud Secret Manager during deployment.  
    — *Provisioned in [`infra/terraform/auth.tf`](infra/terraform/auth.tf) & [`infra/terraform/variables.tf`](infra/terraform/variables.tf)*
  - **Git & Docker Build Secret Exclusions:** Local environment files (`.env*`), Terraform variable files (`terraform.tfvars*`, `*.tfstate*`), and credential files are excluded from both Git version control and Docker build contexts so local developer secrets can never be committed or baked into container images.  
    — *Implemented in [`.gitignore`](.gitignore) & [`.dockerignore`](.dockerignore)*

> **Note on Unimplemented Pillars:**
> - **Customer-Managed Encryption Keys (Cloud KMS CMEK) & Automated Per-Document Sensitivity Labeling (*Not Implemented — Relevant*):** While all buckets and logs are encrypted at rest with Google-managed encryption keys and data is segregated across four architectural sensitivity tiers, Customer-Managed Encryption Keys (Cloud KMS) and automated per-document sensitivity tagging at upload time remain relevant future enhancements for regulated enterprises.

---

### 4. AI-Specific Security

Every user question, retrieved document chunk, and AI reply flows through a multi-stage AI security and sensitive-data redaction pipeline organized around five pillars:

- **4.1 Prompt Injection Mitigation**
  - **Front-Door Prompt Screening:** Before calling the Gemini language model, the agent sends the user's question to Google Cloud Model Armor to detect prompt-injection attacks, jailbreak attempts, and malicious web links. Unsafe questions are blocked immediately before spending language model tokens.  
    — *Provisioned in [`infra/terraform/ai_security.tf`](infra/terraform/ai_security.tf)*  
    — *Implemented in [`src/core/ai_security/armor.py`](src/core/ai_security/armor.py)*  
    — *Called in [`src/agent/graph.py`](src/agent/graph.py)*
  - **XML Boundary Tagging for Retrieved PDF Chunks:** Because malicious instructions hidden inside an uploaded PDF could attempt to impersonate system commands, every retrieved passage is wrapped inside explicit `<retrieved_context>` XML tags with internal angle brackets escaped first, and the system instructions direct the model to treat everything inside those tags strictly as untrusted reference text.  
    — *Implemented in [`src/core/ai_security/guardrails.py`](src/core/ai_security/guardrails.py)*  
    — *Called in [`src/agent/tools.py`](src/agent/tools.py) & [`src/agent/graph.py`](src/agent/graph.py)*
  - **Safe Collection Naming & Tool-Name Sanitization:** Collection names are validated against a strict alphanumeric pattern across all endpoints and sanitized before binding to internal search tools so attackers cannot smuggle prompt instructions through collection names.  
    — *Implemented in [`src/core/ai_security/guardrails.py`](src/core/ai_security/guardrails.py)*  
    — *Called in [`src/ingest/schemas.py`](src/ingest/schemas.py), [`src/ingest/upload_parser.py`](src/ingest/upload_parser.py), [`src/query/schemas.py`](src/query/schemas.py), [`src/agent/schemas.py`](src/agent/schemas.py), [`src/agent/tools.py`](src/agent/tools.py) & [`src/manage/collections.py`](src/manage/collections.py)*

- **4.2 Model Access Controls**
  - **Curated Chat & Embedding Model Allowlists:** Instead of allowing callers to request arbitrary Vertex AI models, the backend enforces explicit allowlists of approved Gemini chat models (`ALLOWED_LLM_MODELS`) and Vertex AI embedding models (`ALLOWED_EMBEDDING_MODELS`).  
    — *Provisioned in [`infra/terraform/cloud_run.tf`](infra/terraform/cloud_run.tf)*  
    — *Implemented in [`src/core/config.py`](src/core/config.py) & [`src/core/ai_security/guardrails.py`](src/core/ai_security/guardrails.py)*
  - **Strict Model Validation on Every Request:** Every upload, search, and chat request validates the requested model against the approved catalog, defaults to the primary approved model when omitted, and immediately rejects unapproved model names.  
    — *Implemented in [`src/core/ai_security/guardrails.py`](src/core/ai_security/guardrails.py)*  
    — *Called in [`src/ingest/schemas.py`](src/ingest/schemas.py), [`src/ingest/upload_parser.py`](src/ingest/upload_parser.py), [`src/query/schemas.py`](src/query/schemas.py) & [`src/agent/schemas.py`](src/agent/schemas.py)*

- **4.3 Input & Ingestion Guardrails**
  - **Query Length & Search Result Bounds:** Request validators cap user question length at 4,000 characters and constrain similarity search result counts between 1 and 50 passages.  
    — *Implemented in [`src/core/ai_security/guardrails.py`](src/core/ai_security/guardrails.py)*  
    — *Called in [`src/query/schemas.py`](src/query/schemas.py) & [`src/agent/schemas.py`](src/agent/schemas.py)*
  - **PDF File-Signature Verification & Upload Ceilings:** Upload validators verify that every file starts with an authentic `%PDF-` binary header, sanitize filenames to prevent directory traversal, and cap uploads at 20 MB per file, 10 files per request, and 1,000 extracted chunks per batch.  
    — *Implemented in [`src/core/ai_security/guardrails.py`](src/core/ai_security/guardrails.py)*  
    — *Called in [`src/ingest/upload_parser.py`](src/ingest/upload_parser.py) & [`src/ingest/splitter.py`](src/ingest/splitter.py)*

- **4.4 Sensitive Data Protection (Cloud DLP) & Output Sanitization**
  - **Regional Cloud DLP Inspection & De-Identification Templates:** Regional Google Cloud Sensitive Data Protection (Cloud DLP) templates wired into Model Armor detect email addresses, phone numbers, credit card numbers, US Social Security numbers, IBAN bank codes, Google Cloud API keys, and authentication tokens and replace any match with a descriptive `[INFO_TYPE]` placeholder (such as `[EMAIL_ADDRESS]`).  
    — *Provisioned in [`infra/terraform/ai_security.tf`](infra/terraform/ai_security.tf)*  
    — *Called in [`infra/terraform/cloud_run.tf`](infra/terraform/cloud_run.tf)*
  - **Two-Stage Redaction Across Retrieval Chunks & Final AI Responses:** Sensitive personal data and credentials are masked both when document passages are retrieved from ChromaDB (before they enter direct search results or the LLM prompt context) and a second time on the final AI response before answers are returned or streamed over SSE.  
    — *Provisioned in [`infra/terraform/ai_security.tf`](infra/terraform/ai_security.tf)*  
    — *Implemented in [`src/core/ai_security/armor.py`](src/core/ai_security/armor.py)*  
    — *Called in [`src/query/service.py`](src/query/service.py), [`src/agent/tools.py`](src/agent/tools.py), [`src/agent/graph.py`](src/agent/graph.py) & [`src/agent/service.py`](src/agent/service.py)*
  - **Sanitized Server Error Messages:** Unexpected exceptions across all REST and streaming endpoints log full diagnostic details privately on the server while returning a generic, safe error message to the client so internal stack traces, file paths, and environment variables are never leaked.  
    — *Implemented in [`src/core/ai_security/safety.py`](src/core/ai_security/safety.py)*  
    — *Called in [`src/ingest/routes.py`](src/ingest/routes.py), [`src/query/routes.py`](src/query/routes.py), [`src/agent/routes.py`](src/agent/routes.py) & [`src/agent/service.py`](src/agent/service.py)*

- **4.5 Content Safety**
  - **Google Cloud Model Armor Responsible AI Filters:** Model Armor screens both incoming user prompts and outgoing AI responses across Hate Speech, Harassment, Sexually Explicit, and Dangerous Content categories.  
    — *Provisioned in [`infra/terraform/ai_security.tf`](infra/terraform/ai_security.tf)*  
    — *Implemented in [`src/core/ai_security/armor.py`](src/core/ai_security/armor.py)*  
    — *Called in [`src/agent/graph.py`](src/agent/graph.py)*
  - **Vertex AI Gemini Harm-Blocking Thresholds:** Every Gemini model call is configured with explicit harm-blocking thresholds (`GEMINI_SAFETY_THRESHOLD`) across all four safety categories and post-generation safety block detection.  
    — *Implemented in [`src/core/ai_security/safety.py`](src/core/ai_security/safety.py)*  
    — *Called in [`src/agent/graph.py`](src/agent/graph.py)*
  - **Polite Refusal Fallback Messages:** Whenever Model Armor or Gemini blocks a prompt or halts generation due to content safety rules, the backend replaces the blocked output with a clear, polite refusal explanation so users never see a blank message bubble.  
    — *Implemented in [`src/core/ai_security/safety.py`](src/core/ai_security/safety.py)*  
    — *Called in [`src/agent/graph.py`](src/agent/graph.py)*

---

### 5. Compliance & Governance

Enterprise governance controls ensure that all data and AI calls stay within approved single-region boundaries, every layer of a request is auditable without exposing personal data, users have self-service privacy controls and AI transparency badges, and operational policies are enforced automatically:

- **5.1 Data Residency & Single-Region Pinning**
  - **Live Organization Policy Verification:** Allowed geographic regions are governed by the project's Google Cloud Resource Location Restriction Organization Policy (`constraints/gcp.resourceLocations`). Before any deployment runs, an automated check queries the live Organization Policy on the project and aborts if no location allowlist is active or if the target region is not permitted.  
    — *Implemented in [`infra/scripts/check_region_policy.py`](infra/scripts/check_region_policy.py)*  
    — *Called in [`Makefile`](Makefile)*
  - **Infrastructure Single-Region Validation:** Terraform validates that the configured deployment location is a concrete single region (such as `us-central1` or `europe-west1`)—rejecting multi-region or global locations—and enforces regional preconditions on storage buckets, security templates, and private endpoints.  
    — *Provisioned in [`infra/terraform/variables.tf`](infra/terraform/variables.tf), [`infra/terraform/storage.tf`](infra/terraform/storage.tf), [`infra/terraform/ai_security.tf`](infra/terraform/ai_security.tf), [`infra/terraform/network.tf`](infra/terraform/network.tf) & [`infra/terraform/cloud_run.tf`](infra/terraform/cloud_run.tf)*
  - **Backend Regional Endpoint Binding:** At runtime, the Python backend validates that the configured location is a single region and explicitly binds all Vertex AI chat models, Vertex AI embedding models, and Model Armor clients to that regional endpoint.  
    — *Implemented in [`src/core/compliance/residency.py`](src/core/compliance/residency.py)*  
    — *Called in [`src/core/storage/chroma.py`](src/core/storage/chroma.py), [`src/agent/graph.py`](src/agent/graph.py) & [`src/core/ai_security/armor.py`](src/core/ai_security/armor.py)*

- **5.2 Privacy-Safe Audit Logging & GCP Data Access Logs**
  - **PII-Free Application Audit Stream:** Backend services publish structured JSON audit records to a dedicated audit stream (`interactiverag-audit`) in Google Cloud Logging for authentication failures, document uploads, searches, agent chats, collection changes, privacy requests, rate-limit blocks, and safety filter actions.  
    — *Implemented in [`src/core/compliance/audit.py`](src/core/compliance/audit.py)*  
    — *Called in [`src/core/auth.py`](src/core/auth.py), [`src/ingest/routes.py`](src/ingest/routes.py), [`src/query/routes.py`](src/query/routes.py), [`src/agent/routes.py`](src/agent/routes.py), [`src/agent/service.py`](src/agent/service.py), [`src/manage/collections.py`](src/manage/collections.py) & [`src/manage/compliance.py`](src/manage/compliance.py)*
  - **One-Way SHA-256 Query Hashing & Log Scrubbing Filter:** Neither compliance audit logs nor operational logs ever record raw user questions or document text; user prompts are converted into one-way cryptographic SHA-256 hashes and character counts before logging, backed by an automatic log scrubbing filter that intercepts and hashes any accidental raw query field before output.  
    — *Implemented in [`src/core/compliance/audit.py`](src/core/compliance/audit.py), [`src/core/observability/telemetry.py`](src/core/observability/telemetry.py) & [`src/core/observability/logging.py`](src/core/observability/logging.py)*  
    — *Called in [`src/core/logger.py`](src/core/logger.py)*
  - **Disabled Third-Party Vector Database Telemetry:** Built-in usage telemetry inside the embedded ChromaDB engine is explicitly turned off (`ANONYMIZED_TELEMETRY=FALSE`) across all environments so no usage beacons leave the regional boundary.  
    — *Provisioned in [`infra/terraform/cloud_run.tf`](infra/terraform/cloud_run.tf)*  
    — *Implemented in [`src/core/config.py`](src/core/config.py)*
  - **Google Cloud Data Access Audit Logs & 365-Day Regional Archive:** Terraform enables Google Cloud Data Access Audit Logs (`ADMIN_READ`, `DATA_READ`, and `DATA_WRITE`) for Cloud Storage, Vertex AI, Cloud DLP, and Model Armor alongside Load Balancer HTTP access logs, routing them together with application audit events through a dedicated sink into the 365-day regional compliance log bucket.  
    — *Provisioned in [`infra/terraform/compliance.tf`](infra/terraform/compliance.tf) & [`infra/terraform/load_balancer.tf`](infra/terraform/load_balancer.tf)*

- **5.3 Regulatory Compliance (GDPR Self-Service Rights & EU AI Act Art. 50 Provenance)**
  - **GDPR Right of Access (Self-Service Data Inventory):** Authenticated users can view a complete inventory of their stored collections, chunk counts, uploaded PDF filenames, live and archived Cloud Storage object counts, total storage bytes, and active chat threads via `GET /compliance/my-data` or the "My Data & Privacy" modal in the web interface.  
    — *Implemented in [`src/core/compliance/dsar.py`](src/core/compliance/dsar.py)*  
    — *Called in [`src/manage/compliance.py`](src/manage/compliance.py) & [`frontend/src/App.jsx`](frontend/src/App.jsx)*
  - **GDPR Right to Erasure (One-Click Full Account Wipe):** Users can permanently erase all of their data in one action via `DELETE /compliance/my-data`—deleting every live and archived object version in Cloud Storage, wiping local vector and upload caches, and clearing all conversation history from agent memory.  
    — *Implemented in [`src/core/compliance/dsar.py`](src/core/compliance/dsar.py)*  
    — *Called in [`src/manage/compliance.py`](src/manage/compliance.py) & [`frontend/src/App.jsx`](frontend/src/App.jsx)*
  - **EU AI Act Art. 50 AI Generation, Model & Region Transparency Badge:** Every agent response includes structured provenance metadata and renders a visual badge in the chat interface showing whether the reply was AI-generated or stopped by a guardrail, which exact Gemini model and Google Cloud region produced it, and the correlating request ID.  
    — *Implemented in [`src/core/compliance/provenance.py`](src/core/compliance/provenance.py) & [`src/core/compliance/state.py`](src/core/compliance/state.py)*  
    — *Called in [`src/agent/schemas.py`](src/agent/schemas.py), [`src/agent/routes.py`](src/agent/routes.py), [`src/agent/service.py`](src/agent/service.py) & [`frontend/src/App.jsx`](frontend/src/App.jsx)*
  - **EU AI Act Art. 50 Citation Grounding Badge:** Every agent response reports whether document grounding was enforced, whether the answer is backed by inline `[filename.pdf, p.X]` citations, and how many document passages were retrieved.  
    — *Implemented in [`src/core/compliance/provenance.py`](src/core/compliance/provenance.py) & [`src/core/compliance/state.py`](src/core/compliance/state.py)*  
    — *Called in [`src/agent/schemas.py`](src/agent/schemas.py), [`src/agent/routes.py`](src/agent/routes.py), [`src/agent/service.py`](src/agent/service.py) & [`frontend/src/App.jsx`](frontend/src/App.jsx)*
  - **EU AI Act Art. 50 PII Redaction & Safety Filter Badge:** Every agent response reports whether sensitive personal data was detected and redacted by Cloud DLP, whether Model Armor screening was active, which security filters ran, and whether any safety guardrails were triggered.  
    — *Implemented in [`src/core/compliance/provenance.py`](src/core/compliance/provenance.py) & [`src/core/compliance/state.py`](src/core/compliance/state.py)*  
    — *Called in [`src/agent/schemas.py`](src/agent/schemas.py), [`src/agent/routes.py`](src/agent/routes.py), [`src/agent/service.py`](src/agent/service.py) & [`frontend/src/App.jsx`](frontend/src/App.jsx)*

- **5.4 Policy Enforcement (Fail-Closed Screening, Rate Limits & Quotas)**
  - **Fail-Closed Security Screening in Production:** In production deployments (`MODEL_ARMOR_FAIL_CLOSED=true`), if Google Cloud Model Armor or Cloud DLP is unreachable, misconfigured, or times out during pre-flight verification, prompt screening, chunk redaction, or output filtering, the backend immediately halts processing, logs a high-severity audit event, and returns an `HTTP 503` service-unavailable error so unscreened content is never exposed.  
    — *Provisioned in [`infra/terraform/cloud_run.tf`](infra/terraform/cloud_run.tf)*  
    — *Implemented in [`src/core/ai_security/armor.py`](src/core/ai_security/armor.py)*  
    — *Called in [`src/ingest/routes.py`](src/ingest/routes.py), [`src/query/routes.py`](src/query/routes.py), [`src/query/service.py`](src/query/service.py), [`src/agent/routes.py`](src/agent/routes.py) & [`src/agent/service.py`](src/agent/service.py)*
  - **Per-User Sliding-Window Rate Limiters with `Retry-After` Guidance:** Sliding-window rate limiters protect each user account on document uploads (10 per minute by default) and queries (30 per minute by default). When exceeded, the limiter returns `HTTP 429` with an exact `Retry-After` wait duration without counting the rejected attempt against the user's window.  
    — *Provisioned in [`infra/terraform/cloud_run.tf`](infra/terraform/cloud_run.tf)*  
    — *Implemented in [`src/core/compliance/policy.py`](src/core/compliance/policy.py)*  
    — *Called in [`src/ingest/upload_parser.py`](src/ingest/upload_parser.py), [`src/query/routes.py`](src/query/routes.py) & [`src/agent/routes.py`](src/agent/routes.py)*
  - **Per-User Collection Count Quotas:** Each user is capped at a maximum number of collections (20 by default), blocking creation of new collections past the limit while still allowing document uploads into existing collections.  
    — *Provisioned in [`infra/terraform/cloud_run.tf`](infra/terraform/cloud_run.tf)*  
    — *Implemented in [`src/core/compliance/policy.py`](src/core/compliance/policy.py)*  
    — *Called in [`src/ingest/upload_parser.py`](src/ingest/upload_parser.py)*

---

## VI/ [RELIABILITY & RESILIENCE]

### 1. Observability

While compliance audit logging records *who* performed *what* action and *when* for regulatory accountability, the observability layer gives operators real-time visibility into *how fast*, *at what token cost*, and *with what retrieval and grounding quality* every request executed:

- **1.1 Structured Logging & Request Correlation**
  - **Dual-Mode Application Logging:** When running in the cloud, backend services emit single-line JSON log records containing severity levels, timestamps, source file locations, and trace identifiers so Google Cloud Logging can index and filter them automatically—while preserving readable color-coded terminal output during local development.  
    — *Provisioned in [`infra/terraform/cloud_run.tf`](infra/terraform/cloud_run.tf)*  
    — *Implemented in [`src/core/observability/logging.py`](src/core/observability/logging.py)*  
    — *Called in [`src/core/logger.py`](src/core/logger.py)*
  - **End-to-End Request & Trace Correlation Context:** A correlation middleware assigns or forwards a unique `X-Request-ID` on every call, parses incoming Google Cloud `X-Cloud-Trace-Context` headers, and automatically enriches every operational and audit log entry with the active request ID, trace ID, span ID, service name, and authenticated user ID.  
    — *Implemented in [`src/core/context.py`](src/core/context.py)*  
    — *Called in [`src/core/app_factory.py`](src/core/app_factory.py), [`src/core/observability/logging.py`](src/core/observability/logging.py) & [`src/core/compliance/audit.py`](src/core/compliance/audit.py)*

- **1.2 Cloud Monitoring Metrics, Dashboards & Automated Alerting**
  - **Zero-Overhead Log-Based Metrics:** Instead of making extra network calls to a monitoring API during user requests, Google Cloud's Log Router automatically extracts 10 custom performance and quality metrics directly from structured telemetry logs—tracking end-to-end turn latency, time-to-first-token, language model versus retrieval stage latency, input/output/total token consumption, retrieval similarity scores, ungrounded answer counts, and ingested chunk throughput with zero request-path overhead.  
    — *Provisioned in [`infra/terraform/observability.tf`](infra/terraform/observability.tf)*  
    — *Called in [`src/core/observability/telemetry.py`](src/core/observability/telemetry.py)*
  - **Unified AI Quality, Latency, Token & Infrastructure Scaling Dashboard:** Terraform provisions a Google Cloud Monitoring dashboard charting median and 95th-percentile response latency, time-to-first-token, Gemini reasoning versus vector search stage durations, token consumption by model, retrieval similarity score distributions, ungrounded or blocked response rates, document ingestion throughput, active vs. idle container instances, concurrent requests per instance, and container CPU and memory utilization.  
    — *Provisioned in [`infra/terraform/observability.tf`](infra/terraform/observability.tf)*
  - **Automated Tail-Latency, Memory Saturation & Error Alerts:** Cloud Monitoring alert policies automatically notify operators whenever 95th-percentile agent response latency exceeds 15 seconds over 5 minutes, container memory utilization exceeds 85% over 3 minutes, or server 5xx error rates exceed 0.05 requests per second over 2 minutes.  
    — *Provisioned in [`infra/terraform/observability.tf`](infra/terraform/observability.tf)*

- **1.3 Distributed Tracing & Stage-Level Latency Breakdown**
  - **Hierarchical Per-Stage Execution Spans:** Every major step of a request—including Cloud Storage synchronization, PDF page splitting, Vertex AI embedding generation, Model Armor input screening, each Gemini reasoning step, ChromaDB similarity search, and output safety screening—is timed as an individual child span linked to the parent request trace so operators can pinpoint exactly which stage dominates latency.  
    — *Implemented in [`src/core/observability/tracing.py`](src/core/observability/tracing.py)*  
    — *Called in [`src/core/storage/gcs_sync.py`](src/core/storage/gcs_sync.py), [`src/ingest/splitter.py`](src/ingest/splitter.py), [`src/ingest/service.py`](src/ingest/service.py), [`src/query/service.py`](src/query/service.py), [`src/agent/graph.py`](src/agent/graph.py) & [`src/agent/tools.py`](src/agent/tools.py)*
  - **Cloud Trace Waterfall Export:** In cloud deployments, completed execution spans are exported to Google Cloud Trace so operators can inspect end-to-end request waterfall timelines alongside correlated application logs.  
    — *Provisioned in [`infra/terraform/cloud_run.tf`](infra/terraform/cloud_run.tf) & [`infra/terraform/iam.tf`](infra/terraform/iam.tf)*  
    — *Implemented in [`src/core/observability/tracing.py`](src/core/observability/tracing.py)*  
    — *Called in [`src/core/app_factory.py`](src/core/app_factory.py)*

- **1.4 AI-Specific Monitoring (Latency, Token Usage & Retrieval Quality)**
  - **Turn Latency, Stage Breakdown & Time-to-First-Token:** Every completed ingestion, similarity search, and agent conversation turn emits a structured telemetry summary recording total wall-clock duration, a millisecond breakdown across internal pipeline stages, and—for live streaming chats—the Time-to-First-Token (how quickly the user sees the first streamed event).  
    — *Implemented in [`src/core/observability/telemetry.py`](src/core/observability/telemetry.py) & [`src/core/observability/state.py`](src/core/observability/state.py)*  
    — *Called in [`src/ingest/routes.py`](src/ingest/routes.py), [`src/query/routes.py`](src/query/routes.py), [`src/agent/routes.py`](src/agent/routes.py) & [`src/agent/service.py`](src/agent/service.py)*
  - **Multi-Step Gemini Token Accounting:** Because an agent turn may invoke Gemini multiple times across iterative tool-calling steps, the backend extracts prompt, completion, and total token counts from each model call and aggregates total token usage and model call counts across the entire conversation turn for accurate cost tracking.  
    — *Implemented in [`src/core/observability/telemetry.py`](src/core/observability/telemetry.py)*  
    — *Called in [`src/agent/graph.py`](src/agent/graph.py)*
  - **Zero-Cost Deterministic Retrieval & Grounding Quality Signals:** Every search and agent turn computes normalized vector similarity scores (top and average match quality), the number of retriever tool calls and chunks fetched, the number of inline `[filename.pdf, p.X]` citations produced, and whether the final answer was properly grounded directly in Python—providing continuous retrieval quality signals without paying for extra "LLM-as-a-judge" API calls.  
    — *Implemented in [`src/core/observability/telemetry.py`](src/core/observability/telemetry.py)*  
    — *Called in [`src/agent/tools.py`](src/agent/tools.py), [`src/query/service.py`](src/query/service.py), [`src/agent/routes.py`](src/agent/routes.py) & [`src/agent/service.py`](src/agent/service.py)*

---

### 2. Graceful Degradation

The platform is engineered to handle partial component failures, search edge cases, lock contention, and runaway agent loops predictably across three resilience pillars:

- **2.1 Fallback Strategies**
  - **Hybrid Search to Dense Semantic Search Fallback:** During document retrieval, if the keyword search index encounters an unexpected error or a user's question shares zero exact keywords with the collection, search transparently falls back to normalized dense semantic vector similarity scores rather than failing the request.  
    — *Implemented in [`src/core/storage/chroma.py`](src/core/storage/chroma.py)*  
    — *Called in [`src/query/service.py`](src/query/service.py) & [`src/agent/tools.py`](src/agent/tools.py)*
  - **Bounded Agent Step-Limit Fallback:** When an ambiguous question causes the agent's search loop to reach its maximum allowed step count (6 steps by default) without converging, the backend catches the step-limit condition, records an audit event, and returns a polite message asking the user to narrow or rephrase their question instead of crashing with a server error.  
    — *Implemented in [`src/core/config.py`](src/core/config.py), [`src/agent/service.py`](src/agent/service.py) & [`src/core/ai_security/safety.py`](src/core/ai_security/safety.py)*  
    — *Called in [`src/agent/routes.py`](src/agent/routes.py)*
  - **Mid-Stream Server-Sent Events Error Recovery:** If a security block or unexpected error occurs while an answer is actively streaming to the browser, the stream generator catches the failure, logs an audit record, sends a structured error event carrying a safe user-facing message, and closes the stream cleanly so the chat interface never freezes on a broken connection.  
    — *Implemented in [`src/agent/service.py`](src/agent/service.py) & [`src/core/ai_security/safety.py`](src/core/ai_security/safety.py)*  
    — *Called in [`src/agent/routes.py`](src/agent/routes.py)*
  - **Cloud Logging Standard-Output Fallback:** When the audit or telemetry publisher cannot reach the Google Cloud Logging API directly, it catches the network error and falls back to writing a single-line structured JSON record to standard output so Cloud Run's built-in log collector still captures the event.  
    — *Implemented in [`src/core/compliance/audit.py`](src/core/compliance/audit.py) & [`src/core/observability/telemetry.py`](src/core/observability/telemetry.py)*
  - **Cloud Trace Outage Isolation:** Optional Cloud Trace export errors are caught and isolated in the background so a tracing service outage can never interrupt live user traffic.  
    — *Implemented in [`src/core/observability/tracing.py`](src/core/observability/tracing.py)*
  - **Collection Embedding Model & Safety Threshold Configuration Fallbacks:** If an older collection lacks explicit embedding model metadata, the backend automatically infers the embedding model from the stored vector dimensions or falls back to the primary approved embedding model; similarly, unrecognized safety threshold strings automatically fall back to the strictest harm-blocking level.  
    — *Implemented in [`src/core/storage/chroma.py`](src/core/storage/chroma.py) & [`src/core/ai_security/safety.py`](src/core/ai_security/safety.py)*

- **2.2 Retry Policies**
  - **Exponential Backoff with Random Jitter on Distributed Storage Locks:** When two concurrent write requests for the same user contend for the user's distributed vector database lock in Cloud Storage, the waiting container retries lock acquisition using exponential backoff (starting at 0.25 seconds and scaling up to 2 seconds) plus random jitter to prevent synchronized retry collisions across containers.  
    — *Implemented in [`src/core/storage/locks.py`](src/core/storage/locks.py)*  
    — *Called in [`src/core/storage/gcs_sync.py`](src/core/storage/gcs_sync.py)*

- **2.3 Timeout Handling & Stale-Lock Crash Recovery**
  - **Bounded Distributed Lock Wait Timeout:** Distributed write lock acquisition enforces a strict timeout (60 seconds by default). If another active upload holds the user's lock past the deadline, the waiting request returns a clean `HTTP 409` conflict error rather than hanging worker threads indefinitely.  
    — *Provisioned in [`infra/terraform/cloud_run.tf`](infra/terraform/cloud_run.tf) & [`infra/terraform/variables.tf`](infra/terraform/variables.tf)*  
    — *Implemented in [`src/core/storage/locks.py`](src/core/storage/locks.py)*  
    — *Called in [`src/ingest/routes.py`](src/ingest/routes.py), [`src/agent/routes.py`](src/agent/routes.py), [`src/manage/collections.py`](src/manage/collections.py) & [`src/manage/compliance.py`](src/manage/compliance.py)*
  - **Automatic Expired-Lock Crash Recovery:** Every distributed lock file in Cloud Storage includes an expiration timestamp (300 seconds by default). If a container crashes mid-write while holding a user's lock, subsequent write requests automatically detect that the lock has expired and atomically take over the stale lock so a user's vector database never becomes permanently deadlocked.  
    — *Provisioned in [`infra/terraform/cloud_run.tf`](infra/terraform/cloud_run.tf) & [`infra/terraform/variables.tf`](infra/terraform/variables.tf)*  
    — *Implemented in [`src/core/storage/locks.py`](src/core/storage/locks.py)*
  - **Workload-Tailored Cloud Run Request Timeouts:** Terraform configures request timeouts matched to each microservice's workload—300 seconds on the Ingestion and Agent services (to accommodate multi-PDF embedding and multi-step streaming reasoning) and 60 seconds on the static Frontend service.  
    — *Provisioned in [`infra/terraform/cloud_run.tf`](infra/terraform/cloud_run.tf)*

> **Note on Unimplemented Pillars:**
> - **Stateful Upstream Circuit Breakers & Automatic Cross-Model Failover (*Not Implemented — Relevant*):** While the platform enforces pre-flight fail-closed security short-circuiting, exponential backoff with jitter on distributed storage locks, bounded lock and request timeouts with automatic expired-lock crash recovery, and dense-only search fallback, a stateful circuit-breaker state machine (tracking consecutive upstream failure rates across requests) and automatic cross-model failover (such as automatically retrying a timed-out `gemini-2.5-pro` call with `gemini-2.5-flash`) are not yet implemented and remain relevant future resilience enhancements.

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

## ☁ Deploying to Google Cloud (Terraform + Cloud Build)

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
