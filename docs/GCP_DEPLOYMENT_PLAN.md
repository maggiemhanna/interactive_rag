# Cloud Deployment & Multi-Tenant Persistence Plan (Cloud Run, Identity Platform, Cloud Storage & Terraform)

This document outlines the architecture and implementation plan for the first foundational layer of **InteractiveRAG**: **Cloud Deployment & Multi-Tenant Persistence**.

It transforms the application from a single-user local prototype into a serverless, multi-tenant cloud platform on **Google Cloud Platform (GCP)** where multiple users can sign in, upload documents, manage vector collections, and chat with an AI agent simultaneously without ever seeing or affecting another user's data. It is organized around four pillars:

1. **Serverless Microservice Architecture** — Packages and runs the React web interface, the Document Ingestion service, and the Agentic RAG service as independent, auto-scaling containers on **Google Cloud Run** that can scale down to zero when idle to minimize cost.
2. **Zero-Trust User Authentication** — Requires every user to sign in through **Google Cloud Identity Platform (Firebase Authentication)** using Google Sign-In or Email & Password. Every backend request is cryptographically verified so user identity is always determined by a trusted authentication token, never by client-supplied parameters.
3. **Multi-Tenant Data & Memory Isolation** — Gives each authenticated user an isolated workspace for their uploaded PDFs, vector database collections, and multi-turn conversation history. Two users can create collections or chat sessions with identical names without any overlap or cross-account visibility.
4. **Durable Cloud Storage Synchronization** — Combines the speed of local in-memory vector search with the durability of **Google Cloud Storage**. Each user's vector database is synchronized on demand between Cloud Storage and fast container memory using distributed locking and version tracking so data survives container restarts and stays consistent across services.

---

## 1. Target Architecture (Before vs. After)

### Before
- **Single-User Local Execution:** The frontend and backend services ran only on a developer's machine with local configuration files and manual startup commands.
- **No Authentication:** Backend APIs accepted anonymous requests without verifying who was calling them.
- **Shared Storage & Memory:** All uploaded documents, vector collections, and chat histories were stored in a single shared folder and shared memory space, meaning any user could read, overwrite, or delete another user's collections.
- **Ephemeral Container State:** Running a local embedded vector database directly inside a stateless cloud container would lose all indexed documents whenever containers scaled down or restarted.

### After
- **Serverless Cloud Deployment:** Three dedicated Cloud Run services (Frontend, Ingestion API, and Agent API) are provisioned reproducibly through Infrastructure as Code (Terraform) and automated container builds, with runtime configuration injected automatically at startup.
- **Verified Identity on Every Request:** Users sign in through the web interface to obtain a short-lived authentication token. Backend services verify this token on every API call before performing any work.
- **Strict Per-User Isolation:** Every user's vector collections, uploaded PDF archives, temporary processing folders, and conversation threads are separated into isolated per-user namespaces derived strictly from their verified account ID.
- **Durable, Coherent Vector Persistence:** User vector databases are stored durably in a private, versioned Cloud Storage bucket and synchronized into fast in-memory container storage on demand, protected by per-user distributed locks and version checks so changes made in the Ingestion service are immediately visible to the Agent service.

---

## 2. Scope Boundaries (What We Implement vs. Exclude)

To keep the deployment and persistence architecture cost-effective, fast, and reliable for serverless workloads, the table below summarizes what is implemented in this plan and what is intentionally left out of scope.

