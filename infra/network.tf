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

# Room 1: Dedicated Direct VPC Egress subnet for Document Ingestion API (src/add_documents.py)
resource "google_compute_subnetwork" "ingestion" {
  name                     = "${var.app_name}-ingestion-subnet-${var.environment}"
  project                  = var.project_id
  region                   = var.region
  network                  = google_compute_network.main.id
  ip_cidr_range            = var.ingestion_subnet_cidr
  private_ip_google_access = true
  description              = "Isolated Direct VPC Egress subnet for ingestion-api Cloud Run service."
}

# Room 2: Dedicated Direct VPC Egress subnet for Agentic RAG API (src/agentic_rag.py)
resource "google_compute_subnetwork" "agent" {
  name                     = "${var.app_name}-agent-subnet-${var.environment}"
  project                  = var.project_id
  region                   = var.region
  network                  = google_compute_network.main.id
  ip_cidr_range            = var.agent_subnet_cidr
  private_ip_google_access = true
  description              = "Isolated Direct VPC Egress subnet for agent-api Cloud Run service."
}
