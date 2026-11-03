"""Automated offline unit test suite for Multi-Tenant API Isolation (`ingest-api` & `agent-api`).

Verifies:
- Public `/health` probes vs JWT-protected API routes across both backend services.
- Cloud Run rejection of arbitrary server-side `pdf_paths` (`HTTP 403`).
- End-to-end tenant isolation across PDF upload archiving, vector search, collection
  management, SSE streaming, and LangGraph `<uid>:<thread_id>` memory checkpointing.
"""

import os
import shutil
import tempfile
from typing import Dict
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from src.agent import app as agent_app_module
from src.agent import graph as agent_graph_module
from src.core.compliance.policy import reset_rate_limiters
from src.core.logger import setup_logging
from src.core.storage import chroma as chroma_module
from src.core.storage import gcs_sync as gcs_sync_module
from src.ingest import app as ingest_app_module
from tests.helpers import (
    DeterministicFakeEmbeddings,
    FakeGCSBucket,
    GroundedCitingChatModel,
    cleanup_chroma_tmpdir,
    make_test_pdf,
    parse_sse_frames,
    reset_memory_saver,
)

logger = setup_logging(name="tests.multi_tenant_api")

FAKE_TOKENS: Dict[str, Dict[str, str]] = {
    "token-alice": {"uid": "user_alice", "email": "alice@example.com", "name": "Alice"},
    "token-bob": {"uid": "user_bob", "email": "bob@example.com", "name": "Bob"},
}


def fake_verify_bearer_token(token: str) -> Dict[str, str]:
    """Deterministic JWT verification double mapping `token-alice` and `token-bob` to tenant claims."""
    if token not in FAKE_TOKENS:
        raise HTTPException(status_code=401, detail="Invalid authentication token.")
    return FAKE_TOKENS[token]