| Architecture Pillar | In Scope (Implemented) | Out of Scope (Intentionally Excluded) |
| :--- | :--- | :--- |
| **1. Serverless Cloud Infrastructure** | • Three auto-scaling **Cloud Run** services (`frontend`, `ingestion-api`, `agent-api`) with scale-to-zero support<br>• Reproducible **Terraform** modules for APIs, IAM service accounts, Artifact Registry, Cloud Storage, Identity Platform, and Cloud Run<br>• Single-pass runtime frontend configuration injected at container startup | • Always-on Kubernetes clusters (GKE) or dedicated virtual machines |
| **2. Authentication & Identity** | • **Google Cloud Identity Platform (Firebase Auth)** supporting Email/Password and Google OAuth Sign-In<br>• Mandatory bearer token verification on all collection, ingestion, query, and agent endpoints<br>• Local development authentication bypass strictly blocked when running in Cloud Run | • Custom username/password database management inside the application |
| **3. Multi-Tenant Isolation** | • Per-user storage prefixes in Cloud Storage and isolated per-user local cache directories<br>• Per-user namespacing of in-memory conversation threads so chat history never leaks across accounts<br>• Blocking arbitrary server-filesystem file path reads in cloud deployments | • Cross-user shared collections or team workspaces |
| **4. Vector Database Persistence** | • Per-user **ChromaDB** snapshots synchronized between versioned **Cloud Storage** and in-memory container storage<br>• Distributed per-user write locks (with stale-lock recovery) and lightweight version markers for cross-service cache coherence<br>• Automatic least-recently-used (LRU) local cache eviction when container memory fills up | • Always-on managed relational/vector database clusters (such as Cloud SQL or Vertex AI Vector Search endpoints) that incur continuous hourly charges when idle<br>• Mounting Cloud Storage directly via GCS FUSE for live SQLite writes (which risks database corruption due to lack of file-locking support) |

---

## 3. Key Design Decisions (In Plain English)

### 3.1 Fast In-Memory Vector Search Synchronized with Cloud Storage
- **Why Not Run SQLite Directly Over a Cloud Storage Mount:** ChromaDB stores its metadata in SQLite and its vector index in memory-mapped files. Object storage systems like Google Cloud Storage do not support the low-level file locks that SQLite requires. Running SQLite directly over a network storage mount across two separate microservices (Ingestion and Agent) can cause locked database errors, corrupted indexes, and stale reads.
- **On-Demand Per-User Synchronization:** Instead, each user's vector database is stored under a dedicated prefix in a private Cloud Storage bucket. When a user uploads documents or asks a question:
  1. The service checks a lightweight version file in Cloud Storage to see if the container's local in-memory copy is already up to date.
  2. For **read operations** (listing collections, searching, or chatting), if a newer version exists in Cloud Storage, the service refreshes its local copy in fast container memory (`/tmp`) before running the query.
  3. For **write operations** (ingesting PDFs or deleting a collection), the service acquires an atomic per-user lock in Cloud Storage, pulls the latest snapshot, performs the update at local memory speed, uploads only the changed files along with a new version marker, and releases the lock.
  4. When container memory runs low, an automatic cache-eviction routine removes the least-recently-used inactive user folders from local memory while their data remains safely stored in Cloud Storage.

### 3.2 Zero-Trust Multi-Tenant Isolation
- **Identity Derived Only from Verified Tokens:** Users sign in through Identity Platform in the browser, and every API request includes their signed authentication token. The backend verifies the token signature with Google's public keys and extracts the user's unique account ID.
- **Physical Separation of User Data:** The user's verified ID—and never a parameter sent in the request body or URL—determines which Cloud Storage prefix, local vector database directory, temporary upload folder, and conversation memory namespace the request can access. Even if two users name a collection `financial_reports` or use the same chat thread ID, their data lives in physically separate databases and memory keys.
- **Safe File Upload Handling:** In cloud deployments, the ingestion endpoint only accepts direct PDF file uploads (rejecting requests that reference local server file paths). Uploaded files are staged in an isolated per-request temporary directory, verified as valid PDFs, optionally archived to the user's private Cloud Storage folder, and automatically cleaned up from local disk as soon as ingestion finishes.

