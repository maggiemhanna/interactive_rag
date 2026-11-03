# Scalability & Elasticity Plan (Horizontal & Vertical Scaling, Autoscaling, Load Balancing, API Throughput & Scalable Inference)

This document outlines the architecture and implementation plan for the sixth architectural layer of **InteractiveRAG**: **Scalability & Elasticity (Performance & Cost Optimization)**.

While earlier layers establish serverless cloud deployment, private zero-internet networking, AI safety guardrails, regulatory compliance, and real-time observability, this layer ensures that the platform scales smoothly under concurrent multi-user load while keeping cloud infrastructure and AI inference costs strictly bounded. Because several foundational scaling mechanisms were built into the earlier layers and additional optimizations were introduced in this layer, this document serves both as a complete reference for the platform's existing scalability capabilities and as the implementation plan for the new elasticity enhancements. It is organized around six pillars:

1. **Horizontal Scaling** — Scales **out** and **in** by adding or removing independent, stateless container instances as traffic fluctuates. Each user's vector database is stored in an isolated Cloud Storage prefix and synchronized on demand into local container memory using distributed write locks so multiple instances never corrupt shared state.
2. **Vertical Scaling** — Tunes the CPU, memory, and in-memory disk capacity of each individual container to match its specific workload—allocating heavy CPU and memory to document parsing and vector indexing while keeping interactive chat and static web server instances lean.
3. **Autoscaling Policies** — Automatically adjusts running container counts from zero when idle up to billing-safe maximum instance caps, driven by explicit per-service request concurrency limits, request-based CPU throttling, and real-time Cloud Monitoring saturation alerts.
4. **Load Balancing** — Routes all external HTTPS traffic through a single **Global External Application Load Balancer** to serverless backend endpoint groups with long-running streaming timeouts and strict internal-only container ingress.
5. **API Throughput Design** — Prevents heavy document uploads from slowing down interactive searches and chat turns by splitting the backend into four modular domain packages deployed across two specialized Cloud Run services, streaming agent workflow events in real time over Server-Sent Events (SSE), screening retrieved passages for sensitive data in parallel, and uploading only modified database files to Cloud Storage.
6. **Scalable Inference (vs. HPC/GPU Cluster Management)** — Delegates foundation model generation and vector embedding calculations to regional serverless **Google Cloud Vertex AI** endpoints with tiered model selection and batched embedding calls, achieving elastic AI inference without the fixed cost or operational complexity of managing dedicated GPU clusters.

---

## 1. Target Architecture (Before vs. After)

### Before
- **Single Service Sharing Heavy Uploads and Fast Queries:** Document uploads, direct vector searches, collection management, and privacy data requests were all served by the same backend container service. Because parsing PDFs, splitting text, generating embeddings, and building vector indexes are CPU-intensive operations, a single user uploading a large PDF froze the Python event loop on that container instance for several seconds—causing other users' searches and collection lookups on that instance to stall.
- **Default High Concurrency on Heavy Compute Containers:** Cloud Run services relied on default request concurrency settings (up to 80 simultaneous requests per instance). Allowing dozens of memory-heavy PDF uploads to land on a single container instance risked CPU contention and out-of-memory crashes.
- **Small Private Subnets Limiting Scale-Out:** Private VPC subnets were sized with narrow IP ranges, leaving limited internal IP addresses available when Cloud Run scaled out multiple container revisions during traffic spikes or rolling deployments.
- **Full Database Re-Uploads on Every Change:** Whenever a user added a document to a collection or deleted a collection, the backend re-uploaded every file in that user's local vector database directory back to Cloud Storage—even when most collection segment files had not changed.
- **Unbounded In-Memory Cache Growth & Missing Scaling Alerts:** Local vector database caches stored in Cloud Run's in-memory `/tmp` storage used fixed internal defaults rather than configurable environment quotas, and the observability dashboard lacked container instance scaling charts and memory saturation alerts.

### After
- **Four Modular Backend Packages Deployed Across Two Workload-Isolated Services:** The backend codebase is cleanly organized into four domain packages—**Ingestion**, **Management**, **Query**, and **Agent**—deployed across two specialized Cloud Run services:
  - A dedicated **Ingestion Service** handles only document uploads with low concurrency and generous CPU and memory.
  - A unified **Interactive Agent, Search & Management Service** handles direct vector searches, collection listing/deletion, self-service privacy requests, and multi-turn AI chat—sharing a single warm local vector database cache and rate limiter without ever being blocked by PDF uploads.
