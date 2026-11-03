"""Offline unit test suite for Scalability & Elasticity (workload isolation, scaling policies, cache bounds, and incremental GCS delta sync)."""

from __future__ import annotations

import inspect
import json
import os
import shutil
import tempfile
import time
import unittest
from typing import Dict
from unittest.mock import patch

from fastapi.testclient import TestClient

from scripts.check_static_policies import evaluate_repo_static_policies
from src.agent import app as agent_app_module
from src.agent import graph as agent_graph_module
from src.core import config as config_module
from src.core.compliance.policy import reset_rate_limiters
from src.core.context import (
    RequestContext,
    bind_request_context,
    reset_request_context,
)
from src.core.observability import get_recorded_spans
from src.core.storage import chroma as chroma_module
from src.core.storage import gcs_sync as gcs_sync_module
from src.core.storage import evict_tmp_cache, release_client_cache, user_write_lock
from src.ingest import app as ingest_app_module
from src.ingest import routes as ingest_routes_module
from tests.ai_security import (
    DeterministicFakeEmbeddings,
    GroundedCitingChatModel,
    make_test_pdf,
)
from tests.storage import FakeGCSBucket


class TestModularPackageSplitAndTwoServiceIsolation(unittest.TestCase):
    """Verifies the 4-package modular split (src/ingest, src/manage, src/query, src/agent) and 2-service backend isolation."""

    def setUp(self) -> None:
        self.chroma_tmp = tempfile.mkdtemp(prefix="scale_chroma_")
        self.uploads_tmp = tempfile.mkdtemp(prefix="scale_uploads_")
        self.fake_bucket = FakeGCSBucket("interactive-rag-scale-bucket")
        agent_graph_module.MEMORY_SAVER.storage.clear()
        reset_rate_limiters()

        self.ingest_client = TestClient(ingest_app_module.app)
        self.agent_client = TestClient(agent_app_module.app)

    def tearDown(self) -> None:
        if os.path.exists(self.chroma_tmp):
            for entry in os.scandir(self.chroma_tmp):
                if entry.is_dir():
                    release_client_cache(entry.path)
            shutil.rmtree(self.chroma_tmp, ignore_errors=True)
        shutil.rmtree(self.uploads_tmp, ignore_errors=True)
        reset_rate_limiters()

    def _env(self, **overrides: str) -> Dict[str, str]:
        env = {
            "AUTH_DISABLED": "true",
            "K_SERVICE": "",
            "DEV_USER_ID": "scale_user",
            "GOOGLE_CLOUD_PROJECT": "interactive-rag",
            "GOOGLE_CLOUD_LOCATION": "us-central1",
            "ALLOWED_LLM_MODELS": "gemini-2.5-flash,gemini-2.5-pro,gemini-2.5-flash-lite",
            "ALLOWED_EMBEDDING_MODELS": "gemini-embedding-001,text-embedding-005",
            "LOCAL_CHROMA_ROOT": self.chroma_tmp,
            "UPLOAD_TMP_ROOT": self.uploads_tmp,
            "GCS_BUCKET_NAME": self.fake_bucket.name,
            "ARCHIVE_UPLOADS_TO_GCS": "false",
            "MODEL_ARMOR_TEMPLATE_ID": "",
            "MODEL_ARMOR_FAIL_CLOSED": "false",
        }
        env.update(overrides)
        return env

    def test_four_modular_packages_and_no_run_in_threadpool(self) -> None:
        """Verifies src/ingest, src/manage, src/query, and src/agent exist cleanly and POST /ingest does NOT use run_in_threadpool."""
        import src.agent
        import src.ingest
        import src.manage
        import src.query

        self.assertTrue(hasattr(src.ingest, "ingest_router"))
        self.assertTrue(hasattr(src.manage, "collections_router"))
        self.assertTrue(hasattr(src.manage, "compliance_router"))
        self.assertTrue(hasattr(src.manage, "manage_router"))
        self.assertTrue(hasattr(src.query, "query_router"))
        self.assertTrue(hasattr(src.agent, "agent_router"))
        self.assertTrue(hasattr(src.agent, "app"))

        # Verify legacy src/ingestion package is removed
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.assertFalse(os.path.exists(os.path.join(repo_root, "src", "ingestion")))

        # Verify POST /ingest is an async handler and does NOT use run_in_threadpool
        self.assertTrue(inspect.iscoroutinefunction(ingest_routes_module.ingest))
        routes_source = inspect.getsource(ingest_routes_module)
        self.assertNotIn("run_in_threadpool", routes_source)

    def test_two_service_route_isolation_and_shared_agent_cache_and_rate_limiter(self) -> None:
        """Verifies ingest-api mounts only /ingest + /health, while agent-api mounts /query, /collections, /compliance/my-data, /agent, /agent/stream + /health."""
        ingest_paths = set(ingest_app_module.app.openapi()["paths"].keys())
        agent_paths = set(agent_app_module.app.openapi()["paths"].keys())

        self.assertEqual(ingest_paths, {"/ingest", "/health"})
        for forbidden_on_ingest in (
            "/query",
            "/collections",
            "/collections/{collection_name}",
            "/compliance/my-data",
            "/agent",
            "/agent/stream",
        ):
            self.assertNotIn(forbidden_on_ingest, ingest_paths)

        for required_on_agent in (
            "/query",
            "/collections",
            "/collections/{collection_name}",
            "/compliance/my-data",
            "/agent",
            "/agent/stream",
            "/health",
        ):
            self.assertIn(required_on_agent, agent_paths)
        self.assertNotIn("/ingest", agent_paths)

        # Verify HTTP 404 on unmounted routes and shared _QUERY_RATE_LIMITER on agent-api
        with patch.dict(
            os.environ,
            self._env(RATE_LIMIT_QUERY_PER_MINUTE="2"),
            clear=False,
        ), patch.object(
            gcs_sync_module, "_get_bucket", return_value=self.fake_bucket
        ), patch.object(
            chroma_module,
            "GoogleGenerativeAIEmbeddings",
            return_value=DeterministicFakeEmbeddings(),
        ), patch.object(
            agent_graph_module,
            "ChatGoogleGenerativeAI",
            side_effect=GroundedCitingChatModel,
        ):
            self.assertEqual(self.ingest_client.get("/collections").status_code, 404)
            self.assertEqual(
                self.ingest_client.post("/query", json={"query": "hi", "collection_name": "c"}).status_code,
                404,
            )
            self.assertEqual(self.ingest_client.get("/compliance/my-data").status_code, 404)
            self.assertEqual(
                self.agent_client.post("/ingest", json={"pdf_paths": ["a.pdf"], "collection_name": "c"}).status_code,
                404,
            )

            # Ingest via ingest-api
            r_ing = self.ingest_client.post(
                "/ingest",
                data={"collection_name": "shared_col"},
                files=[("files", ("doc.pdf", make_test_pdf("Shared cache test"), "application/pdf"))],
            )
            self.assertEqual(r_ing.status_code, 200)

            # 1st query via /query on agent-api (consumes 1/2 of RATE_LIMIT_QUERY_PER_MINUTE)
            r_q1 = self.agent_client.post(
                "/query",
                json={"query": "Shared cache", "collection_name": "shared_col"},
            )
            self.assertEqual(r_q1.status_code, 200)

            # 2nd query via /agent on agent-api (consumes 2/2 of shared RATE_LIMIT_QUERY_PER_MINUTE)
            r_a2 = self.agent_client.post(
                "/agent",
                json={"query": "Shared cache", "collection_name": "shared_col", "stream": False},
            )
            self.assertEqual(r_a2.status_code, 200)

            # 3rd query via /query on agent-api hits HTTP 429 from the single shared _QUERY_RATE_LIMITER
            r_q3 = self.agent_client.post(
                "/query",
                json={"query": "Shared cache", "collection_name": "shared_col"},
            )
            self.assertEqual(r_q3.status_code, 429)