### 3.3 Build-Once Frontend with Runtime Cloud Configuration
- **Separating Image Builds from Cloud Provisioning:** Single-page web applications normally bake API URLs, project IDs, and Firebase API keys into static JavaScript files at `docker build` time (`make build-push`), which happens *before* Terraform (`make deploy`) has provisioned Identity Platform or knows the target project's generated Firebase API key.
- **Startup Configuration Injection:** Instead, the frontend container generates `/env-config.js` from Cloud Run environment variables the moment the container starts. This lets you build the frontend Docker image once and configure it at deploy time:
  - **API URLs (`INGESTION_API_URL` & `AGENT_API_URL`):** Set to relative Load Balancer paths (`"/api/ingest"` and `"/api/agent"`) in Cloud Run (overriding the local development defaults `http://localhost:8000` and `http://localhost:8001`).
  - **Firebase & Project Settings (`FIREBASE_API_KEY`, `FIREBASE_PROJECT_ID`, `FIREBASE_AUTH_DOMAIN`):** Dynamically populated by Terraform from the newly created Identity Platform resource and project variables during `make deploy`.
  - **Approved Model Catalogs (`ALLOWED_LLM_MODELS` & `ALLOWED_EMBEDDING_MODELS`):** Populated from `terraform.tfvars` (in Cloud Run via `/env-config.js`) or root `.env` (in local development via Vite) and mirrored by `GET /collections` so the frontend dropdowns always reflect the active model allowlists.

---

## 4. Step-by-Step Implementation Phases

### Phase 1: Containerization & Infrastructure as Code (`docker/`, `infra/`, `Makefile`)
**Goal:** Package the frontend and backend services into production container images and define all Google Cloud resources reproducibly in Terraform.

- [x] **Docker Packaging (`docker/` & `.dockerignore`):**
  - **`docker/Dockerfile.backend`:** Build a single unified Python image able to run either the Ingestion service (`src.ingest.app`) or the Agent service (`src.agent.app`).
  - **`docker/Dockerfile.frontend`, `docker/nginx.conf.template`, `docker/entrypoint-frontend.sh`:** Build the React application and serve it via Nginx, generating `/usr/share/nginx/html/env-config.js` (`window.__ENV__`) dynamically at container startup.
- [x] **Terraform Modules (`infra/`):**
  - **`infra/versions.tf`, `infra/variables.tf`, `infra/apis.tf`, `infra/outputs.tf`:** Define provider versions, input variables, required Google Cloud APIs, and stack outputs.
  - **`infra/storage.tf`:** Provision a private, versioned Cloud Storage bucket (`uniform_bucket_level_access = true`, `public_access_prevention = "enforced"`) with lifecycle rules that prune old archived object versions.
  - **`infra/iam.tf`:** Provision a least-privilege backend runtime service account (`backend_sa`) with access only to the persistence bucket and Vertex AI models.
  - **`infra/auth.tf`:** Provision Google Cloud Identity Platform (Email/Password + optional Google OAuth via Secret Manager) and register a Firebase Web App + API key.
  - **`infra/cloud_run.tf`:** Provision the three Cloud Run Gen2 services (`frontend`, `ingest_api`, `agent_api`) with startup/liveness probes, session affinity on the agent service, and auto-wired environment variables.
- [x] **Build & Deployment Automation (`Makefile` & `cloudbuild.yaml`):**
  - Provide `make bootstrap`, `make build-push`, `make plan`, `make deploy`, and `make outputs` commands supporting both local Terraform and Google Cloud Build execution.

---

### Phase 2: Backend Authentication (`src/core/auth.py` & `src/core/config.py`)
**Goal:** Verify user identity on every API request and prevent unauthorized or cross-tenant access.

- [x] **Firebase Admin Token Verification (`src/core/auth.py` & `src/core/config.py`):**
  - Initialize the Firebase Admin SDK using Application Default Credentials and the configured Google Cloud project.
  - Implement the `get_current_user` FastAPI dependency to extract and validate the `Authorization: Bearer <ID_TOKEN>` header via `firebase_admin.auth.verify_id_token()`, returning `HTTP 401 Unauthorized` on missing, expired, or invalid tokens.
  - Validate that every extracted user ID (`uid`) conforms to a strict safe character pattern (`^[a-zA-Z0-9_-]{1,128}$`) so it can never be used for directory traversal.
  - Support a local-development authentication bypass (`AUTH_DISABLED=true`, checked via `is_local_auth_disabled()` and `get_dev_user_id()` in `src/core/config.py`) that is automatically ignored whenever the service runs inside Cloud Run (`K_SERVICE` set).

---

### Phase 3: Per-User Cloud Storage ↔ ChromaDB Persistence (`src/core/storage/` & `src/manage/`)
**Goal:** Provide fast local ChromaDB reads and writes backed by durable, lock-protected per-user storage in Google Cloud Storage.