- **Tuned Concurrency, Vertical Memory & Subnet Capacity:** Each Cloud Run service enforces an explicit per-instance concurrency ceiling matched to its workload, the Ingestion service is provisioned with expanded memory for large PDF parsing and index building, and both backend private VPC subnets are sized as `/24` networks (251 usable IPs each) to support horizontal scale-out.
- **Incremental Cloud Storage Delta Sync & Configurable Cache Quotas:** Only new or modified vector database files (verified via MD5 checksum comparison) are uploaded to Cloud Storage after a write, while removed files are cleaned up. In-memory `/tmp` cache limits are explicitly configured on both backend services to automatically evict older inactive user caches before memory fills up.
- **Real-Time Scaling Visibility & Saturation Alerts:** The Cloud Monitoring dashboard tracks active vs. idle container instances, concurrent requests per instance, CPU utilization, and memory/`/tmp` utilization alongside automated alert policies for sustained high latency, memory saturation, and server error spikes.

---

## 2. Scope Boundaries (Already Implemented vs. Added)

Because several core scalability capabilities were already established in earlier architectural layers, the table below distinguishes what was **already in place** (and documented here) from what was **added in this plan**.

| Pillar | In Scope (Already Implemented) | In Scope (Added) |
| :--- | :--- | :--- |
| **1. Horizontal Scaling** | • Stateless Cloud Run Gen2 services that scale out and in automatically as traffic changes<br>• Per-user data partitioning in Cloud Storage (`users/{uid}/chroma_db/`) so different users never contend on the same files or locks<br>• Two-level concurrency control combining atomic Cloud Storage distributed write locks across container instances with re-entrant thread locks inside each container process<br>• Session affinity on the Agent service so sequential requests from the same user stay on the same warm instance | • `/24` private VPC subnets (`251` usable IPs each)<br>• Agent service max scale-out raised to `10` instances (`320` concurrent requests) |
| **2. Vertical Scaling** | • Per-service CPU and memory allocations matched to each service's role (Frontend, Ingestion, and Agent)<br>• Fast in-memory `/tmp` RAM disk (`tmpfs`) on Cloud Run Gen2 for sub-millisecond local SQLite and vector index reads<br>• Automatic Least-Recently-Used (LRU) cache eviction that removes the oldest inactive user directories in `/tmp` while skipping any directory currently in use | • Ingestion memory upgraded from `2 GiB` to `4 GiB` RAM (`2 vCPU`)<br>• Configurable `/tmp` cache quotas (`512 MiB` max usage, `256 MiB` min free RAM) |
| **3. Autoscaling Policies** | • Request-driven Cloud Run autoscaling with scale-to-zero (`min_instances = 0`) in development and warm instance support in production<br>• Request-based CPU billing (`cpu_idle = true`) that throttles CPU to near zero between requests so idle warm instances cost minimal compute<br>• Billing-safe maximum instance caps on all services to prevent runaway cloud spend | • Explicit concurrency caps (`4` on Ingestion, `32` on Agent, `80` on Frontend)<br>• 4 Cloud Monitoring scaling charts + 3 sustained saturation alerts |
| **4. Load Balancing** | • Single **Global External Application Load Balancer** with a reserved static IP, Google-managed SSL certificate, and automatic HTTP-to-HTTPS redirect<br>• Serverless Network Endpoint Groups (NEGs) bridging the global proxy to regional Cloud Run services<br>• 300-second backend timeouts for multi-file uploads and streaming chat, plus strict internal-only Cloud Run ingress | • Path routing updated so `/api/ingest/*` serves only `POST /ingest`, while `/api/agent/*` serves search, management, privacy, and chat |
| **5. API Throughput Design** | • Real-time Server-Sent Events (SSE) step streaming (`POST /agent/stream`) delivering each completed workflow event (`Human` $\rightarrow$ `AI` tool call $\rightarrow$ `Tool` passages $\rightarrow$ screened `AI` answer $\rightarrow$ `compliance` metadata) over a single connection<br>• Parallel retrieval-time privacy screening across up to `8` worker threads (each thread screening one retrieved passage concurrently via Model Armor + Cloud DLP)<br>• Per-user sliding-window rate limiters (`10` uploads/min, `30` queries/min) and strict payload ceilings (file size, file count, chunk count, and query length) | • 4 modular packages (`src/ingest`, `src/manage`, `src/query`, `src/agent`) across 2 Cloud Run services<br>• Incremental MD5 delta sync to Cloud Storage (uploads only modified files) |
| **6. Scalable Inference (vs. HPC/GPU Clusters)** | • Serverless regional **Vertex AI** endpoints for both Gemini language models and embeddings accessed over private Google networking<br>• Tiered model catalog (`gemini-2.5-flash` default, `gemini-2.5-pro` for deep reasoning, `gemini-2.5-flash-lite` for fast low-cost turns)<br>• Sequential batched embedding requests (`100` chunks per Vertex AI API call) and per-turn token usage telemetry | • Architectural documentation of serverless Vertex AI elasticity vs. self-hosted GPU clusters |