class TestStorageCacheBoundsAndIncrementalMD5Sync(unittest.TestCase):
    """Verifies configurable /tmp LRU cache bounds and incremental MD5 delta sync to GCS."""

    def setUp(self) -> None:
        self.tmp_root = tempfile.mkdtemp(prefix="scale_cache_sync_")
        self.fake_bucket = FakeGCSBucket("interactive-rag-delta-bucket")

    def tearDown(self) -> None:
        if os.path.exists(self.tmp_root):
            for entry in os.scandir(self.tmp_root):
                if entry.is_dir():
                    release_client_cache(entry.path)
            shutil.rmtree(self.tmp_root, ignore_errors=True)

    def test_configurable_chroma_tmp_cache_quotas_and_lru_eviction(self) -> None:
        """Verifies CHROMA_TMP_MAX_BYTES and CHROMA_TMP_MIN_FREE_BYTES getters and LRU eviction."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CHROMA_TMP_MAX_BYTES", None)
            os.environ.pop("CHROMA_TMP_MIN_FREE_BYTES", None)
            self.assertEqual(config_module.get_chroma_tmp_max_bytes(), 536870912)
            self.assertEqual(config_module.get_chroma_tmp_min_free_bytes(), 268435456)

        with patch.dict(
            os.environ,
            {"CHROMA_TMP_MAX_BYTES": "1024", "CHROMA_TMP_MIN_FREE_BYTES": "0"},
            clear=False,
        ):
            self.assertEqual(config_module.get_chroma_tmp_max_bytes(), 1024)
            self.assertEqual(config_module.get_chroma_tmp_min_free_bytes(), 0)

            # Create 2 tenant directories totaling > 1024 bytes; older tenant must be evicted
            u_old = os.path.join(self.tmp_root, "tenant_old")
            u_new = os.path.join(self.tmp_root, "tenant_new")
            os.makedirs(u_old, exist_ok=True)
            os.makedirs(u_new, exist_ok=True)
            with open(os.path.join(u_old, "segment.bin"), "wb") as fh:
                fh.write(b"x" * 700)
            with open(os.path.join(u_new, "segment.bin"), "wb") as fh:
                fh.write(b"y" * 700)

            now = time.time()
            os.utime(u_old, (now - 120, now - 120))
            os.utime(u_new, (now, now))

            with patch.dict(os.environ, {"LOCAL_CHROMA_ROOT": self.tmp_root}, clear=False):
                evict_tmp_cache(exclude_uid="tenant_new")

            self.assertFalse(os.path.exists(u_old))
            self.assertTrue(os.path.exists(u_new))

    def test_incremental_md5_delta_push_skips_unchanged_blobs_and_deletes_stale_blobs(self) -> None:
        """Verifies _push_user_dir_to_gcs skips unchanged files via MD5 comparison and records span attributes."""
        uid = "delta_tenant"
        env = {
            "GCS_BUCKET_NAME": self.fake_bucket.name,
            "LOCAL_CHROMA_ROOT": self.tmp_root,
        }

        ctx1 = RequestContext(
            request_id="req-delta-1",
            trace_id="105445aa7843bc8bf206b12000100000",
            service_name="ingest-api",
            uid=uid,
        )
        t1 = bind_request_context(ctx1)
        try:
            with patch.dict(os.environ, env, clear=False), patch.object(
                gcs_sync_module, "_get_bucket", return_value=self.fake_bucket
            ):
                with user_write_lock(uid) as user_dir:
                    seg_dir = os.path.join(user_dir, "hnsw_segment_01")
                    os.makedirs(seg_dir, exist_ok=True)
                    sqlite_file = os.path.join(user_dir, "chroma.sqlite3")
                    hnsw_data = os.path.join(seg_dir, "data_level0.bin")
                    hnsw_header = os.path.join(seg_dir, "header.bin")

                    with open(sqlite_file, "wb") as fh:
                        fh.write(b"sqlite-v1-content")
                    with open(hnsw_data, "wb") as fh:
                        fh.write(b"hnsw-graph-vectors-unchanged" * 50)
                    with open(hnsw_header, "wb") as fh:
                        fh.write(b"hnsw-header-unchanged")

                spans_1 = [s for s in get_recorded_spans() if s["name"] == "storage.gcs_push"]
                self.assertEqual(len(spans_1), 1)
                self.assertEqual(spans_1[0]["attributes"]["uploaded_files"], 3)
                self.assertEqual(spans_1[0]["attributes"]["skipped_files"], 0)
                self.assertEqual(spans_1[0]["attributes"]["deleted_files"], 0)
        finally:
            reset_request_context(t1)

        # Record initial generations of the 2 unchanged HNSW files
        gen_data_before = self.fake_bucket.blob(
            f"users/{uid}/chroma_db/hnsw_segment_01/data_level0.bin"
        ).generation
        gen_header_before = self.fake_bucket.blob(
            f"users/{uid}/chroma_db/hnsw_segment_01/header.bin"
        ).generation

        ctx2 = RequestContext(
            request_id="req-delta-2",
            trace_id="205445aa7843bc8bf206b12000100000",
            service_name="ingest-api",
            uid=uid,
        )
        t2 = bind_request_context(ctx2)
        try:
            with patch.dict(os.environ, env, clear=False), patch.object(
                gcs_sync_module, "_get_bucket", return_value=self.fake_bucket
            ):
                with user_write_lock(uid) as user_dir:
                    # Modify ONLY chroma.sqlite3 and remove header.bin; keep data_level0.bin identical
                    sqlite_file = os.path.join(user_dir, "chroma.sqlite3")
                    hnsw_header = os.path.join(user_dir, "hnsw_segment_01", "header.bin")
                    with open(sqlite_file, "wb") as fh:
                        fh.write(b"sqlite-v2-modified-content")
                    os.remove(hnsw_header)

                spans_2 = [s for s in get_recorded_spans() if s["name"] == "storage.gcs_push"]
                self.assertEqual(len(spans_2), 1)
                self.assertEqual(spans_2[0]["attributes"]["uploaded_files"], 1)
                self.assertEqual(spans_2[0]["attributes"]["skipped_files"], 1)
                self.assertEqual(spans_2[0]["attributes"]["deleted_files"], 1)
        finally:
            reset_request_context(t2)

        # Unchanged HNSW data file was NOT re-uploaded (same GCS generation!)
        gen_data_after = self.fake_bucket.blob(
            f"users/{uid}/chroma_db/hnsw_segment_01/data_level0.bin"
        ).generation
        self.assertEqual(gen_data_before, gen_data_after)
        self.assertIsNotNone(gen_header_before)
        self.assertFalse(
            self.fake_bucket.blob(f"users/{uid}/chroma_db/hnsw_segment_01/header.bin").exists()
        )
        version_doc = json.loads(
            self.fake_bucket.blob(f"users/{uid}/chroma_db/version.json").download_as_text()
        )
        self.assertIn("version", version_doc)


class TestTerraformAndStaticScalingPolicies(unittest.TestCase):
    """Verifies Terraform scaling, concurrency, subnet, and monitoring definitions."""

    def test_terraform_scaling_concurrency_subnet_and_monitoring_definitions(self) -> None:
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        ok, violations = evaluate_repo_static_policies(repo_root)
        self.assertTrue(ok, f"Expected static policy check to pass, got: {violations}")
        self.assertEqual(violations, [])

        infra_dir = os.path.join(repo_root, "infra")
        with open(os.path.join(infra_dir, "variables.tf"), encoding="utf-8") as fh:
            vars_tf = fh.read()
        for var_name in (
            "ingest_concurrency",
            "agent_concurrency",
            "frontend_concurrency",
            "chroma_tmp_max_bytes",
            "chroma_tmp_min_free_bytes",
        ):
            self.assertIn(f'variable "{var_name}"', vars_tf)

        with open(os.path.join(infra_dir, "cloud_run.tf"), encoding="utf-8") as fh:
            cr_tf = fh.read()
        self.assertIn("max_instance_request_concurrency = var.ingest_concurrency", cr_tf)
        self.assertIn("max_instance_request_concurrency = var.agent_concurrency", cr_tf)
        self.assertIn("max_instance_request_concurrency = var.frontend_concurrency", cr_tf)
        self.assertEqual(cr_tf.count('name  = "CHROMA_TMP_MAX_BYTES"'), 2)
        self.assertEqual(cr_tf.count('name  = "CHROMA_TMP_MIN_FREE_BYTES"'), 2)

        with open(os.path.join(infra_dir, "observability.tf"), encoding="utf-8") as fh:
            obs_tf = fh.read()
        for alert_name in (
            "agent_high_latency_alert",
            "cloud_run_memory_saturation_alert",
            "cloud_run_5xx_error_rate_alert",
        ):
            self.assertIn(f'resource "google_monitoring_alert_policy" "{alert_name}"', obs_tf)
        for widget_title in (
            "9. Cloud Run Active Container Instances by Service (Horizontal Scaling)",
            "10. Cloud Run CPU & Memory (+ /tmp tmpfs) Utilization (p95)",
            "11. Cloud Run Max Concurrent Requests per Instance (p95)",
            "12. Cloud Run End-to-End Request Latencies by Service (p50 / p95)",
        ):
            self.assertIn(widget_title, obs_tf)


if __name__ == "__main__":
    unittest.main(verbosity=2)