- [x] **Isolated Tenant Paths & Client Cache (`src/core/storage/paths.py`):**
  - Map each user `uid` to isolated local directories (`/tmp/chroma_db/{uid}` and `/tmp/uploads/{uid}`) and Cloud Storage prefixes (`users/{uid}/chroma_db/`, `users/{uid}/uploads/`, and `users/{uid}/locks/chroma.lock`).
  - Manage cached ChromaDB client instances with `release_client_cache()` so stale SQLite connections are cleanly closed before refreshing files from Cloud Storage.
- [x] **Cloud Storage Synchronization (`src/core/storage/gcs_sync.py`):**
  - Compare the remote `users/{uid}/chroma_db/version.json` marker against the local `/tmp/chroma_db/{uid}/version.json` file and download updated blobs only when a newer snapshot exists.
  - Upload modified local ChromaDB files, delete remote blobs that were removed locally (such as deleted collection folders), write an updated `version.json` marker, and optionally archive uploaded source PDFs under `users/{uid}/uploads/`.
- [x] **Distributed Locks & Local Cache Eviction (`src/core/storage/locks.py`):**
  - Implement `user_read_lock(uid)` and `user_write_lock(uid)` combining per-user in-process locks with an atomic Cloud Storage lock object (`if_generation_match=0` and automatic stale-lock TTL recovery).
  - Implement `evict_tmp_cache()` to remove the least-recently-used user cache directories from `/tmp` when free container memory drops below threshold.