> **Out of Scope (Intentionally Excluded — HPC / Self-Managed GPU Clusters):**
> Provisioning and autoscaling dedicated GPU clusters—such as **Google Kubernetes Engine (GKE)** GPU node pools (NVIDIA L4, A100, or H100), **Slurm** HPC clusters, multi-node **vLLM / TGI** inference servers, or custom model training pipelines—is intentionally excluded. InteractiveRAG uses foundation models and embedding models via API rather than training custom weights. Running dedicated GPU nodes would introduce high minimum hourly costs (hundreds to thousands of dollars per month even when idle) and break our serverless scale-to-zero cost model. Instead, GPU scaling is delegated to **Google Cloud Vertex AI**, which elastically pools accelerator capacity across the region on a pay-per-token basis (with optional **Vertex AI Provisioned Throughput** available if reserved capacity is ever required).

---

## 3. Key Design Decisions (In Plain English)

### 3.1 Workload Isolation & API Throughput: Why One Upload No Longer Freezes Interactive Traffic

- **`[Added]` Splitting the Backend into Four Modular Packages Across Two Cloud Run Services:**
  - **The Problem (Head-of-Line Blocking):** Previously, document uploads (`POST /ingest`), vector searches (`POST /query`), collection listing/deletion (`/collections`), and privacy requests (`/compliance/my-data`) all ran inside the same FastAPI service. Because PDF parsing, text chunking, synchronous embedding calls, and vector index construction are CPU- and memory-intensive, a single upload blocked that container's event loop for 5 to 30 seconds. Any search or collection request that landed on the same container instance had to wait in line behind the PDF upload.
  - **Why We Chose Two Backend Cloud Run Services Instead of a Threadpool or a Third Service:**
    - Moving uploads into a background threadpool inside the same container would still leave heavy PDF parsing competing for the same Python Global Interpreter Lock (GIL), CPU cores, and in-memory `/tmp` RAM as interactive user queries.
    - Conversely, spinning up a third separate Cloud Run service just for `/query` and `/collections` would force the platform to maintain two separate copies of the user's vector database cache in `/tmp` (one on the query service and another on the agent service) and would split per-user query rate limiters across two services.
    - Instead, we organized the Python codebase into **four clean domain packages**—**Ingestion** (`src/ingest`), **Management** (`src/manage`), **Query** (`src/query`), and **Agent** (`src/agent`)—and deployed them across **two specialized Cloud Run services**:
      1. **Dedicated Ingestion Service (`ingest-api`):** Mounts **only** `src/ingest` (`POST /ingest` and `GET /health`). It is tuned for heavy batch compute (`2 vCPU`, `4 GiB RAM`, max `4` concurrent requests). Because no interactive endpoints exist on this service, synchronous PDF processing never blocks interactive users.
      2. **Unified Interactive Agent, Search & Management Service (`agent-api`):** Mounts `src/query` (`POST /query`), `src/manage` (`GET /collections`, `DELETE /collections/{name}`, `GET/DELETE /compliance/my-data`), and `src/agent` (`POST /agent`, `POST /agent/stream`).
  - **Benefits of Co-Locating Search, Management, Privacy & Chat on `agent-api`:**
    - **One Shared Warm Vector Cache:** When a user lists their collections, runs a direct vector search, and asks the AI agent a follow-up question, all three requests use the **exact same warm `/tmp` vector database cache** on the `agent-api` instance without downloading the database from Cloud Storage twice.
    - **One Unified Query Rate Limiter:** Direct `/query` searches and `/agent` chat turns share a single per-user sliding-window rate limiter in memory.
    - **Atomic Self-Service Privacy Inventory & Erasure:** Because collection management (`src/manage`) and conversation memory (`src/agent`) live in the same service, a single call to `GET /compliance/my-data` or `DELETE /compliance/my-data` inspects or permanently erases both the user's ChromaDB collections and their active chat memory threads at once.

- **`[Added]` Incremental MD5 Delta Synchronization to Cloud Storage:**
  - **The Problem:** Previously, every time a user uploaded a PDF or deleted a collection, the backend re-uploaded **every file** in that user's local `/tmp` ChromaDB directory back to Cloud Storage—even when 90% of existing vector index segment files were completely unchanged.
  - **The Solution:** Before uploading files at the end of a write operation, the backend now computes a fast MD5 checksum for each local file and compares it against the MD5 hash already stored on the remote Cloud Storage object:
    1. **Unchanged files are skipped completely**, saving network bandwidth, reducing Cloud Storage write operations, and shortening the time the user's distributed write lock is held.
    2. **Only new or modified files** (such as the updated SQLite catalog and new vector segment files) are uploaded.
    3. **Stale remote files** that were deleted locally (for example, after deleting a collection) are removed from Cloud Storage, and the remote `version.json` manifest is updated last to commit the snapshot.

