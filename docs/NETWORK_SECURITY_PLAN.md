# Network Security & Load Balancer Plan (Private VPC, Network Segmentation & Private Endpoints)

This document outlines the architecture and implementation plan for the second defense-in-depth layer of **InteractiveRAG**: **Network Security & Load Balancing**.

While the first layer establishes authentication and multi-tenant storage isolation, this layer locks down the network perimeter so backend services cannot be reached directly from the public internet and cannot make outbound connections to external websites. It is organized around three pillars:

1. **Private Virtual Network (Zero Outbound Internet)** — Places backend services inside a custom Virtual Private Cloud (VPC) with no route to the open internet. Even if an attacker or a malicious document attempted to trick a backend container into sending data to an outside server, the network itself blocks the connection.
2. **Network Segmentation & Single HTTPS Front Door** — Replaces direct internet access to individual containers with a single **Global External Application Load Balancer**. The Load Balancer acts as the only public entry point over HTTPS, while backend workloads are isolated in separate private subnets according to their role.
3. **Private Google Endpoints** — Routes all communication between the backend services and Google Cloud APIs (Cloud Storage, Vertex AI, Identity Platform, Cloud Logging, Cloud DLP, and Model Armor) exclusively through private internal Google tunnels and private DNS rather than the public internet.

---

## 1. Target Architecture (Before vs. After)

### Before
- **Multiple Public Service Endpoints:** The frontend, document ingestion service, and AI agent service were each directly reachable on the public internet through their own individual service URLs, requiring cross-origin (CORS) requests between separate domains.
- **Unrestricted Outbound Internet Access:** Backend containers used the default shared cloud network with full outbound internet connectivity.
- **No Workload Network Isolation:** Document parsing/ingestion and AI agent execution shared the same network boundary without subnet separation.

### After
- **Single HTTPS Entry Point:** Only the Global External HTTPS Load Balancer is reachable from the internet. It serves the web interface and routes API traffic internally by URL path, while direct internet requests to any individual Cloud Run service URL are automatically blocked at Google's network edge.
- **Zero Outbound Internet:** Backend services route all outbound traffic into a custom private VPC that has its default internet route removed, completely preventing data exfiltration to external hosts.
- **Segmented Subnets & Private Internal Tunnels:** The Document Ingestion service and the Agent service run in separate private subnets and reach Google Cloud storage, AI models, authentication keys, and security screening services strictly over Google's private internal network using Private Google Access and Regional Private Service Connect.

---

## 2. Scope Boundaries (What We Implement vs. Exclude)

To achieve strong network isolation without adding unnecessary fixed infrastructure costs (such as always-on NAT gateways or proxy VMs), the table below summarizes what is implemented in this architecture and what is intentionally left out of scope.

| Network Pillar | In Scope (Implemented) | Out of Scope (Intentionally Excluded) |
| :--- | :--- | :--- |
| **1. Private VPC Design** | • Custom VPC network with automatic subnet creation disabled and the default public internet route deleted<br>• Serverless **Direct VPC Egress** routing all outbound backend traffic through the private VPC ($0/month idle cost, no connector VMs)<br>• Third-party outbound telemetry disabled on backend containers | • Cloud NAT gateways or outbound internet proxy servers (backends never need to call external non-Google websites) |
| **2. Network Segmentation & Load Balancing** | • **Global External Application Load Balancer** with a reserved static IP, automatic HTTP-to-HTTPS redirect, and a Google-managed SSL certificate<br>• Path-based routing (`/api/ingest/*`, `/api/agent/*`, and `/*`) uniting the frontend and APIs under a single HTTPS origin<br>• Cloud Run ingress locked strictly to internal and Load Balancer traffic<br>• Separate private subnets isolating the PDF ingestion workload from the AI agent workload | • Paying for a custom registered domain name by default (uses a free wildcard DNS hostname tied to the Load Balancer IP, though custom domains remain supported) |
| **3. Private Endpoints** | • **Private Google Access** route and **Private Cloud DNS** zone routing `*.googleapis.com` traffic over Google's private internal VIPs<br>• Dedicated **Regional Private Service Connect (PSC)** endpoint and private DNS zone for regional Model Armor screening traffic | • Site-to-site VPN or Cloud Interconnect to on-premises data centers |