- [x] **Collection Management Routes (`src/core/storage/chroma.py` & `src/manage/collections.py`):**
  - Expose authenticated `GET /collections` (returning collection names, chunk counts, each collection's bound `embedding_model`, and `allowed_llm_models` / `allowed_embedding_models`) and `DELETE /collections/{collection_name}` endpoints wired into `agent-api` via `src/manage/routes.py` and `src/core/app_factory.py`.

---

### Phase 4: Multi-Tenant Backend Hardening (`src/ingest/`, `src/query/`, `src/agent/`, `src/core/`)
**Goal:** Isolate every document upload, vector search, and agent conversation by authenticated user ID and harden server settings for production.

- [x] **Isolated Document Ingestion & Vector Search (`src/ingest/` & `src/query/`):**
  - Require `uid: str = Depends(get_current_user)` on `POST /ingest` (`src/ingest/routes.py`) and `POST /query` (`src/query/routes.py`).
  - Block server-filesystem `pdf_paths` ingestion when running in Cloud Run via `validate_server_pdf_paths()` (`src/core/ai_security/guardrails.py` & `src/ingest/upload_parser.py`).
  - Stage multipart PDF uploads in a per-request directory (`/tmp/uploads/{uid}/{request_uuid}`), archive validated PDFs to Cloud Storage, and guarantee cleanup in a `finally` block.
- [x] **Isolated Agent Chat & Conversation Memory (`src/agent/`):**
  - Require `uid: str = Depends(get_current_user)` on `POST /agent` and `POST /agent/stream` (`src/agent/routes.py`).
  - Namespace all in-memory conversation threads by user ID (`make_user_thread_id(uid, thread_id)` $\rightarrow$ `"{uid}:{thread_id}"` in `src/agent/memory.py`, `src/agent/routes.py`, and `src/agent/service.py`) so users can never read or continue another user's conversation thread.
- [x] **Production Server & Streaming Configuration (`src/core/app_factory.py` & `src/core/config.py`):**
  - Provide unauthenticated `GET /health` endpoints for Cloud Run health probes.
  - Enforce explicit `ALLOWED_ORIGINS` CORS policies in Cloud Run rather than wildcard origins.
  - Attach `Cache-Control: no-cache` and `X-Accel-Buffering: no` headers to Server-Sent Events (SSE) responses so streaming tokens flush immediately.

---

### Phase 5: Frontend Authentication & Runtime Configuration (`frontend/`)
**Goal:** Add sign-in flows to the React web application and attach authentication tokens to all API requests.

- [x] **Runtime Environment & Firebase Initialization (`frontend/index.html` & `frontend/src/firebase.js`):**
  - Load `/env-config.js` before the application bundle starts (with seamless fallback to local Vite environment variables during local development).
  - Initialize Firebase Authentication and provide an `authFetch()` wrapper that automatically attaches `Authorization: Bearer <ID_TOKEN>` to all backend REST and streaming requests.
- [x] **Authentication UI & Session Cleanup (`frontend/src/AuthGate.jsx`, `frontend/src/App.jsx`, `frontend/src/api.js`):**
  - Gate the dashboard behind a sign-in / sign-up screen supporting Email & Password and Google Sign-In.
  - Display the signed-in user's account in the header bar with a **Sign Out** button that clears all local collection and chat state upon sign-out or account switch.

---

## 5. Deployment & Multi-Tenant Verification Playbook

### 1. Reproducible Cloud Deployment Commands
Ensure the `gcloud` CLI is authenticated (`gcloud auth login` and `gcloud auth application-default login`), then run:

```bash
# 1. Create your Terraform variables file from the template and set your project_id & region
cp infra/terraform.tfvars.example infra/terraform.tfvars

# 2. Enable required GCP APIs, create the Terraform state bucket, and provision Artifact Registry
make bootstrap PROJECT_ID="${PROJECT_ID}" REGION="${REGION}"

# 3. Build and push linux/amd64 backend & frontend container images via Google Cloud Build
make build-push PROJECT_ID="${PROJECT_ID}" REGION="${REGION}"

# 4. Provision all infrastructure and deploy the Cloud Run services
make deploy PROJECT_ID="${PROJECT_ID}" REGION="${REGION}"

# 5. View the deployed application URL and resource identifiers
make outputs PROJECT_ID="${PROJECT_ID}" REGION="${REGION}"
```

### 2. Run the Automated Offline Test Suites
Verify authentication, distributed locking, Cloud Storage synchronization, and multi-tenant API isolation offline without requiring live cloud credentials:

```bash
# Run authentication, storage sync/locking, and multi-tenant isolation unit tests
.venv/bin/python -m unittest tests.auth tests.storage tests.multi_tenant_api -v
```

### 3. Live Multi-Tenant Verification Checks

```bash
# 1. Verify Unauthenticated Requests Are Rejected (HTTP 401 Unauthorized)
curl -i "${INGEST_URL}/collections"
curl -i -X POST "${AGENT_URL}/agent" \
  -H "Content-Type: application/json" \
  -d '{"query": "Hello", "collection_name": "test_col"}'
# Expect on both: HTTP/2 401 Unauthorized

# 2. Verify Strict Tenant Data & Conversation Isolation
# - Sign in as User A (TOKEN_A) and upload a PDF into collection `confidential_docs`:
curl -s -X POST "${INGEST_URL}/ingest" \
  -H "Authorization: Bearer ${TOKEN_A}" \
  -F "collection_name=confidential_docs" \
  -F "files=@sample.pdf;type=application/pdf" | jq .

# - Sign in as User B (TOKEN_B) and list collections:
curl -s "${INGEST_URL}/collections" \
  -H "Authorization: Bearer ${TOKEN_B}" | jq .
# Expect: {"collections": []} (User A's collection is completely invisible to User B)

# - Attempt to query or delete User A's collection using User B's token:
curl -i -X POST "${INGEST_URL}/query" \
  -H "Authorization: Bearer ${TOKEN_B}" \
  -H "Content-Type: application/json" \
  -d '{"query": "revenue", "collection_name": "confidential_docs"}'
curl -i -X DELETE "${INGEST_URL}/collections/confidential_docs" \
  -H "Authorization: Bearer ${TOKEN_B}"
# Expect on both: HTTP/2 404 Not Found (User A's collection remains untouched)

# 3. Verify Cross-Service Synchronization & Scale-to-Zero Durability
# - Immediately query `confidential_docs` on the Agent service as User A (proves `agent-api` pulls the latest `version.json` snapshot written by `ingestion-api`):
curl -s -X POST "${AGENT_URL}/agent" \
  -H "Authorization: Bearer ${TOKEN_A}" \
  -H "Content-Type: application/json" \
  -d '{"query": "Summarize the document.", "collection_name": "confidential_docs", "stream": false}' | jq .
```