- **`[Already Implemented]` Real-Time Step Event Streaming (`POST /agent/stream`):**
  - **Streaming Workflow Steps Rather Than Raw Unscreened Tokens:** When a user chats via `POST /agent/stream`, the backend streams **completed workflow step events** over a single Server-Sent Events (SSE) connection as each stage of the LangGraph agent finishes:
    1. **Human Message Event:** Emitted as soon as the **Input Guard** approves the user's prompt (recording sub-second Time-to-First-Event / `ttft_ms`).
    2. **AI Tool-Call Event:** Emitted when Gemini decides which search query to run against the collection.
    3. **Tool Result Event:** Emitted when the retriever returns the DLP-redacted document passages.
    4. **Final Screened AI Answer Event:** Emitted once the **Output Guard** has verified and redacted the final answer.
    5. **Compliance Provenance Event:** Emitted at the end of the turn with EU AI Act Article 50 transparency metadata.
  - **Why We Stream Complete Step Events Instead of Individual Words:** Because the final AI response must pass through **Model Armor and Cloud DLP** in the Output Guard to mask sensitive personal data (such as `[EMAIL_ADDRESS]` or `[PHONE_NUMBER]`) and block unsafe content *before* the user sees it. If raw tokens were streamed to the browser word-by-word before the sentence finished, sensitive data could appear on screen before the safety filter had a chance to redact it. Step-by-step SSE streaming gives the user live visual progress through each stage of the agent's reasoning while guaranteeing that every message reaching the browser has been fully screened.

