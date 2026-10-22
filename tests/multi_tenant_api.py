import json
import os
import shutil
import tempfile
import unittest
from typing import Dict, List
from unittest.mock import patch

from fastapi.testclient import TestClient
from langchain_core.embeddings import Embeddings
from langchain_core.messages import AIMessage

from src import add_documents as ingestion_module
from src import agentic_rag as agent_module
from tests.chroma_storage import FakeGCSBucket
from utils import chroma_storage
from utils.chroma_storage import release_chroma_client_cache
from utils.logging import setup_logging

logger = setup_logging(name="test_multi_tenant_api")


def make_test_pdf(text: str) -> bytes:
    """Builds a minimal valid single-page PDF 1.4 document containing `text`."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("latin-1")
    objs = [
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n",
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n",
        b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>\nendobj\n",
        f"4 0 obj\n<< /Length {len(stream)} >>\nstream\n".encode("latin-1") + stream + b"\nendstream\nendobj\n",
        b"5 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for obj in objs:
        offsets.append(len(out))
        out.extend(obj)
    xref_pos = len(out)
    out.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode("latin-1"))
    for off in offsets[1:]:
        out.extend(f"{off:010d} 00000 n \n".encode("latin-1"))
    out.extend(
        f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n".encode("latin-1")
    )
    return bytes(out)


class DeterministicFakeEmbeddings(Embeddings):
    """Deterministic local embeddings double so Chroma HNSW indexing works offline without Vertex AI calls."""

    def _vec(self, text: str) -> List[float]:
        lower = text.lower()
        return [
            1.0 if "alice" in lower else 0.1,
            1.0 if "bob" in lower else 0.1,
            float((sum(ord(c) for c in text) % 97) + 1) / 100.0,
            0.5,
        ]

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        return [self._vec(t) for t in texts]

    def embed_query(self, text: str) -> List[float]:
        return self._vec(text)


class FakeToolCallingChatModel:
    """Deterministic LLM double that invokes the bound retriever tool once and summarizes the retrieved text."""

    def __init__(self, *args, **kwargs):
        self._tool_name = "retriever"

    def bind_tools(self, tools):
        if tools:
            self._tool_name = tools[0].name
        return self

    def invoke(self, messages):
        last = messages[-1]
        if getattr(last, "type", "") == "tool":
            return AIMessage(content=f"Grounded answer from tool: {last.content}")
        query_text = getattr(last, "content", "query")
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "id": "call_1",
                    "name": self._tool_name,
                    "args": {"query": str(query_text)},
                }
            ],
        )


FAKE_TOKENS: Dict[str, Dict[str, str]] = {
    "token-alice": {"uid": "user_alice", "email": "alice@example.com", "name": "Alice"},
    "token-bob": {"uid": "user_bob", "email": "bob@example.com", "name": "Bob"},
}


def fake_verify_bearer_token(token: str) -> Dict[str, str]:
    from fastapi import HTTPException

    if token not in FAKE_TOKENS:
        raise HTTPException(status_code=401, detail="Invalid authentication token.")
    return FAKE_TOKENS[token]


class TestMultiTenantBackendAPIs(unittest.TestCase):
    def setUp(self):
        self.ingestion_tmp = tempfile.mkdtemp(prefix="ingest_chroma_")
        self.agent_tmp = tempfile.mkdtemp(prefix="agent_chroma_")
        self.uploads_tmp = tempfile.mkdtemp(prefix="uploads_tmp_")
        self.fake_bucket = FakeGCSBucket("interactive-rag-mt-bucket")
        agent_module._MEMORY_SAVER.storage.clear()

        self.ingestion_client = TestClient(ingestion_module.app)
        self.agent_client = TestClient(agent_module.app)

    def tearDown(self):
        for root in (self.ingestion_tmp, self.agent_tmp):
            if os.path.exists(root):
                for entry in os.scandir(root):
                    if entry.is_dir():
                        release_chroma_client_cache(entry.path)
                shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(self.uploads_tmp, ignore_errors=True)

    def _base_env(self, chroma_root: str, **extra) -> Dict[str, str]:
        env = {
            "AUTH_DISABLED": "false",
            "K_SERVICE": "interactive-rag-prod",
            "GOOGLE_CLOUD_PROJECT": "interactive-rag",
            "GCS_BUCKET_NAME": self.fake_bucket.name,
            "LOCAL_CHROMA_ROOT": chroma_root,
            "UPLOAD_TMP_ROOT": self.uploads_tmp,
        }
        env.update(extra)
        return env

    def test_01_health_endpoints_are_public_and_api_endpoints_require_jwt(self):
        with patch.dict(os.environ, self._base_env(self.ingestion_tmp), clear=False):
            # Health probes succeed without Authorization header
            r_health_1 = self.ingestion_client.get("/health")
            r_health_2 = self.agent_client.get("/health")
            self.assertEqual(r_health_1.status_code, 200)
            self.assertEqual(r_health_2.status_code, 200)
            self.assertEqual(r_health_1.json()["status"], "ok")
            self.assertEqual(r_health_2.json()["status"], "ok")

            # Every protected endpoint rejects unauthenticated requests with 401
            self.assertEqual(self.ingestion_client.get("/collections").status_code, 401)
            self.assertEqual(self.ingestion_client.delete("/collections/secret").status_code, 401)
            self.assertEqual(self.ingestion_client.post("/add_documents", json={"pdf_paths": ["a.pdf"]}).status_code, 401)
            self.assertEqual(self.ingestion_client.post("/query", json={"query": "hi"}).status_code, 401)

            self.assertEqual(self.agent_client.get("/collections").status_code, 401)
            self.assertEqual(self.agent_client.delete("/collections/secret").status_code, 401)
            self.assertEqual(self.agent_client.post("/agentic_rag", json={"query": "hi"}).status_code, 401)
            self.assertEqual(self.agent_client.post("/agentic_rag/stream", json={"query": "hi"}).status_code, 401)

    def test_02_cloud_run_blocks_arbitrary_server_pdf_paths(self):
        with patch("utils.auth.verify_bearer_token", side_effect=fake_verify_bearer_token), patch.object(
            chroma_storage, "_get_bucket", return_value=self.fake_bucket
        ), patch.dict(os.environ, self._base_env(self.ingestion_tmp), clear=False):
            headers = {"Authorization": "Bearer token-alice"}

            # JSON pdf_paths attempt in Cloud Run -> 403 Forbidden
            res_json = self.ingestion_client.post(
                "/add_documents",
                headers=headers,
                json={"pdf_paths": ["/etc/passwd"], "collection_name": "hack"},
            )
            self.assertEqual(res_json.status_code, 403)

            # Multipart form pdf_paths attempt in Cloud Run -> 403 Forbidden
            res_form = self.ingestion_client.post(
                "/add_documents",
                headers=headers,
                data={"pdf_paths": "./data/Stock_Market_Performance_2024.pdf", "collection_name": "hack"},
            )
            self.assertEqual(res_form.status_code, 403)

    def test_03_end_to_end_multi_tenant_isolation_across_ingestion_and_agent_services(self):
        alice_headers = {"Authorization": "Bearer token-alice"}
        bob_headers = {"Authorization": "Bearer token-bob"}

        with patch("utils.auth.verify_bearer_token", side_effect=fake_verify_bearer_token), patch.object(
            chroma_storage, "_get_bucket", return_value=self.fake_bucket
        ), patch.object(
            ingestion_module, "GoogleGenerativeAIEmbeddings", return_value=DeterministicFakeEmbeddings()
        ), patch.object(
            agent_module, "GoogleGenerativeAIEmbeddings", return_value=DeterministicFakeEmbeddings()
        ), patch.object(
            agent_module, "ChatGoogleGenerativeAI", side_effect=FakeToolCallingChatModel
        ):
            # 1. Alice uploads a confidential PDF on Container 1 (ingestion-api)
            with patch.dict(os.environ, self._base_env(self.ingestion_tmp), clear=False):
                alice_pdf = make_test_pdf("Alice confidential acquisition target is Project Helios.")
                res_ingest = self.ingestion_client.post(
                    "/add_documents",
                    headers=alice_headers,
                    data={"collection_name": "confidential_a"},
                    files=[("files", ("../../alice_secret.pdf", alice_pdf, "application/pdf"))],
                )
                self.assertEqual(res_ingest.status_code, 200, res_ingest.text)
                body = res_ingest.json()
                self.assertEqual(body["collection_name"], "confidential_a")
                # Path traversal stripped from filename
                self.assertEqual(body["processed_files"], ["alice_secret.pdf"])

                # Per-request temp directory under /tmp/uploads/user_alice must be cleaned up!
                alice_upload_dir = os.path.join(self.uploads_tmp, "user_alice")
                self.assertFalse(os.path.exists(alice_upload_dir), "Temp upload dir must be deleted in finally block")

                # Raw PDF archived under gs://<bucket>/users/user_alice/uploads/
                archived_blobs = [
                    b.name for b in self.fake_bucket.list_blobs("users/user_alice/uploads/")
                ]
                self.assertEqual(len(archived_blobs), 1)

                # Alice queries her collection -> retrieves her confidential chunk with clean metadata
                res_query_a = self.ingestion_client.post(
                    "/query",
                    headers=alice_headers,
                    json={"query": "Alice acquisition", "collection_name": "confidential_a", "k": 3},
                )
                self.assertEqual(res_query_a.status_code, 200)
                results = res_query_a.json()["results"]
                self.assertEqual(len(results), 1)
                self.assertIn("Project Helios", results[0]["page_content"])
                self.assertEqual(results[0]["metadata"]["source"], "alice_secret.pdf")

            # 2. Bob connects to ingestion-api -> sees ZERO collections and gets 404 on Alice's collection
            with patch.dict(os.environ, self._base_env(self.ingestion_tmp), clear=False):
                res_bob_cols = self.ingestion_client.get("/collections", headers=bob_headers)
                self.assertEqual(res_bob_cols.status_code, 200)
                self.assertEqual(res_bob_cols.json()["collections"], [])

                # Bob uploads his own collection so Bob DOES have a Chroma SQLite DB
                bob_pdf = make_test_pdf("Bob public travel itinerary for Rome.")
                res_bob_ingest = self.ingestion_client.post(
                    "/add_documents",
                    headers=bob_headers,
                    data={"collection_name": "bob_notes"},
                    files=[("files", ("bob_notes.pdf", bob_pdf, "application/pdf"))],
                )
                self.assertEqual(res_bob_ingest.status_code, 200)

                # Even with his own DB initialized, Bob gets 404 querying or deleting Alice's `confidential_a`!
                self.assertEqual(
                    self.ingestion_client.post(
                        "/query",
                        headers=bob_headers,
                        json={"query": "Project Helios", "collection_name": "confidential_a"},
                    ).status_code,
                    404,
                )
                self.assertEqual(
                    self.ingestion_client.delete(
                        "/collections/confidential_a", headers=bob_headers
                    ).status_code,
                    404,
                )

            # 3. Cross-service sync on Container 2 (agent-api, completely separate `/tmp` directory!)
            with patch.dict(os.environ, self._base_env(self.agent_tmp), clear=False):
                # Bob gets 404 on both standard and SSE streaming agent calls against Alice's collection
                self.assertEqual(
                    self.agent_client.post(
                        "/agentic_rag",
                        headers=bob_headers,
                        json={"query": "What is Project Helios?", "collection_name": "confidential_a", "stream": False},
                    ).status_code,
                    404,
                )
                self.assertEqual(
                    self.agent_client.post(
                        "/agentic_rag",
                        headers=bob_headers,
                        json={"query": "What is Project Helios?", "collection_name": "confidential_a", "stream": True},
                    ).status_code,
                    404,
                )

                # Alice queries agent-api using SSE streaming -> syncs from GCS and streams grounded answer!
                res_sse = self.agent_client.post(
                    "/agentic_rag",
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

                sse_payloads = []
                for line in res_sse.text.splitlines():
                    if line.startswith("data: ") and line[6:].strip() != "[DONE]":
                        sse_payloads.append(json.loads(line[6:]))
                self.assertTrue(any("Project Helios" in p["message"].get("content", "") for p in sse_payloads))

                # Bob uses the EXACT same client `thread_id="shared-thread"` on his own collection ->
                # verify LangGraph checkpointer namespaces keys as `user_alice:shared-thread` vs `user_bob:shared-thread`!
                res_bob_agent = self.agent_client.post(
                    "/agentic_rag",
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

                checkpoint_threads = set(agent_module._MEMORY_SAVER.storage.keys())
                self.assertIn("user_alice:shared-thread", checkpoint_threads)
                self.assertIn("user_bob:shared-thread", checkpoint_threads)
                self.assertNotIn("shared-thread", checkpoint_threads)


if __name__ == "__main__":
    logger.info("Running Phase 4 Multi-Tenant Backend API security & isolation test suite...")
    unittest.main(verbosity=2)
