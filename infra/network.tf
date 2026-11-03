# ==============================================================================
# 1. Zero-Internet Custom VPC & Segmented Cloud Run Subnets
# ==============================================================================

# Custom VPC with `delete_default_routes_on_create = true` to remove the 0.0.0.0/0
# default internet route. Any packet addressed outside local VPC subnets (10.10.x.x)
# is dropped unless an explicit `google_compute_route` exists below.
resource "google_compute_network" "main" {
  name                            = "${var.app_name}-vpc-${var.environment}"
  project                         = var.project_id
  auto_create_subnetworks         = false
  routing_mode                    = "REGIONAL"
  delete_default_routes_on_create = true
  description                     = "Private custom VPC for InteractiveRAG (${var.environment}) with no public internet egress route."

  depends_on = [
    google_project_service.required_apis["compute.googleapis.com"],
  ]
}

# Isolated Direct VPC Egress subnet for `ingest-api` (processes untrusted user PDFs).
resource "google_compute_subnetwork" "ingest" {
  name                     = "${var.app_name}-ingest-subnet-${var.environment}"
  project                  = var.project_id
  region                   = var.region
  network                  = google_compute_network.main.id
  ip_cidr_range            = var.ingest_subnet_cidr
  private_ip_google_access = true
  description              = "Isolated Direct VPC Egress subnet for ingest-api Cloud Run service."
}

# Isolated Direct VPC Egress subnet for `agent-api` (runs LangGraph & Vertex AI calls).
resource "google_compute_subnetwork" "agent" {
  name                     = "${var.app_name}-agent-subnet-${var.environment}"
  project                  = var.project_id
  region                   = var.region
  network                  = google_compute_network.main.id
  ip_cidr_range            = var.agent_subnet_cidr
  private_ip_google_access = true
  description              = "Isolated Direct VPC Egress subnet for agent-api Cloud Run service."
}

# ==============================================================================
# 2. Private Google Access for Standard `*.googleapis.com` APIs
#    (Requires BOTH a Layer-3 VPC Route AND Private Cloud DNS records)
# ==============================================================================

# Step A — Layer-3 Packet Route:
# Because `199.36.153.8/30` sits outside our `10.10.x.x` subnets and `0.0.0.0/0` was deleted,
# this route explicitly permits packets ONLY to Google's Private Google Access VIP range.
# For `199.36.153.8/30`, GCP treats `default-internet-gateway` as an internal virtual hop
# onto Google's private backbone without granting public internet access.
resource "google_compute_route" "private_googleapis" {
  name             = "${var.app_name}-private-googleapis-${var.environment}"
  project          = var.project_id
  network          = google_compute_network.main.id
  dest_range       = "199.36.153.8/30"
  next_hop_gateway = "default-internet-gateway"
  priority         = 1000
  description      = "Routes private.googleapis.com (199.36.153.8/30) traffic privately inside Google's backbone."
}

# Step B — Authoritative Private DNS Zone for `googleapis.com.`:
# Creates the empty DNS container scoped to our VPC so `*.googleapis.com` queries do not
# resolve to public Google IPs (e.g., `142.250.x.x`), which have no route in this VPC.
resource "google_dns_managed_zone" "googleapis" {
  name          = "${var.app_name}-googleapis-${var.environment}"
  project       = var.project_id
  dns_name      = "googleapis.com."
  description   = "Private DNS zone routing *.googleapis.com over Private Google Access (private.googleapis.com)."
  visibility    = "private"
  force_destroy = true

  private_visibility_config {
    networks {
      network_url = google_compute_network.main.id
    }
  }

  depends_on = [
    google_project_service.required_apis["dns.googleapis.com"],
  ]
}

# DNS Record 1 (`A`): Maps Google's official `private.googleapis.com.` tier to its 4 Virtual IPs.
# All 4 VIPs are identical Anycast addresses fronting Google Front End (GFE) load balancers
# (provided for DNS round-robin load spreading). A single VIP serves hundreds of Google APIs
# by reading the original hostname (`storage.googleapis.com`, `aiplatform.googleapis.com`, etc.)
# from the TLS SNI handshake and HTTPS `Host` header.
resource "google_dns_record_set" "private_googleapis_a" {
  name         = "private.googleapis.com."
  project      = var.project_id
  managed_zone = google_dns_managed_zone.googleapis.name
  type         = "A"
  ttl          = 300
  rrdatas = [
    "199.36.153.8",
    "199.36.153.9",
    "199.36.153.10",
    "199.36.153.11",
  ]
}

