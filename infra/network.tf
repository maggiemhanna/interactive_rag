# Private Custom VPC with no default public internet route
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

# Room 1: Dedicated Direct VPC Egress subnet for Document Ingestion API (src/ingestion/app.py)
resource "google_compute_subnetwork" "ingestion" {
  name                     = "${var.app_name}-ingestion-subnet-${var.environment}"
  project                  = var.project_id
  region                   = var.region
  network                  = google_compute_network.main.id
  ip_cidr_range            = var.ingestion_subnet_cidr
  private_ip_google_access = true
  description              = "Isolated Direct VPC Egress subnet for ingestion-api Cloud Run service."
}

# Room 2: Dedicated Direct VPC Egress subnet for Agentic RAG API (src/agent/app.py)
resource "google_compute_subnetwork" "agent" {
  name                     = "${var.app_name}-agent-subnet-${var.environment}"
  project                  = var.project_id
  region                   = var.region
  network                  = google_compute_network.main.id
  ip_cidr_range            = var.agent_subnet_cidr
  private_ip_google_access = true
  description              = "Isolated Direct VPC Egress subnet for agent-api Cloud Run service."
}

# Private Tunnel Route: Allows traffic ONLY to Google's Private Google Access VIP range (199.36.153.8/30).
# Note: In GCP, 'default-internet-gateway' for 199.36.153.8/30 is Google's internal virtual hop
# that keeps packets strictly on Google's private backbone without granting public internet access.
resource "google_compute_route" "private_googleapis" {
  name             = "${var.app_name}-private-googleapis-${var.environment}"
  project          = var.project_id
  network          = google_compute_network.main.id
  dest_range       = "199.36.153.8/30"
  next_hop_gateway = "default-internet-gateway"
  priority         = 1000
  description      = "Routes private.googleapis.com (199.36.153.8/30) traffic privately inside Google's backbone."
}

# Private Address Book (Cloud DNS): Directs all *.googleapis.com calls (Cloud Storage, Vertex AI,
# Identity Platform, Google public JWT certs) into the private 199.36.153.8/30 tunnel.
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

resource "google_dns_record_set" "googleapis_wildcard_cname" {
  name         = "*.googleapis.com."
  project      = var.project_id
  managed_zone = google_dns_managed_zone.googleapis.name
  type         = "CNAME"
  ttl          = 300
  rrdatas      = ["private.googleapis.com."]
}