class TestMultiTenantBackendAPIs(unittest.TestCase):
    """End-to-end multi-tenant isolation tests across `ingest-api` and `agent-api`."""

    def setUp(self) -> None:
        self.ingestion_tmp = tempfile.mkdtemp(prefix="ingest_chroma_")
        self.agent_tmp = tempfile.mkdtemp(prefix="agent_chroma_")
        self.uploads_tmp = tempfile.mkdtemp(prefix="uploads_tmp_")
        self.fake_bucket = FakeGCSBucket("interactive-rag-mt-bucket")
        reset_memory_saver()
        reset_rate_limiters()

        self.ingest_client = TestClient(ingest_app_module.app)
        self.agent_client = TestClient(agent_app_module.app)

    def tearDown(self) -> None:
        cleanup_chroma_tmpdir(self.ingestion_tmp, self.agent_tmp)
        shutil.rmtree(self.uploads_tmp, ignore_errors=True)
        reset_memory_saver()
        reset_rate_limiters()

    def _base_env(self, chroma_root: str, **extra: str) -> Dict[str, str]:
        env = {
            "AUTH_DISABLED": "false",
            "K_SERVICE": "interactive-rag-prod",
            "GOOGLE_CLOUD_PROJECT": "interactive-rag",
            "ALLOWED_LLM_MODELS": "gemini-2.5-flash,gemini-2.5-pro,gemini-2.5-flash-lite",
            "ALLOWED_EMBEDDING_MODELS": "gemini-embedding-001,text-embedding-005",
            "GCS_BUCKET_NAME": self.fake_bucket.name,
            "LOCAL_CHROMA_ROOT": chroma_root,
            "UPLOAD_TMP_ROOT": self.uploads_tmp,
            "MODEL_ARMOR_TEMPLATE_ID": "",
            "MODEL_ARMOR_FAIL_CLOSED": "false",
        }
        env.update(extra)
        return env

    def test_01_health_endpoints_are_public_and_api_endpoints_require_jwt(self) -> None:
        """Verifies `/health` is unauthenticated on both services while all data routes enforce Bearer JWTs."""
        with patch.dict(os.environ, self._base_env(self.ingestion_tmp), clear=False):
            r_health_1 = self.ingest_client.get("/health")
            r_health_2 = self.agent_client.get("/health")
            self.assertEqual(r_health_1.status_code, 200)
            self.assertEqual(r_health_2.status_code, 200)
            self.assertEqual(r_health_1.json()["status"], "ok")
            self.assertEqual(r_health_2.json()["status"], "ok")

            # ingest-api mounts ONLY POST /ingest (+ GET /health)
            self.assertEqual(self.ingest_client.post("/ingest", json={"pdf_paths": ["a.pdf"]}).status_code, 401)
            self.assertEqual(self.ingest_client.get("/collections").status_code, 404)
            self.assertEqual(self.ingest_client.delete("/collections/secret").status_code, 404)
            self.assertEqual(self.ingest_client.post("/query", json={"query": "hi"}).status_code, 404)

            # agent-api mounts /collections, /query, /compliance/my-data, /agent, /agent/stream (+ GET /health)
            self.assertEqual(self.agent_client.get("/collections").status_code, 401)
            self.assertEqual(self.agent_client.delete("/collections/secret").status_code, 401)
            self.assertEqual(self.agent_client.post("/query", json={"query": "hi"}).status_code, 401)
            self.assertEqual(self.agent_client.post("/agent", json={"query": "hi"}).status_code, 401)
            self.assertEqual(self.agent_client.post("/agent/stream", json={"query": "hi"}).status_code, 401)

    def test_02_cloud_run_blocks_arbitrary_server_pdf_paths(self) -> None:
        """Verifies `pdf_paths` ingestion is rejected with HTTP 403 when `K_SERVICE` is set (Cloud Run)."""
        with patch("src.core.auth.verify_bearer_token", side_effect=fake_verify_bearer_token), patch.object(
            gcs_sync_module, "_get_bucket", return_value=self.fake_bucket
        ), patch.dict(os.environ, self._base_env(self.ingestion_tmp), clear=False):
            headers = {"Authorization": "Bearer token-alice"}

            res_json = self.ingest_client.post(
                "/ingest",
                headers=headers,
                json={"pdf_paths": ["/etc/passwd"], "collection_name": "hack"},
            )
            self.assertEqual(res_json.status_code, 403)

            res_form = self.ingest_client.post(
                "/ingest",
                headers=headers,
                data={"pdf_paths": "./data/Stock_Market_Performance_2024.pdf", "collection_name": "hack"},
            )
            self.assertEqual(res_form.status_code, 403)

    def test_03_end_to_end_multi_tenant_isolation_across_ingestion_and_agent_services(self) -> None:
        """Verifies strict tenant isolation across PDF uploads, GCS sync, `/query`, `/collections`, `/agent`, and memory threads."""
        alice_headers = {"Authorization": "Bearer token-alice"}
        bob_headers = {"Authorization": "Bearer token-bob"}

        with patch("src.core.auth.verify_bearer_token", side_effect=fake_verify_bearer_token), patch.object(
            gcs_sync_module, "_get_bucket", return_value=self.fake_bucket
        ), patch.object(
            chroma_module, "GoogleGenerativeAIEmbeddings", return_value=DeterministicFakeEmbeddings()
        ), patch.object(
            agent_graph_module, "ChatGoogleGenerativeAI", side_effect=GroundedCitingChatModel
        ):
            # 1. Alice uploads a confidential PDF on Container 1 (ingest-api)
            with patch.dict(os.environ, self._base_env(self.ingestion_tmp), clear=False):
                alice_pdf = make_test_pdf("Alice confidential acquisition target is Project Helios.")
                res_ingest = self.ingest_client.post(
                    "/ingest",
                    headers=alice_headers,
                    data={"collection_name": "confidential_a"},
                    files=[("files", ("../../alice_secret.pdf", alice_pdf, "application/pdf"))],
                )
                self.assertEqual(res_ingest.status_code, 200, res_ingest.text)
                body = res_ingest.json()
                self.assertEqual(body["collection_name"], "confidential_a")
                self.assertEqual(body["processed_files"], ["alice_secret.pdf"])

                alice_upload_dir = os.path.join(self.uploads_tmp, "user_alice")
                self.assertFalse(os.path.exists(alice_upload_dir), "Temp upload dir must be deleted in finally block")

                archived_blobs = [
                    b.name for b in self.fake_bucket.list_blobs("users/user_alice/uploads/")
                ]
                self.assertEqual(len(archived_blobs), 1)

                res_query_a = self.agent_client.post(
                    "/query",
                    headers=alice_headers,
                    json={"query": "Alice acquisition", "collection_name": "confidential_a", "k": 3},
                )
                self.assertEqual(res_query_a.status_code, 200)
                results = res_query_a.json()["results"]
                self.assertEqual(len(results), 1)
                self.assertIn("Project Helios", results[0]["page_content"])
                self.assertEqual(results[0]["metadata"]["source"], "alice_secret.pdf")

            # 2. Bob connects to agent-api -> sees ZERO collections and gets 404 on Alice's collection
            with patch.dict(os.environ, self._base_env(self.ingestion_tmp), clear=False):
                res_bob_cols = self.agent_client.get("/collections", headers=bob_headers)
                self.assertEqual(res_bob_cols.status_code, 200)
                self.assertEqual(res_bob_cols.json()["collections"], [])

                bob_pdf = make_test_pdf("Bob public travel itinerary for Rome.")
                res_bob_ingest = self.ingest_client.post(
                    "/ingest",
                    headers=bob_headers,
                    data={"collection_name": "bob_notes"},
                    files=[("files", ("bob_notes.pdf", bob_pdf, "application/pdf"))],
                )
                self.assertEqual(res_bob_ingest.status_code, 200)

                self.assertEqual(
                    self.agent_client.post(
                        "/query",
                        headers=bob_headers,
                        json={"query": "Project Helios", "collection_name": "confidential_a"},
                    ).status_code,
                    404,
                )
                self.assertEqual(
                    self.agent_client.delete(
                        "/collections/confidential_a", headers=bob_headers
                    ).status_code,
                    404,
                )

            # 3. Cross-service sync on Container 2 (agent-api, completely separate `/tmp` directory!)
            with patch.dict(os.environ, self._base_env(self.agent_tmp), clear=False):
                self.assertEqual(
                    self.agent_client.post(
                        "/agent",
                        headers=bob_headers,
                        json={"query": "What is Project Helios?", "collection_name": "confidential_a", "stream": False},
                    ).status_code,
                    404,
                )
                self.assertEqual(
                    self.agent_client.post(
                        "/agent",
                        headers=bob_headers,
                        json={"query": "What is Project Helios?", "collection_name": "confidential_a", "stream": True},
                    ).status_code,
                    404,
                )

                res_sse = self.agent_client.post(
                    "/agent",
                    headers=alice_headers,
                    json={
                        "query": "What is Alice confidential target?",
                        "collection_name": "confidential_a",
                        "thread_id": "shared-thread",
                        "stream": True,
                    },
                )
                self.assertEqual(res_sse.status_code, 200)
                self.assertEqual(res_sse.headers.get("cache-control"), "no-cache")
                self.assertEqual(res_sse.headers.get("x-accel-buffering"), "no")

                sse_payloads = parse_sse_frames(res_sse.text)
                self.assertTrue(
                    any("Project Helios" in p.get("message", {}).get("content", "") for p in sse_payloads)
                )

                res_bob_agent = self.agent_client.post(
                    "/agent",
                    headers=bob_headers,
                    json={
                        "query": "Where is Bob traveling?",
                        "collection_name": "bob_notes",
                        "thread_id": "shared-thread",
                        "stream": False,
                    },
                )
                self.assertEqual(res_bob_agent.status_code, 200)
                bob_answer = res_bob_agent.json()["answer"]
                self.assertIn("Rome", bob_answer)
                self.assertNotIn("Helios", bob_answer)

                checkpoint_threads = set(agent_graph_module.MEMORY_SAVER.storage.keys())
                self.assertIn("user_alice:shared-thread", checkpoint_threads)
                self.assertIn("user_bob:shared-thread", checkpoint_threads)
                self.assertNotIn("shared-thread", checkpoint_threads)


if __name__ == "__main__":
    logger.info("Running Multi-Tenant Backend API security & isolation unit test suite...")
    unittest.main(verbosity=2)
