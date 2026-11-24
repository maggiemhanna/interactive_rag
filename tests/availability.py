"""Automated offline unit test suite for Layer 3: Availability Design (`infra/terraform/availability.tf`).

Verifies:
- Cloud Monitoring HTTPS Uptime Checks probing `/healthz`, `/api/ingest/health`, and `/api/agent/health`
  through the Global External HTTPS Load Balancer with TLS validation and content matchers.
- Cloud Monitoring Custom Services and Declarative SLOs (`good_total_ratio` availability SLOs across
  all three services + `distribution_cut` latency SLO on `agent_turn_latency_ms`).
- Availability alert policies for multi-region uptime check failures and `select_slo_burn_rate` fast burn.
- Static pre-deploy policy gate enforcement (`evaluate_repo_static_policies`) against availability regressions.
- SLI / SLO / Reference SLA hierarchy documentation and mathematical consistency.
"""

import logging
import os
import re
import shutil
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from infra.scripts.check_static_policies import evaluate_repo_static_policies

logger = logging.getLogger("tests.availability")


def _create_synthetic_repo_copy(repo_root: str, prefix: str = "avail_static_policy_") -> str:
    """Creates a minimal temporary copy of `src/core/config.py` and `infra/` for policy mutation tests."""
    tmp_root = tempfile.mkdtemp(prefix=prefix)
    os.makedirs(os.path.join(tmp_root, "src", "core"), exist_ok=True)
    shutil.copy2(
        os.path.join(repo_root, "src", "core", "config.py"),
        os.path.join(tmp_root, "src", "core", "config.py"),
    )
    shutil.copytree(
        os.path.join(repo_root, "infra"),
        os.path.join(tmp_root, "infra"),
    )
    return tmp_root