# DNS Record 2 (`CNAME`): Wildcard alias pointing all `*.googleapis.com.` hostnames to
# `private.googleapis.com.` (Google's 2-record reference pattern—keeps `dig` output explicit
# vs. `restricted.googleapis.com.` and avoids repeating raw IP lists if overrides/IPv6 are added).
resource "google_dns_record_set" "googleapis_wildcard_cname" {
  name         = "*.googleapis.com."
  project      = var.project_id
  managed_zone = google_dns_managed_zone.googleapis.name
  type         = "CNAME"
  ttl          = 300
  rrdatas      = ["private.googleapis.com."]
}

# ==============================================================================
# 3. Regional Private Service Connect (PSC) for Model Armor
#    Why needed: Google's `199.36.153.8/30` VIPs do NOT support regional
#    `*.rep.googleapis.com` endpoints (`modelarmor.<region>.rep.googleapis.com`).
# ==============================================================================

# Dedicated internal VPC subnet (`10.10.3.0/28`) hosting regional PSC endpoint IPs.
# Because this subnet lives inside `google_compute_network.main`, GCP creates a local
# subnet route automatically—no extra `google_compute_route` is required.
resource "google_compute_subnetwork" "psc_endpoints" {
  name                     = "${var.app_name}-psc-subnet-${var.environment}"
  project                  = var.project_id
  region                   = var.region
  network                  = google_compute_network.main.id
  ip_cidr_range            = var.psc_subnet_cidr
  private_ip_google_access = true
  description              = "Dedicated internal subnet for regional Private Service Connect Google API endpoints."
}

# Step A — Network Connection (PSC Endpoint):
# Allocates a private IP inside `psc_endpoints` (e.g., `10.10.3.2`) wired directly to
# Google's regional Model Armor backend. Note: PSC only creates the IP connection—it
# does NOT configure DNS, so application code still needs the DNS override below.
resource "google_network_connectivity_regional_endpoint" "modelarmor_psc" {
  name              = "${var.app_name}-modelarmor-psc-${var.environment}"
  project           = var.project_id
  location          = var.region
  target_google_api = "modelarmor.${var.region}.rep.googleapis.com"
  access_type       = "REGIONAL"
  network           = google_compute_network.main.id
  subnetwork        = google_compute_subnetwork.psc_endpoints.id
  description       = "Private Service Connect regional endpoint for Model Armor (modelarmor.${var.region}.rep.googleapis.com)."

  lifecycle {
    precondition {
      condition     = can(regex("^[a-z]+-[a-z]+[0-9]+$", var.region))
      error_message = "Model Armor regional PSC endpoint must reside in a concrete single GCP region."
    }
  }

  depends_on = [
    google_project_service.required_apis["networkconnectivity.googleapis.com"],
    google_project_service.required_apis["modelarmor.googleapis.com"],
  ]
}

# Step B — More-Specific Private DNS Zone for `modelarmor.<region>.rep.googleapis.com.`:
# Cloud DNS uses longest-suffix matching, so this zone takes precedence over the broader
# `googleapis.com.` wildcard zone above and prevents Model Armor queries from going to `199.36.153.8`.
resource "google_dns_managed_zone" "modelarmor_rep" {
  name          = "${var.app_name}-modelarmor-rep-${var.environment}"
  project       = var.project_id
  dns_name      = "modelarmor.${var.region}.rep.googleapis.com."
  description   = "Private DNS zone routing regional Model Armor endpoint to internal PSC endpoint IP."
  visibility    = "private"
  force_destroy = true

  private_visibility_config {
    networks {
      network_url = google_compute_network.main.id
    }
  }

  depends_on = [
    google_project_service.required_apis["dns.googleapis.com"],
  ]
}

# DNS Record (`A`): Resolves `modelarmor.<region>.rep.googleapis.com.` to the internal PSC IP
# (`modelarmor_psc.address`) so `ModelArmorClient` connects over the private PSC endpoint while
# keeping the real hostname for valid TLS certificate verification.
resource "google_dns_record_set" "modelarmor_rep_a" {
  name         = "modelarmor.${var.region}.rep.googleapis.com."
  project      = var.project_id
  managed_zone = google_dns_managed_zone.modelarmor_rep.name
  type         = "A"
  ttl          = 300
  rrdatas      = [google_network_connectivity_regional_endpoint.modelarmor_psc.address]
}