---

## 3. Key Design Decisions (In Plain English)

### 3.1 A Private Network With No Internet Exit
- **Eliminating Outbound Data Exfiltration:** By creating a custom VPC and removing its default route to the internet, backend containers have no path to external IP addresses. Attach points use Cloud Run's serverless Direct VPC Egress with all traffic routed into the VPC, which scales automatically and incurs no idle virtual machine charges.
- **Disabling External Telemetry:** Backend containers also disable anonymous third-party library telemetry so services never waste time attempting outbound analytics connections that the private network will drop.

### 3.2 Public Front Lobby vs. Private Back Rooms (Load Balancer & Segmentation)
- **One Public Front Door:** A Global External Application Load Balancer receives all incoming HTTPS traffic from users' browsers and forwards requests internally based on the URL path:
  - Requests to `/api/ingest/*` are stripped of the `/api/ingest` prefix and routed to the Ingestion service.
  - Requests to `/api/agent/*` are stripped of the `/api/agent` prefix and routed to the Agent service.
  - All other requests (`/*`) are routed to the Frontend web application.
- **Same-Origin Simplicity:** Because the website and both backend APIs share the exact same HTTPS domain through the Load Balancer, the browser communicates with `/api/ingest` and `/api/agent` on the same origin, simplifying security configuration and CORS handling.
- **Blocking Direct Container Access:** All three Cloud Run services are configured to accept traffic only from the internal Load Balancer. Anyone attempting to visit a service's underlying `*.run.app` URL directly from the internet is immediately rejected by Google's front-end edge before the request ever reaches the container.
- **Separate Internal Subnets:** The Ingestion service (which parses untrusted user-uploaded PDFs) runs in its own dedicated private subnet, isolated from the Agent service subnet and the Private Service Connect subnet.

### 3.3 Private Underground Tunnels to Google Cloud Services
- **Private Google Access for Global & Regional Google APIs:** Both backend subnets enable Private Google Access with a single internal route to Google's private virtual IP range (`199.36.153.8/30`). A Private Cloud DNS zone for `googleapis.com` maps Google API calls (Cloud Storage, Vertex AI, Cloud Logging, Cloud DLP, and Firebase public signing keys) onto that private internal pathway without touching the public internet.
- **Regional Private Service Connect for Model Armor:** Because regional `*.rep.googleapis.com` endpoints use Private Service Connect rather than the standard `private.googleapis.com` VIP range, the VPC also provisions a dedicated Private Service Connect subnet, a regional endpoint (`modelarmor.<region>.rep.googleapis.com`), and a private DNS zone so AI safety screening traffic stays 100% inside the private network.

### 3.4 Automatic Free HTTPS Certificate Without Buying a Domain
- **Zero-Cost Managed TLS:** Google-managed SSL certificates and browser authentication flows require a valid HTTPS domain name rather than a raw IP address. Terraform automatically constructs an `<IP-with-dashes>.sslip.io` hostname from the Load Balancer's reserved static IP. Because `sslip.io` automatically resolves that hostname back to the Load Balancer IP, Google Cloud can provision and renew a trusted HTTPS certificate automatically with zero manual DNS setup.

---

## 4. Step-by-Step Implementation Phases

### Phase 1: Networking APIs, IAM Roles & Variables (`Makefile` & `infra/`)
**Goal:** Enable the Google Cloud networking, DNS, and Private Service Connect APIs and grant the deployment service account permission to manage network resources.

- [x] **Enable APIs & Deployer Permissions (`Makefile` & `infra/apis.tf`):**
  - Enable `compute.googleapis.com`, `dns.googleapis.com`, and `networkconnectivity.googleapis.com`.
  - Grant `roles/compute.networkAdmin`, `roles/compute.loadBalancerAdmin`, `roles/compute.securityAdmin`, `roles/dns.admin`, and `roles/networkconnectivity.regionalEndpointAdmin` to the Terraform deployer service account.
