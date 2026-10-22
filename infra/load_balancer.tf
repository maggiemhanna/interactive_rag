locals {
  # Automatic wildcard DNS hostname resolving directly to the Load Balancer's static IP
  lb_domain = "${replace(google_compute_global_address.lb_ip.address, ".", "-")}.sslip.io"
  lb_url    = "https://${local.lb_domain}"
}

# 1. Permanent Public Static IPv4 Address (Single Front Door)
resource "google_compute_global_address" "lb_ip" {
  name         = "${var.app_name}-lb-ip-${var.environment}"
  project      = var.project_id
  address_type = "EXTERNAL"
  ip_version   = "IPV4"
  description  = "Static global IPv4 address for the InteractiveRAG External HTTPS Load Balancer."

  depends_on = [
    google_project_service.required_apis["compute.googleapis.com"],
  ]
}

# 2. Free Google-Managed SSL Certificate for <LB_IP_WITH_DASHES>.sslip.io
resource "google_compute_managed_ssl_certificate" "lb_cert" {
  name        = "${var.app_name}-lb-cert-${var.environment}"
  project     = var.project_id
  description = "Google-managed HTTPS certificate for ${local.lb_domain}."

  managed {
    domains = [local.lb_domain]
  }

  lifecycle {
    create_before_destroy = true
  }
}

# 3. Serverless Network Endpoint Groups (NEGs) connecting Load Balancer to Cloud Run
resource "google_compute_region_network_endpoint_group" "frontend_neg" {
  name                  = "${var.app_name}-fe-neg-${var.environment}"
  project               = var.project_id
  region                = var.region
  network_endpoint_type = "SERVERLESS"

  cloud_run {
    service = google_cloud_run_v2_service.frontend.name
  }
}

resource "google_compute_region_network_endpoint_group" "ingestion_neg" {
  name                  = "${var.app_name}-ingest-neg-${var.environment}"
  project               = var.project_id
  region                = var.region
  network_endpoint_type = "SERVERLESS"

  cloud_run {
    service = google_cloud_run_v2_service.ingestion_api.name
  }
}

resource "google_compute_region_network_endpoint_group" "agent_neg" {
  name                  = "${var.app_name}-agent-neg-${var.environment}"
  project               = var.project_id
  region                = var.region
  network_endpoint_type = "SERVERLESS"

  cloud_run {
    service = google_cloud_run_v2_service.agent_api.name
  }
}

# 4. Global External Managed Backend Services
resource "google_compute_backend_service" "frontend_backend" {
  name                  = "${var.app_name}-fe-backend-${var.environment}"
  project               = var.project_id
  load_balancing_scheme = "EXTERNAL_MANAGED"
  enable_cdn            = false
  description           = "Backend service routing default /* traffic to frontend Cloud Run."

  backend {
    group = google_compute_region_network_endpoint_group.frontend_neg.id
  }
}

resource "google_compute_backend_service" "ingestion_backend" {
  name                  = "${var.app_name}-ingest-backend-${var.environment}"
  project               = var.project_id
  load_balancing_scheme = "EXTERNAL_MANAGED"
  enable_cdn            = false
  description           = "Backend service routing /api/ingest/* traffic to ingestion-api Cloud Run."

  backend {
    group = google_compute_region_network_endpoint_group.ingestion_neg.id
  }
}

resource "google_compute_backend_service" "agent_backend" {
  name                  = "${var.app_name}-agent-backend-${var.environment}"
  project               = var.project_id
  load_balancing_scheme = "EXTERNAL_MANAGED"
  enable_cdn            = false
  description           = "Backend service routing /api/agent/* SSE & REST traffic to agent-api Cloud Run."

  backend {
    group = google_compute_region_network_endpoint_group.agent_neg.id
  }
}

# 5. HTTPS URL Map (Path Routing + Prefix Rewriting)
resource "google_compute_url_map" "https_router" {
  name            = "${var.app_name}-https-urlmap-${var.environment}"
  project         = var.project_id
  default_service = google_compute_backend_service.frontend_backend.id
  description     = "Routes /api/ingest/* and /api/agent/* to internal backends and /* to frontend."

  host_rule {
    hosts        = ["*"]
    path_matcher = "all-paths"
  }

  path_matcher {
    name            = "all-paths"
    default_service = google_compute_backend_service.frontend_backend.id

    path_rule {
      paths   = ["/api/ingest/*"]
      service = google_compute_backend_service.ingestion_backend.id

      route_action {
        url_rewrite {
          path_prefix_rewrite = "/"
        }
      }
    }

    path_rule {
      paths   = ["/api/agent/*"]
      service = google_compute_backend_service.agent_backend.id

      route_action {
        url_rewrite {
          path_prefix_rewrite = "/"
        }
      }
    }
  }
}

# 6. HTTPS Target Proxy & Port 443 Global Forwarding Rule
resource "google_compute_target_https_proxy" "https_proxy" {
  name             = "${var.app_name}-https-proxy-${var.environment}"
  project          = var.project_id
  url_map          = google_compute_url_map.https_router.id
  ssl_certificates = [google_compute_managed_ssl_certificate.lb_cert.id]
}

resource "google_compute_global_forwarding_rule" "https" {
  name                  = "${var.app_name}-https-fwd-${var.environment}"
  project               = var.project_id
  load_balancing_scheme = "EXTERNAL_MANAGED"
  ip_protocol           = "TCP"
  port_range            = "443"
  ip_address            = google_compute_global_address.lb_ip.id
  target                = google_compute_target_https_proxy.https_proxy.id
}

# 7. HTTP-to-HTTPS 301 Redirect on Port 80
resource "google_compute_url_map" "http_redirect" {
  name        = "${var.app_name}-http-redirect-${var.environment}"
  project     = var.project_id
  description = "Redirects all plain HTTP traffic on port 80 to HTTPS on port 443."

  default_url_redirect {
    https_redirect         = true
    redirect_response_code = "MOVED_PERMANENTLY_DEFAULT"
    strip_query            = false
  }
}

resource "google_compute_target_http_proxy" "http_proxy" {
  name    = "${var.app_name}-http-proxy-${var.environment}"
  project = var.project_id
  url_map = google_compute_url_map.http_redirect.id
}

resource "google_compute_global_forwarding_rule" "http_redirect" {
  name                  = "${var.app_name}-http-fwd-${var.environment}"
  project               = var.project_id
  load_balancing_scheme = "EXTERNAL_MANAGED"
  ip_protocol           = "TCP"
  port_range            = "80"
  ip_address            = google_compute_global_address.lb_ip.id
  target                = google_compute_target_http_proxy.http_proxy.id
}