class TestAvailabilityDesignTerraformAndPolicies(unittest.TestCase):
    """Verifies Terraform availability design resources, outputs, variables, and static policy gates."""

    def setUp(self) -> None:
        self.infra_dir = os.path.join(REPO_ROOT, "infra", "terraform")
        with open(os.path.join(self.infra_dir, "availability.tf"), encoding="utf-8") as fh:
            self.availability_tf = fh.read()
        with open(os.path.join(self.infra_dir, "variables.tf"), encoding="utf-8") as fh:
            self.variables_tf = fh.read()
        with open(os.path.join(self.infra_dir, "outputs.tf"), encoding="utf-8") as fh:
            self.outputs_tf = fh.read()
        with open(os.path.join(self.infra_dir, "terraform.tfvars.example"), encoding="utf-8") as fh:
            self.tfvars_example = fh.read()

    def test_uptime_checks_cover_all_three_services_over_validated_https(self) -> None:
        """Verifies HTTPS uptime checks probe frontend, ingest-api, and agent-api with SSL validation, prod-only gating, and 3-region scoping."""
        self.assertIn('enable_uptime_checks = var.environment == "prod"', self.availability_tf)
        expected_checks = {
            "frontend_health": ("/healthz", "ok"),
            "ingest_api_health": ("/api/ingest/health", "ingest-api"),
            "agent_api_health": ("/api/agent/health", "agent-api"),
        }
        for name, (expected_path, expected_content) in expected_checks.items():
            match = re.search(
                rf'resource\s+"google_monitoring_uptime_check_config"\s+"{name}"\s*\{{(?P<body>.*?)\n\}}',
                self.availability_tf,
                re.DOTALL,
            )
            self.assertIsNotNone(match, f"Missing uptime check resource '{name}'.")
            body = match.group("body")
            self.assertIn("count            = local.enable_uptime_checks ? 1 : 0", body)
            self.assertIn("selected_regions = var.uptime_check_regions", body)
            self.assertIn(f'path           = "{expected_path}"', body)
            self.assertIn("port           = 443", body)
            self.assertIn("use_ssl        = true", body)
            self.assertIn("validate_ssl   = true", body)
            self.assertIn("host       = local.lb_domain", body)
            self.assertIn(f'content = "{expected_content}"', body)

    def test_declarative_monitoring_services_and_slos(self) -> None:
        """Verifies custom services and 4 declarative SLOs (3 availability + 1 latency distribution cut)."""
        for svc_name in ("agent_api", "ingest_api", "frontend"):
            self.assertIn(
                f'resource "google_monitoring_custom_service" "{svc_name}"',
                self.availability_tf,
            )

        for slo_name in (
            "agent_api_availability_slo",
            "ingest_api_availability_slo",
            "frontend_availability_slo",
        ):
            match = re.search(
                rf'resource\s+"google_monitoring_slo"\s+"{slo_name}"\s*\{{(?P<body>.*?)\n\}}',
                self.availability_tf,
                re.DOTALL,
            )
            self.assertIsNotNone(match, f"Missing availability SLO '{slo_name}'.")
            body = match.group("body")
            self.assertIn("rolling_period_days = var.slo_rolling_period_days", body)
            self.assertIn("good_total_ratio", body)
            self.assertIn("bad_service_filter", body)
            self.assertIn("total_service_filter", body)
            self.assertIn('response_code_class=\\"5xx\\"', body)

        latency_match = re.search(
            r'resource\s+"google_monitoring_slo"\s+"agent_api_latency_slo"\s*\{(?P<body>.*?)\n\}',
            self.availability_tf,
            re.DOTALL,
        )
        self.assertIsNotNone(latency_match, "Missing agent_api_latency_slo.")
        latency_body = latency_match.group("body")
        self.assertIn("distribution_cut", latency_body)
        self.assertIn("agent_turn_latency_ms", latency_body)
        self.assertIn("max = var.slo_agent_latency_threshold_ms", latency_body)

    def test_availability_alert_policies_and_outputs(self) -> None:
        """Verifies multi-region uptime failure alert, fast SLO burn-rate alert, variables, and outputs."""
        self.assertIn(
            'resource "google_monitoring_alert_policy" "uptime_check_failure_alert"',
            self.availability_tf,
        )
        self.assertIn("REDUCE_COUNT_FALSE", self.availability_tf)
        self.assertIn(
            'resource "google_monitoring_alert_policy" "slo_fast_burn_alert"',
            self.availability_tf,
        )
        self.assertEqual(self.availability_tf.count("select_slo_burn_rate("), 3)

        period_match = re.search(
            r'variable\s+"uptime_check_period_seconds"\s*\{.*?default\s*=\s*([0-9]+)',
            self.variables_tf,
            re.DOTALL,
        )
        self.assertIsNotNone(period_match)
        self.assertEqual(int(period_match.group(1)), 300)

        for var_name in (
            "uptime_check_period_seconds",
            "uptime_check_regions",
            "uptime_check_timeout_seconds",
            "slo_rolling_period_days",
            "slo_agent_availability_goal",
            "slo_agent_latency_goal",
            "slo_agent_latency_threshold_ms",
            "slo_ingest_availability_goal",
            "slo_frontend_availability_goal",
            "slo_fast_burn_rate_threshold",
        ):
            self.assertIn(f'variable "{var_name}"', self.variables_tf)
            self.assertIn(f"{var_name}", self.tfvars_example)

        for out_name in (
            "uptime_check_ids",
            "monitoring_service_ids",
            "slo_ids",
            "availability_alert_policy_ids",
        ):
            self.assertIn(f'output "{out_name}"', self.outputs_tf)

    def test_static_policy_gate_catches_availability_regressions(self) -> None:
        """Verifies `evaluate_repo_static_policies` catches disabled SSL validation or missing SLOs."""
        ok, violations = evaluate_repo_static_policies(REPO_ROOT)
        self.assertTrue(ok, f"Expected static policy check to pass, got: {violations}")

        syn_root = _create_synthetic_repo_copy(REPO_ROOT, prefix="avail_static_policy_")
        try:
            avail_path = os.path.join(syn_root, "infra", "terraform", "availability.tf")
            with open(avail_path, encoding="utf-8") as fh:
                orig_avail = fh.read()

            # Regression 1: Disabling TLS validation on an uptime check
            with open(avail_path, "w", encoding="utf-8") as fh:
                fh.write(orig_avail.replace("validate_ssl   = true", "validate_ssl   = false", 1))
            ok_ssl, v_ssl = evaluate_repo_static_policies(syn_root)
            self.assertFalse(ok_ssl)
            self.assertTrue(any("validate_ssl = true" in m for m in v_ssl), v_ssl)

            # Regression 2: Removing an availability SLO
            with open(avail_path, "w", encoding="utf-8") as fh:
                fh.write(
                    orig_avail.replace(
                        'resource "google_monitoring_slo" "agent_api_availability_slo"',
                        'resource "google_monitoring_slo" "removed_slo"',
                        1,
                    )
                )
            ok_slo, v_slo = evaluate_repo_static_policies(syn_root)
            self.assertFalse(ok_slo)
            self.assertTrue(any("agent_api_availability_slo" in m for m in v_slo), v_slo)
        finally:
            shutil.rmtree(syn_root, ignore_errors=True)

    def test_sli_slo_sla_hierarchy_documentation_and_defaults(self) -> None:
        """Verifies default SLO goals maintain a strict safety margin above reference SLAs and are documented in README.md."""
        # Extract default numeric goals from variables.tf
        def _default_float(var_name: str) -> float:
            m = re.search(
                rf'variable\s+"{var_name}"\s*\{{.*?default\s*=\s*([0-9.]+)',
                self.variables_tf,
                re.DOTALL,
            )
            self.assertIsNotNone(m, f"Could not parse default for {var_name}")
            return float(m.group(1))

        agent_avail_slo = _default_float("slo_agent_availability_goal")
        ingest_avail_slo = _default_float("slo_ingest_availability_goal")
        frontend_avail_slo = _default_float("slo_frontend_availability_goal")
        agent_latency_slo = _default_float("slo_agent_latency_goal")

        # Reference external SLAs must remain strictly looser than internal SLO targets
        # (creating an operational error-budget buffer before any external SLA breach).
        self.assertGreater(agent_avail_slo, 0.990)
        self.assertGreater(ingest_avail_slo, 0.990)
        self.assertGreater(frontend_avail_slo, 0.995)
        self.assertGreaterEqual(agent_latency_slo, 0.90)

        with open(os.path.join(REPO_ROOT, "README.md"), encoding="utf-8") as fh:
            readme = fh.read()
        self.assertIn("Cloud Monitoring Uptime Checks", readme)
        self.assertIn("Declarative Cloud Monitoring SLOs", readme)
        self.assertIn("SLI / SLO / Reference SLA Hierarchy", readme)


if __name__ == "__main__":
    logger.info("Running Layer 3 Availability Design unit test suite...")
    unittest.main(verbosity=2)