- **`[Already Implemented]` Parallel Cloud DLP Privacy Screening on Retrieved Passages:**
  - **When It Runs:** Privacy screening of document text happens at **retrieval time** (when `POST /query` or the agent's search tool retrieves the top-$k$ matching passages from ChromaDB, such as 4 or 5 chunks), rather than during PDF upload.
  - **How Parallel Screening Works:** Each retrieved passage must be checked by **Model Armor + Cloud DLP** over the network. If a search retrieves 5 passages and we checked them one after another in a single thread, network latency would multiply by 5. Instead, the backend uses a pool of **up to 8 parallel worker threads** (`min(number_of_retrieved_chunks, 8)`), where **each worker thread screens one retrieved passage at the same time**. All retrieved passages for a search are therefore screened concurrently in roughly the time of a single network call.

- **`[Already Implemented]` Up-Front Payload & Rate Guardrails:**
  - Strict limits on file size (`20 MB`), files per upload (`10`), extracted chunks per upload (`1,000`), query length (`4,000` characters), and per-minute request rates (`10` uploads/min, `30` queries/min) prevent any single user or request from monopolizing service throughput.

---

### 3.2 Horizontal Scaling: Stateless Containers, Distributed Locking & Expanded Subnet Capacity

- **`[Already Implemented]` Stateless Containers with Per-User Cloud Storage Snapshots:**
  - Cloud Run containers can be started or stopped at any second. To ensure no data is ever lost when containers scale down, each user's vector database lives in its own isolated Cloud Storage folder (`users/{uid}/chroma_db/`).
  - When any container instance receives a request for a user, it checks a lightweight `version.json` marker in Cloud Storage. If the container already has the latest version in its local `/tmp` directory, it reuses the warm local files immediately; otherwise, it downloads the latest snapshot before running the query.
  - Because each user's data is completely separate, 100 different users can be served across 10 container instances in parallel with zero lock contention between users.

- **`[Already Implemented]` Two-Level Concurrency Control Across Replicas:**
  - **Cross-Container Distributed Write Locks:** If the same user triggers two write operations at the same time (for example, uploading PDFs from two browser tabs that land on two different Cloud Run instances), the backend acquires an atomic distributed lock file in Cloud Storage (`if_generation_match=0`). Only one container can create the lock; the second container waits and retries with jitter until the first write finishes and pushes its updated snapshot. Stale locks from crashed containers automatically expire after 90 seconds.
  - **In-Process Thread Locks:** Inside each container, a bounded Least-Recently-Used (LRU) pool of per-user re-entrant thread locks serializes concurrent threads accessing the same user's local `/tmp` directory.
  - **Session Affinity on `agent-api`:** Cloud Run session affinity is enabled on the Agent service so sequential requests from the same user's browser are preferentially routed to the same warm container instance—maximizing local `/tmp` vector cache hits and keeping in-memory chat history warm.

- **`[Added]` Expanded `/24` Private Subnets for Horizontal Scale-Out:**
  - All backend containers route 100% of their outbound traffic into our private VPC using serverless **Direct VPC Egress**, which assigns an internal subnet IP address to each running and scaling container instance.
  - Previously, the private subnets were sized as smaller CIDR blocks, which risked running out of internal IP addresses during traffic spikes or overlapping deployment revisions.
  - Both the Ingestion subnet (`10.10.1.0/24`) and the Agent subnet (`10.10.2.0/24`) are now provisioned as `/24` networks (**251 usable IP addresses per subnet**), providing ample capacity for horizontal scaling.

---

### 3.3 Vertical Scaling: Right-Sizing CPU, Memory & In-Memory Cache Eviction

- **`[Already Implemented]` Workload-Specific CPU & Memory Profiles:**
  - Rather than using a one-size-fits-all container size, each Cloud Run service is vertically sized for its specific task:
    - **Frontend Service (`1 vCPU`, `512 MiB RAM`):** Serves pre-built static React/Vite files via Nginx with a minimal memory footprint.
    - **Agent Service (`2 vCPU`, `2 GiB RAM`):** Spends most of its time waiting on network I/O (awaiting Gemini responses or calling Cloud DLP/Model Armor) while holding warm SQLite/Chroma caches in memory.
- **`[Added]` Upgraded Vertical Memory (`4 GiB RAM`) on the Ingestion Service:**
  - The Ingestion service (`2 vCPU`, **`4 GiB RAM`**) performs memory-intensive PDF parsing, text splitting, batch embedding array handling, and ChromaDB Rust HNSW graph index construction. Doubling its memory allocation from `2 GiB` to `4 GiB` prevents out-of-memory terminations when multiple users upload large, multi-page PDFs concurrently.

- **`[Already Implemented]` In-Memory `/tmp` Storage & Automatic LRU Cache Eviction:**
  - In Cloud Run Gen2, the `/tmp` directory is an **in-memory RAM disk (`tmpfs`)** backed directly by the container's memory limit. Reading ChromaDB's SQLite and vector index files from `/tmp` gives sub-millisecond local disk speed, but if too many users' databases accumulate in `/tmp`, the container could run out of RAM.
  - Before and after every storage operation, an automatic **Least-Recently-Used (LRU) cache evictor** checks `/tmp` usage and deletes the oldest inactive user directories (while never evicting a directory whose lock is currently in use by an active request).
- **`[Added]` Explicit Configurable `/tmp` Cache Quotas (`512 MiB` Max / `256 MiB` Free Headroom):**
  - Both `ingest-api` and `agent-api` now expose and enforce explicit environment-driven `/tmp` cache thresholds—capping total cached vector databases at **`512 MiB`** (`CHROMA_TMP_MAX_BYTES`) and requiring at least **`256 MiB`** of free container RAM headroom (`CHROMA_TMP_MIN_FREE_BYTES`)—guaranteeing that cached `/tmp` files never starve the Python process or HNSW index builder of memory.

---

### 3.4 Autoscaling Policies & Saturation Observability

- **`[Added]` Explicit Per-Service Request Concurrency Targets:**
  - By default, Cloud Run allows up to 80 concurrent requests on a single container instance before spinning up a second instance. While 80 concurrent requests works well for static web assets, routing 80 simultaneous PDF uploads to a single Python container would saturate its CPUs and exhaust its memory long before Cloud Run scaled out.
  - Each service now enforces an explicit per-instance concurrency ceiling tailored to its workload:
    - **Ingestion Service (`4` concurrent requests per instance):** Forces Cloud Run to spin up additional `ingest-api` instances early whenever more than 4 document uploads arrive at the same time.
    - **Agent Service (`32` concurrent requests per instance):** Safely multiplexes up to 32 I/O-bound chat streams, vector searches, and collection lookups on each `2 vCPU / 2 GiB` container before scaling out.
    - **Frontend Service (`80` concurrent requests per instance):** Allows Nginx to serve static bundles at high concurrency on a single lightweight container.

- **`[Already Implemented]` Scale-to-Zero Elasticity & Request-Driven CPU Throttling:**
  - **Scale-to-Zero (`min_instances = 0`):** In development and low-traffic environments, all three Cloud Run services scale down to zero instances when idle so compute cost drops to `$0/hour`. In production, `min_instances` can be raised (for example, `1` warm instance on `agent-api`) to eliminate cold-start latency.
  - **Request-Based CPU Billing (`cpu_idle = true`):** All services enable CPU throttling when idle, meaning Google Cloud only bills for CPU cycles while a container is actively handling an HTTP request.
  - **Billing-Safe Upper Bounds (`max_instances`):** Upper instance limits protect against runaway cloud bills during traffic spikes or denial-of-wallet attacks.
- **`[Added]` Balanced Maximum Instance Cap on the Agent Service (`10` Instances):**
  - The Ingestion service is capped at `5` instances (`5 × 4 = 20` simultaneous PDF uploads), the Frontend service at `2` instances (`2 × 80 = 160` simultaneous static requests), and the Agent service is scaled up to **`10` instances** (`10 × 32 = 320` simultaneous interactive chat and search requests).

- **`[Added]` Cloud Monitoring Scaling Dashboards & Sustained Saturation Alerts:**
  - The unified Cloud Monitoring dashboard is extended with four infrastructure scaling panels:
    1. **Active vs. Idle Container Instances by Service** (real-time horizontal scaling visibility).
    2. **Container CPU & Memory (`/tmp` `tmpfs`) Utilization (`p95`)** (vertical capacity visibility).
    3. **Max Concurrent Requests per Instance (`p95`)** (autoscaler concurrency pressure).
    4. **End-to-End Cloud Run Request Latencies by Service (`p50` / `p95`)**.
  - Three automated **Cloud Monitoring Alert Policies** notify operators when degraded conditions persist across a **sustained evaluation window** (so a single slow request or brief 1-minute cold-start spike never triggers a false alarm):
    1. **High Agent Turn Latency (`p95 > 15,000 ms` sustained for 5 consecutive minutes):** Every 1 minute, Cloud Monitoring calculates the **95th percentile (`p95`) response time** across agent chat turns—meaning the duration within which 95% of turns finished (ignoring the slowest 5% of outliers). Our target goal (Service Level Objective, or SLO) is `15,000 ms` (`15 seconds`). If the `p95` latency stays above 15 seconds continuously for **5 minutes in a row**, the alert fires.
    2. **Container + `/tmp` RAM-Disk Memory Saturation (`p95 > 85%` sustained for 3 consecutive minutes):** Fires if the 95th percentile memory utilization of a service stays above **85% of its container RAM limit** continuously for **3 minutes in a row**, warning operators of vertical memory pressure before an out-of-memory crash occurs.
    3. **Elevated HTTP `5xx` Server Error Rate (`> 0.05 req/s` sustained for 2 consecutive minutes):** Fires if server-side `5xx` errors exceed `0.05` requests per second (`3` errors per minute) continuously for **2 minutes in a row**.

---

### 3.5 Load Balancing & Request Routing

- **`[Already Implemented]` Single Global HTTPS Front Door with Serverless Backends:**
  - A **Global External Application Load Balancer** terminates client TLS connections at Google's edge using a Google-managed SSL certificate, automatically redirects HTTP (`:80`) to HTTPS (`:443`), and forwards traffic to regional Cloud Run services through Serverless Network Endpoint Groups (NEGs).
  - Both backend services configure a `300-second` Load Balancer timeout so multi-file PDF uploads and long Server-Sent Events (SSE) chat streams are never cut off prematurely.
  - All Cloud Run services enforce `INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER`, blocking direct internet access to `*.run.app` URLs.
- **`[Added]` Workload-Isolated Path Routing Across `ingest-api` and `agent-api`:**
  - The Load Balancer URL map and frontend API client route traffic cleanly by workload:
    - `/api/ingest/*` $\rightarrow$ **`ingest-api`** (`POST /ingest`, `GET /health`).
    - `/api/agent/*` $\rightarrow$ **`agent-api`** (`POST /query`, `GET /collections`, `DELETE /collections/{name}`, `GET/DELETE /compliance/my-data`, `POST /agent`, `POST /agent/stream`, `GET /health`).
    - `/*` $\rightarrow$ **`frontend`** (React web application).

---

### 3.6 Scalable Inference vs. HPC/GPU Cluster Scaling

- **`[Already Implemented]` Regional Serverless Vertex AI Endpoints Over Private Networking:**
  - Instead of provisioning fixed GPU virtual machines, all LLM reasoning calls and vector embedding calls are sent to regional **Google Cloud Vertex AI** endpoints (`us-central1` or configured single region) over **Private Google Access**. Vertex AI automatically distributes inference across Google's regional TPU/GPU fleet and bills strictly per input/output token.
- **`[Already Implemented]` Multi-Model Tiering for Latency & Cost Efficiency:**
  - Callers and the frontend UI can select from three approved regional Gemini models depending on the complexity of the task:
    - **`gemini-2.5-flash` (Default):** Best balance of low latency, high throughput, and low cost for everyday multi-step RAG turns.
    - **`gemini-2.5-pro`:** Higher reasoning capacity for complex multi-document synthesis.
    - **`gemini-2.5-flash-lite`:** Ultra-fast, lowest-cost model for simple factual lookups.
- **`[Already Implemented]` Sequential Batched Embedding Indexing (`100` Chunks per Batch):**
  - **What "Batched Embedding Indexing" Means:** When an uploaded PDF is split into, for example, `250` text chunks, the backend does **not** make 250 separate HTTP calls (one chunk per call) to Vertex AI's embedding API, nor does it send all `1,000` allowed chunks in one giant request (which could exceed Vertex AI's per-request token limit). Instead, it groups the chunks into **batches of `100` chunks per API call** (so `250` chunks require just `3` API calls: `100` + `100` + `50`). Inside each call, Vertex AI vectorizes all 100 chunks in one round-trip and ChromaDB inserts those 100 vectors into the local index together.
  - **Sequential vs. Parallel Execution Across Batches:** Within a single upload request, those 100-chunk batches are processed **sequentially, one batch after another** (not in parallel threads). This is intentional for two reasons:
    1. Each batch writes directly into the user's local SQLite and Rust HNSW vector index on disk (`/tmp/chroma_db/{uid}`), which requires single-writer access per collection.
    2. Sending 100 chunks per call already lets Google's Vertex AI backend compute those 100 embeddings in parallel on Google's accelerators, while sequential batching prevents a single large upload from bursting past regional Vertex AI rate quotas.