- [x] **Network Subnet Variables (`infra/variables.tf` & `infra/terraform.tfvars.example`):**
  - Define configurable private IPv4 CIDR ranges for `ingestion_subnet_cidr` (`10.10.1.0/26`), `agent_subnet_cidr` (`10.10.2.0/26`), and `psc_subnet_cidr` (`10.10.3.0/28`).

---

### Phase 2: Private VPC, Segmented Subnets & Private Endpoints (`infra/network.tf`)
**Goal:** Provision the zero-internet VPC, isolated workload subnets, Private Google Access route, Regional Private Service Connect endpoint, and private DNS zones.

- [x] **Custom Zero-Internet VPC & Workload Subnets (`infra/network.tf`):**
  - Provision `google_compute_network.main` with `auto_create_subnetworks = false` and `delete_default_routes_on_create = true` to remove default public internet egress.
  - Provision `ingestion-subnet` (`10.10.1.0/26`) and `agent-subnet` (`10.10.2.0/26`) in `var.region` with `private_ip_google_access = true`.
- [x] **Private Google Access Route & Private Cloud DNS (`infra/network.tf`):**
  - Create a static VPC route (`google_compute_route.private_googleapis`) directing `199.36.153.8/30` to Google's internal gateway.
  - Create a private Cloud DNS zone for `googleapis.com.` mapping `private.googleapis.com.` to `199.36.153.8–11` and wildcard `*.googleapis.com.` via CNAME.
- [x] **Regional Private Service Connect for Model Armor (`infra/network.tf`):**
  - Provision `psc-subnet` (`10.10.3.0/28`), a regional PSC endpoint (`google_network_connectivity_regional_endpoint.model_armor_psc`) targeting `modelarmor.${var.region}.rep.googleapis.com`, and a private DNS zone resolving `modelarmor.${var.region}.rep.googleapis.com.` to the internal PSC IP address.

---

### Phase 3: Global External HTTPS Load Balancer (`infra/load_balancer.tf`)
**Goal:** Expose the entire application through a single HTTPS Load Balancer with path-based routing, managed TLS, HTTP-to-HTTPS redirection, and access logging.

- [x] **Static IP & Managed SSL Certificate (`infra/load_balancer.tf`):**
  - Reserve a global static IPv4 address (`google_compute_global_address.lb_ip`).
  - Derive the `sslip.io` domain name and provision a Google-managed SSL certificate (`google_compute_managed_ssl_certificate.lb_cert`).
- [x] **Serverless NEGs, Backend Services & URL Map (`infra/load_balancer.tf`):**
  - Create Serverless Network Endpoint Groups (`google_compute_region_network_endpoint_group`) for `frontend`, `ingestion_api`, and `agent_api`.
  - Create Backend Services (`EXTERNAL_MANAGED`) with HTTP access logging enabled (`log_config`) on the backend API services.
  - Configure the HTTPS URL Map (`google_compute_url_map.https_router`) to rewrite `/api/ingest/*` $\rightarrow$ `/` on `ingestion_backend`, `/api/agent/*` $\rightarrow$ `/` on `agent_backend`, and route all other paths (`/*`) to `frontend_backend`.
  - Configure port `443` HTTPS forwarding and port `80` HTTP-to-HTTPS (`301`) redirect rules.

---

### Phase 4: Cloud Run Lockdown & Same-Origin Wiring (`infra/cloud_run.tf`, `infra/auth.tf`, `infra/outputs.tf`)
**Goal:** Block direct internet access to Cloud Run containers, attach backend services to their private subnets, and connect the frontend and authentication domain to the Load Balancer URL.

