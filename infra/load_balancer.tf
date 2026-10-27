# ==============================================================================
# Global External Application Load Balancer (Single Public HTTPS Front Door)
#
# End-to-end request flow:
#   Browser -> Global Static IP (`lb_ip:443`)
#           -> Forwarding Rule (`https`)
#           -> Target HTTPS Proxy (`https_proxy` + Managed TLS Cert `lb_cert`)
#           -> URL Map (`https_router`: path routing + `/api/*` prefix stripping)
#           -> Backend Services (`*_backend`)
#           -> Serverless NEGs (`*_neg`)
#           -> Cloud Run (`frontend`, `ingestion-api`, `agent-api`)
# ==============================================================================

# ==============================================================================
# 1. Static Global IPv4 Address & Automatic `sslip.io` Hostname
# ==============================================================================

locals {
  # Public Certificate Authorities and Firebase Auth require a domain name rather
  # than a raw IP. `sslip.io` is a free wildcard DNS service that automatically
  # resolves `<IP-with-dashes>.sslip.io` (e.g., `136-81-158-54.sslip.io`) back to
  # `lb_ip.address`, enabling zero-config HTTPS without purchasing a custom domain.
  lb_domain = "${replace(google_compute_global_address.lb_ip.address, ".", "-")}.sslip.io"
  lb_url    = "https://${local.lb_domain}"
}

# Reserves a permanent global Anycast IPv4 address at Google's edge.
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

# ==============================================================================
# 2. Google-Managed TLS/HTTPS Certificate
# ==============================================================================

# Provisions and auto-renews a free, publicly trusted TLS certificate for `local.lb_domain`.
# Once port 443 (`https` forwarding rule below) is live, Google verifies DNS points to
# `lb_ip` and flips certificate status from `PROVISIONING` to `ACTIVE` (~10–15 min on first deploy).
resource "google_compute_managed_ssl_certificate" "lb_cert" {
  name        = "${var.app_name}-lb-cert-${var.environment}"
  project     = var.project_id
  description = "Google-managed HTTPS certificate for ${local.lb_domain}."

  managed {
    domains = [local.lb_domain]
  }

  # Managed SSL certificates are immutable, and GCP blocks deleting a cert while
  # `https_proxy` still references it. `create_before_destroy` provisions any replacement
  # cert first, repoints `https_proxy`, and deletes the old cert with zero downtime.
  lifecycle {
    create_before_destroy = true
  }
}

# ==============================================================================
# 3. Serverless Network Endpoint Groups (NEGs) — Bridge Into Cloud Run
# ==============================================================================

# Instead of VM IP:port backends, a `SERVERLESS` NEG lets the Global Load Balancer
# forward requests privately into regional Cloud Run services (whose direct `*.run.app`
# URLs are locked down via `INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER` in `cloud_run.tf`).
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

# ==============================================================================
# 4. Backend Services (`EXTERNAL_MANAGED`)
# ==============================================================================

# Wraps each Serverless NEG into an Envoy-backed Global Application Load Balancer target
# (also the attachment point if Cloud Armor WAF policies are added later).
# `enable_cdn = false` ensures authenticated API responses, `/env-config.js`, and live
# Server-Sent Events (SSE) streams are never cached at Google's edge.
# Note: `timeout_sec` must NOT be set on Serverless NEG backend services (GCP rejects it
# with HTTP 400); request/SSE timeouts are governed by Cloud Run's `timeout = "300s"`.
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

  log_config {
    enable      = true
    sample_rate = var.lb_log_sample_rate
  }
}

resource "google_compute_backend_service" "agent_backend" {
  name                  = "${var.app_name}-agent-backend-${var.environment}"
  project               = var.project_id
  load_balancing_scheme = "EXTERNAL_MANAGED"
  enable_cdn            = false
  description           = "Backend service routing /api/agent/* SSE (/agent/stream) & REST traffic to agent-api Cloud Run."

  backend {
    group = google_compute_region_network_endpoint_group.agent_neg.id
  }

  log_config {
    enable      = true
    sample_rate = var.lb_log_sample_rate
  }
}

# ==============================================================================
# 5. HTTPS URL Map — Single-Origin Path Routing & Prefix Rewriting
# ==============================================================================

# Serves the React UI and both backend APIs under one HTTPS origin:
# - `/api/ingest/*` -> strips `/api/ingest` (`path_prefix_rewrite = "/"`) and forwards
#   to `ingestion-api` (e.g., `/api/ingest/collections` arrives at FastAPI as `/collections`).
# - `/api/agent/*`  -> strips `/api/agent` (`path_prefix_rewrite = "/"`) and forwards
#   to `agent-api` (e.g., `/api/agent/agent/stream` arrives at FastAPI as `/agent/stream`).
# - `/*` (default)  -> forwards directly to `frontend` (React SPA).
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

# ==============================================================================
# 6. HTTPS Target Proxy & Port 443 Global Forwarding Rule
# ==============================================================================

# Terminates TLS at Google's edge using `lb_cert` and passes decrypted HTTP/2
# requests to `https_router` for path-based routing.
resource "google_compute_target_https_proxy" "https_proxy" {
  name             = "${var.app_name}-https-proxy-${var.environment}"
  project          = var.project_id
  url_map          = google_compute_url_map.https_router.id
  ssl_certificates = [google_compute_managed_ssl_certificate.lb_cert.id]
}

# Public listener binding `lb_ip:443` to `https_proxy`.
resource "google_compute_global_forwarding_rule" "https" {
  name                  = "${var.app_name}-https-fwd-${var.environment}"
  project               = var.project_id
  load_balancing_scheme = "EXTERNAL_MANAGED"
  ip_protocol           = "TCP"
  port_range            = "443"
  ip_address            = google_compute_global_address.lb_ip.id
  target                = google_compute_target_https_proxy.https_proxy.id
}

# ==============================================================================
# 7. Port 80 HTTP-to-HTTPS `301` Redirect
# ==============================================================================

# Returns an edge `301 Moved Permanently` redirect to `https://` for any plain `http://`
# visitor on port 80 (preserving URL paths and query parameters via `strip_query = false`),
# ensuring unencrypted requests never reach Cloud Run.
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