---

## 4. Step-by-Step Implementation Phases

### Phase 1: Four-Package Modular Backend Split & Two-Service Topology (`src/` & `frontend/`)
**Goal:** Isolate CPU-heavy PDF ingestion (`src/ingest`) onto `ingest-api` while consolidating interactive vector search (`src/query`), collection & privacy management (`src/manage`), and LangGraph agent chat (`src/agent`) onto `agent-api`.

- [x] **Create `src/manage/` (`collections.py`, `compliance.py`, `routes.py`, `__init__.py`):**
  - Define `collections_router` in `src/manage/collections.py` (`GET /collections` and `DELETE /collections/{collection_name}`).
  - Define `compliance_router` in `src/manage/compliance.py` (`GET /compliance/my-data` and `DELETE /compliance/my-data`), inspecting and erasing both ChromaDB storage and in-memory LangGraph conversation checkpoints (`list_user_memory_threads`, `clear_user_memory`).
  - Combine both routers into `manage_router` in `src/manage/routes.py`.
- [x] **Create `src/query/` (`schemas.py`, `service.py`, `routes.py`, `__init__.py`):**
  - Define `QueryRequest` in `src/query/schemas.py`, `search_collection()` in `src/query/service.py`, and `query_router` (`POST /query`) in `src/query/routes.py`.