- [x] **Lock Down Cloud Run Ingress & Egress (`infra/cloud_run.tf`):**
  - Set `ingress = "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER"` on `frontend`, `ingestion_api`, and `agent_api`.
  - Attach `vpc_access` (`egress = "ALL_TRAFFIC"`) connecting `ingestion_api` to `ingestion-subnet` and `agent_api` to `agent-subnet`.
  - Set `ANONYMIZED_TELEMETRY = "FALSE"` on both backend services.
  - Configure `frontend` with same-origin relative API paths (`INGESTION_API_URL = "/api/ingest"`, `AGENT_API_URL = "/api/agent"`) and include the Load Balancer `https://` URL in `ALLOWED_ORIGINS`.
- [x] **Authorize Load Balancer Domain & Export Outputs (`infra/auth.tf` & `infra/outputs.tf`):**
  - Add the Load Balancer `sslip.io` domain to Identity Platform `authorized_domains`.
  - Export `load_balancer_url`, `load_balancer_ip`, and `model_armor_psc_endpoint_ip` in Terraform outputs.

---

## 5. Network Security & Connectivity Verification Playbook

First, retrieve your deployed Load Balancer URL and Cloud Run service URLs:

```bash
make outputs PROJECT_ID="${PROJECT_ID}" REGION="${REGION}"
```

### 1. Verify Direct Cloud Run URLs Are Blocked from the Internet
Attempting to call any underlying `*.run.app` service URL directly from outside the Load Balancer must be rejected at Google's network edge:

```bash
curl -i "${INGESTION_RUN_APP_URL}/health"
curl -i "${AGENT_RUN_APP_URL}/health"
curl -i "${FRONTEND_RUN_APP_URL}/healthz"
# Expect on all 3: HTTP/2 404 or 403 rejection from Google Front End (request never reaches the container)
```

### 2. Verify HTTPS Load Balancer Routing & HTTP-to-HTTPS Redirect
Confirm that the single public Load Balancer front door serves the frontend and routes `/api/ingest/*` and `/api/agent/*` internally over HTTPS:

```bash
# 1. Verify port 80 HTTP requests redirect (301) to HTTPS
curl -i "http://${LB_DOMAIN}/"
# Expect: HTTP/1.1 301 Moved Permanently -> Location: https://${LB_DOMAIN}/

# 2. Verify all three services respond with HTTP 200 OK through the HTTPS Load Balancer
curl -i "${LB_URL}/healthz"
curl -i "${LB_URL}/api/ingest/health"
curl -i "${LB_URL}/api/agent/health"
# Expect on all 3: HTTP/2 200 OK
```

### 3. Verify Unauthenticated API Calls Still Require Authentication
Confirm that routing through the Load Balancer preserves zero-trust authentication on all protected endpoints:

```bash
curl -i "${LB_URL}/api/ingest/collections"
curl -i -X POST "${LB_URL}/api/agent/agent" \
  -H "Content-Type: application/json" \
  -d '{"query": "Test", "collection_name": "docs"}'
# Expect on both: HTTP/2 401 Unauthorized
```

### 4. Verify Private Internal Tunnels to Storage, Vertex AI & Model Armor
Verify that the isolated backend containers (which have zero outbound internet access) can privately reach Cloud Storage, Vertex AI, Identity Platform keys, Cloud Logging, and the Regional Model Armor Private Service Connect endpoint:

```bash
# 1. Upload a PDF through the Load Balancer (exercises Private Google Access to Cloud Storage + Vertex AI Embeddings)
curl -s -X POST "${LB_URL}/api/ingest/ingest" \
  -H "Authorization: Bearer ${ID_TOKEN}" \
  -F "collection_name=network_test" \
  -F "files=@sample.pdf;type=application/pdf" | jq .

# 2. Query the Agent through the Load Balancer (exercises Regional PSC to Model Armor + Private Google Access to Vertex AI Gemini & Cloud Storage)
curl -s -X POST "${LB_URL}/api/agent/agent" \
  -H "Authorization: Bearer ${ID_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"query": "Summarize this document.", "collection_name": "network_test", "stream": false}' | jq .
```