- [x] **Create `src/ingest/` (`schemas.py`, `splitter.py`, `upload_parser.py`, `service.py`, `routes.py`, `app.py`, `__main__.py`, `__init__.py`):**
  - Define `ingest_documents()` in `src/ingest/service.py` and `ingest_router` (`POST /ingest`) in `src/ingest/routes.py`.
  - Mount **only** `ingest_router` and `GET /health` in `src/ingest/app.py` (`ingest-api`, port `8000`).
- [x] **Mount Interactive Packages on `agent-api` (`src/agent/routes.py`, `src/agent/app.py`) & Update Frontend (`frontend/src/api.js`):**
  - Define `agent_router` (`POST /agent`, `POST /agent/stream`) in `src/agent/routes.py` and mount `agent_router`, `query_router`, and `manage_router` on `src/agent/app.py` (`agent-api`, port `8001`).
  - Remove legacy `src/ingestion/`, `src/core/storage/routes.py`, and `src/core/compliance/routes.py`.
  - Update `frontend/src/api.js` and `docker/Dockerfile.backend` so `POST /ingest` calls `ingest-api` and `/query`, `/collections`, `/compliance/my-data`, and `/agent*` call `agent-api`.

---

### Phase 2: Concurrency Tuning, Vertical Sizing, `/24` Subnets & Configurable `/tmp` Quotas (`infra/` & `src/core/`)
**Goal:** Align Cloud Run concurrency, memory limits, VPC subnet capacity, and `/tmp` RAM-disk cache eviction quotas with each service's workload profile.

- [x] **Explicit Per-Service Concurrency & Vertical Sizing (`infra/variables.tf`, `infra/cloud_run.tf`, `infra/terraform.tfvars.example`):**
  - Add `ingest_concurrency = 4`, `agent_concurrency = 32`, and `frontend_concurrency = 80` and wire `max_instance_request_concurrency` into `google_cloud_run_v2_service.ingest_api`, `agent_api`, and `frontend`.
  - Upgrade `ingest_memory` default to `"4Gi"` and `agent_max_instances` default to `10`.
- [x] **Expand Private VPC Subnets to `/24` (`infra/variables.tf`, `infra/network.tf`, `infra/terraform.tfvars.example`, `infra/terraform.tfvars`):**
  - Expand `ingest_subnet_cidr` to `"10.10.1.0/24"` and `agent_subnet_cidr` to `"10.10.2.0/24"` (`251` usable IPs per subnet).
- [x] **Configurable `/tmp` Cache Eviction Quotas (`src/core/config.py`, `src/core/storage/locks.py`, `infra/cloud_run.tf`):**
  - Add `get_chroma_tmp_max_bytes()` (default `536870912` / `512 MiB`) and `get_chroma_tmp_min_free_bytes()` (default `268435456` / `256 MiB`) in `src/core/config.py`.
  - Update `evict_tmp_cache()` in `src/core/storage/locks.py` to read those dynamic getters and set `CHROMA_TMP_MAX_BYTES` and `CHROMA_TMP_MIN_FREE_BYTES` on both `ingest_api` and `agent_api` in `infra/cloud_run.tf`.

---

### Phase 3: Incremental MD5 Cloud Storage Sync, Scaling Observability & Offline Test Suite (`src/core/storage/`, `infra/observability.tf`, `tests/`)
**Goal:** Eliminate redundant Cloud Storage file uploads during writes, add Cloud Monitoring scaling panels and saturation alerts, and verify all invariants offline.

- [x] **Incremental MD5 Delta Push (`src/core/storage/gcs_sync.py`):**
  - Implement `_compute_file_md5_b64(file_path)` and update `_push_user_dir_to_gcs(uid, user_dir)` to compare local file MD5 digests against existing Cloud Storage blob `md5_hash` values—uploading only new or modified files, deleting removed files, and recording `uploaded_files`, `skipped_files`, and `deleted_files` on the `storage.gcs_push` trace span.
- [x] **Cloud Monitoring Scaling Widgets & Alert Policies (`infra/observability.tf`, `infra/variables.tf`):**
  - Extend `google_monitoring_dashboard.interactiverag_observability` with Widgets 9–12 tracking active vs. idle container instances, `p95` CPU and memory (`/tmp`) utilization, `p95` concurrent requests per instance, and `p50`/`p95` end-to-end service request latencies.
  - Provision `google_monitoring_alert_policy.agent_high_latency_alert`, `cloud_run_memory_saturation_alert`, and `cloud_run_5xx_error_rate_alert`.
- [x] **Static Policy Gate & Automated Unit Tests (`scripts/check_static_policies.py` & `tests/scalability_elasticity.py`):**
  - Extend `scripts/check_static_policies.py` to verify concurrency variables, `/24` subnet masks, `4Gi` ingestion memory, `/tmp` quota environment variables, and Cloud Monitoring alert policies.
  - Add `tests/scalability_elasticity.py` to test the 4-package / 2-service split, shared rate limiter and cache behavior on `agent-api`, dynamic `/tmp` LRU cache eviction, incremental MD5 delta sync to Cloud Storage, and Terraform scaling policies offline.

---

## 5. Scalability & Elasticity Verification Playbook

### 1. Run the Offline Static Policy Gate & Unit Test Suite
Verify all infrastructure scaling policies and the dedicated Scalability & Elasticity unit test suite completely offline:

```bash
# 1. Run the static infrastructure & security policy gate
.venv/bin/python scripts/check_static_policies.py

# 2. Run the dedicated Scalability & Elasticity test suite
.venv/bin/python -m unittest tests.scalability_elasticity -v

# 3. Run all 7 repository test suites together
.venv/bin/python -m unittest \
  tests.auth \
  tests.storage \
  tests.multi_tenant_api \
  tests.ai_security \
  tests.compliance \
  tests.observability \
  tests.scalability_elasticity -v
```

### 2. Live API Verification Commands (Workload Isolation & Shared Cache)

```bash
# 1. Upload a PDF to the dedicated Ingestion Service (`ingest-api` via `/api/ingest`)
curl -s -X POST "${LB_URL}/api/ingest/ingest" \
  -H "Authorization: Bearer ${ID_TOKEN}" \
  -F "files=@./data/Stock_Market_Performance_2024.pdf" \
  -F "collection_name=market_reports" | jq .
# Expect: HTTP 200 OK with {"status": "success", "chunks_added": ..., "collection_name": "market_reports"}

# 2. Verify interactive endpoints (/collections, /query, /compliance/my-data, /agent) are served by `agent-api` (`/api/agent`)
curl -s "${LB_URL}/api/agent/collections" \
  -H "Authorization: Bearer ${ID_TOKEN}" | jq .

curl -s -X POST "${LB_URL}/api/agent/query" \
  -H "Authorization: Bearer ${ID_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"query": "What was the S&P 500 return?", "collection_name": "market_reports", "k": 4}' | jq .

curl -s "${LB_URL}/api/agent/compliance/my-data" \
  -H "Authorization: Bearer ${ID_TOKEN}" | jq .
```

### 3. Inspect Incremental Delta Sync & Scaling Telemetry in Google Cloud Console
Open **Google Cloud Console → Logging → Logs Explorer** or **Monitoring → Dashboards** and use these filters:

```text
# 1. Inspect incremental Cloud Storage delta sync metrics (uploaded_files vs. skipped_files) on ingestion
logName="projects/${PROJECT_ID}/logs/run.googleapis.com%2Fstdout"
AND jsonPayload.message:"Synced ChromaDB to GCS"

# 2. Compare stage latencies (gcs_sync_ms, chunking_ms, embedding_ms, retrieval_ms, llm_ms) across services
logName="projects/${PROJECT_ID}/logs/interactiverag-telemetry"
AND (jsonPayload.service="ingest-api" OR jsonPayload.service="agent-api")
```
