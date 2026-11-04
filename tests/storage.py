"""Automated offline unit test suite for Per-User GCS <-> ChromaDB Storage (`src.core.storage`).

Verifies:
- Strict per-tenant directory and GCS prefix isolation (`users/<uid>/chroma_db/`).
- Cross-service `version.json` invalidation and Chroma `PersistentClient` cache refresh.
- Remote GCS blob cleanup upon collection deletion.
- Distributed GCS write-lock TTL recovery (`users/<uid>/locks/chroma.lock`) and contention timeouts.
- `/tmp` LRU cache eviction preserving actively locked readers.
"""

import json
import os
import tempfile
import time
from typing import Dict
import unittest
from unittest.mock import patch

import chromadb
from langchain_chroma import Chroma
from langchain_core.documents import Document

from src.core.logger import setup_logging
from src.core.storage import (
    LockTimeoutError,
    evict_tmp_cache,
    gcs_sync as gcs_sync_module,
    similarity_search_with_scores,
    sync_user_dir,
    user_read_lock,
    user_write_lock,
)
from tests.helpers import (
    DeterministicFakeEmbeddings,
    FakeGCSBlob,
    FakeGCSBucket,
    cleanup_chroma_tmpdir,
)

logger = setup_logging(name="tests.storage")


class TestPerUserChromaStorage(unittest.TestCase):
    """Unit tests for per-user GCS synchronization, distributed locking, and `/tmp` LRU cache eviction."""

    def setUp(self) -> None:
        self.container1_tmp = tempfile.mkdtemp(prefix="chroma_c1_")
        self.container2_tmp = tempfile.mkdtemp(prefix="chroma_c2_")
        self.fake_bucket = FakeGCSBucket("interactive-rag-chroma-test")

    def tearDown(self) -> None:
        cleanup_chroma_tmpdir(self.container1_tmp, self.container2_tmp)

    def _env(self, local_root: str, **overrides: str) -> Dict[str, str]:
        base = {
            "GCS_BUCKET_NAME": self.fake_bucket.name,
            "LOCAL_CHROMA_ROOT": local_root,
            "CHROMA_TMP_MAX_BYTES": str(512 * 1024 * 1024),
            "CHROMA_TMP_MIN_FREE_BYTES": "0",
            "CHROMA_LOCK_TTL_SECONDS": "300",
            "CHROMA_LOCK_TIMEOUT_SECONDS": "5",
        }
        base.update(overrides)
        return base

    def test_01_strict_per_user_isolation_same_collection_name(self) -> None:
        """Verifies two users creating identically named collections stay completely isolated across containers."""
        with patch.object(gcs_sync_module, "_get_bucket", return_value=self.fake_bucket), patch.dict(
            os.environ, self._env(self.container1_tmp), clear=False
        ):
            # Alice creates 'stock_market_vs' with 2 documents
            with user_write_lock("user_alice") as alice_dir:
                client_a = chromadb.PersistentClient(path=alice_dir)
                col_a = client_a.get_or_create_collection("stock_market_vs")
                col_a.add(
                    ids=["a1", "a2"],
                    documents=["Alice secret Q1 report", "Alice secret Q2 report"],
                    embeddings=[[1.0, 0.0, 0.0], [0.9, 0.1, 0.0]],
                )

            # Bob creates 'stock_market_vs' with 1 completely different document
            with user_write_lock("user_bob") as bob_dir:
                client_b = chromadb.PersistentClient(path=bob_dir)
                col_b = client_b.get_or_create_collection("stock_market_vs")
                col_b.add(
                    ids=["b1"],
                    documents=["Bob private portfolio"],
                    embeddings=[[0.0, 1.0, 0.0]],
                )

        # Verify isolated reads on a completely fresh container (container 2)
        with patch.object(gcs_sync_module, "_get_bucket", return_value=self.fake_bucket), patch.dict(
            os.environ, self._env(self.container2_tmp), clear=False
        ):
            alice_read_dir = sync_user_dir("user_alice")
            bob_read_dir = sync_user_dir("user_bob")
            charlie_read_dir = sync_user_dir("user_charlie")

            self.assertFalse(os.path.exists(charlie_read_dir), "New user should have no DB directory yet")

            alice_col = chromadb.PersistentClient(path=alice_read_dir).get_collection("stock_market_vs")
            bob_col = chromadb.PersistentClient(path=bob_read_dir).get_collection("stock_market_vs")

            self.assertEqual(alice_col.count(), 2)
            self.assertEqual(bob_col.count(), 1)
            self.assertEqual(bob_col.get()["documents"], ["Bob private portfolio"])

    def test_02_cross_service_version_invalidation_refreshes_cached_chroma_client(self) -> None:
        """Verifies remote `version.json` increments invalidate stale Chroma client caches on other containers."""
        # Step 1: Container 1 (ingest-api) writes 1 doc
        with patch.object(gcs_sync_module, "_get_bucket", return_value=self.fake_bucket), patch.dict(
            os.environ, self._env(self.container1_tmp), clear=False
        ):
            with user_write_lock("user_alice") as dir_c1:
                col = chromadb.PersistentClient(path=dir_c1).get_or_create_collection("reports")
                col.add(ids=["1"], documents=["Doc 1"], embeddings=[[0.1, 0.2, 0.3]])

        # Step 2: Container 2 (agent-api) syncs and opens PersistentClient (populates Chroma internal cache)
        with patch.object(gcs_sync_module, "_get_bucket", return_value=self.fake_bucket), patch.dict(
            os.environ, self._env(self.container2_tmp), clear=False
        ):
            dir_c2 = sync_user_dir("user_alice")
            self.assertEqual(chromadb.PersistentClient(path=dir_c2).get_collection("reports").count(), 1)

        # Step 3: Container 1 adds a second doc -> bumps version.json in GCS
        with patch.object(gcs_sync_module, "_get_bucket", return_value=self.fake_bucket), patch.dict(
            os.environ, self._env(self.container1_tmp), clear=False
        ):
            with user_write_lock("user_alice") as dir_c1:
                col = chromadb.PersistentClient(path=dir_c1).get_collection("reports")
                col.add(ids=["2"], documents=["Doc 2"], embeddings=[[0.4, 0.5, 0.6]])

        # Step 4: Container 2 syncs again -> detects version bump, invalidates Chroma system cache, sees 2 docs!
        with patch.object(gcs_sync_module, "_get_bucket", return_value=self.fake_bucket), patch.dict(
            os.environ, self._env(self.container2_tmp), clear=False
        ):
            dir_c2_refreshed = sync_user_dir("user_alice")
            self.assertEqual(
                chromadb.PersistentClient(path=dir_c2_refreshed).get_collection("reports").count(),
                2,
            )

    def test_03_deleted_collection_cleans_up_remote_gcs_blobs(self) -> None:
        """Verifies deleting a Chroma collection removes stale segment files from GCS on write-lock exit."""
        with patch.object(gcs_sync_module, "_get_bucket", return_value=self.fake_bucket), patch.dict(
            os.environ, self._env(self.container1_tmp), clear=False
        ):
            with user_write_lock("user_alice") as alice_dir:
                client = chromadb.PersistentClient(path=alice_dir)
                c1 = client.create_collection("keep_me")
                c1.add(ids=["1"], documents=["Keep"], embeddings=[[1.0, 0.0, 0.0]])
                c2 = client.create_collection("delete_me")
                c2.add(ids=["2"], documents=["Drop"], embeddings=[[0.0, 1.0, 0.0]])

            blobs_before = [b.name for b in self.fake_bucket.list_blobs("users/user_alice/chroma_db/")]

            with user_write_lock("user_alice") as alice_dir:
                client = chromadb.PersistentClient(path=alice_dir)
                client.delete_collection("delete_me")

            blobs_after = [b.name for b in self.fake_bucket.list_blobs("users/user_alice/chroma_db/")]
            self.assertLessEqual(len(blobs_after), len(blobs_before))

        # Fresh container only sees 'keep_me'
        with patch.object(gcs_sync_module, "_get_bucket", return_value=self.fake_bucket), patch.dict(
            os.environ, self._env(self.container2_tmp), clear=False
        ):
            restored_dir = sync_user_dir("user_alice")
            cols = [c.name for c in chromadb.PersistentClient(path=restored_dir).list_collections()]
            self.assertEqual(cols, ["keep_me"])

    def test_04_stale_gcs_lock_ttl_recovery_and_active_lock_contention(self) -> None:
        """Verifies expired GCS write locks are reclaimed automatically while active locks raise `LockTimeoutError`."""
        expired_lock = json.dumps(
            {
                "uid": "user_alice",
                "owner": "crashed-revision:999",
                "acquired_at": time.time() - 900,
                "expires_at": time.time() - 600,
            }
        )
        self.fake_bucket.blob("users/user_alice/locks/chroma.lock").upload_from_string(
            expired_lock, if_generation_match=0
        )

        with patch.object(gcs_sync_module, "_get_bucket", return_value=self.fake_bucket), patch.dict(
            os.environ, self._env(self.container1_tmp), clear=False
        ):
            with user_write_lock("user_alice") as alice_dir:
                chromadb.PersistentClient(path=alice_dir).create_collection("recovered_col")

            self.assertFalse(
                self.fake_bucket.blob("users/user_alice/locks/chroma.lock").exists(),
                "Lock must be deleted after write context exits",
            )

        active_lock = json.dumps(
            {
                "uid": "user_bob",
                "owner": "active-revision:111",
                "acquired_at": time.time(),
                "expires_at": time.time() + 300,
            }
        )
        self.fake_bucket.blob("users/user_bob/locks/chroma.lock").upload_from_string(
            active_lock, if_generation_match=0
        )

        with patch.object(gcs_sync_module, "_get_bucket", return_value=self.fake_bucket), patch.dict(
            os.environ,
            self._env(self.container1_tmp, CHROMA_LOCK_TIMEOUT_SECONDS="0.4"),
            clear=False,
        ):
            with self.assertRaises(LockTimeoutError):
                with user_write_lock("user_bob"):
                    pass

    def test_05_lru_tmp_eviction_preserves_active_users_and_evicts_oldest_inactive(self) -> None:
        """Verifies `evict_tmp_cache` skips active read-locked tenants and evicts the oldest inactive tenant first."""
        with patch.object(gcs_sync_module, "_get_bucket", return_value=self.fake_bucket), patch.dict(
            os.environ, self._env(self.container1_tmp), clear=False
        ):
            for uid in ("u_oldest", "u_middle", "u_newest"):
                with user_write_lock(uid) as udir:
                    col = chromadb.PersistentClient(path=udir).create_collection("notes")
                    col.add(ids=["1"], documents=[f"Data for {uid}"], embeddings=[[0.2, 0.3, 0.5]])
                time.sleep(0.02)

            with user_read_lock("u_oldest"):
                with patch.dict(os.environ, {"CHROMA_TMP_MAX_BYTES": "1"}, clear=False):
                    evicted_while_pinned = evict_tmp_cache(exclude_uid="u_newest")
                    self.assertNotIn("u_oldest", evicted_while_pinned)
                    self.assertIn("u_middle", evicted_while_pinned)

            restored_middle = sync_user_dir("u_middle")
            self.assertEqual(
                chromadb.PersistentClient(path=restored_middle).get_collection("notes").count(),
                1,
            )

    def test_06_hybrid_search_rrf_combines_dense_and_bm25_with_normalized_scores(self) -> None:
        """Verifies `similarity_search_with_scores` fuses dense Chroma similarity and BM25 lexical ranks with normalized `[0.0, 1.0]` scores."""
        vs = Chroma(
            collection_name="hybrid_test_col",
            persist_directory=self.container1_tmp,
            embedding_function=DeterministicFakeEmbeddings(),
        )
        vs.add_documents(
            [
                Document(
                    page_content="General quarterly financial overview and outlook",
                    metadata={"source": "overview.pdf", "page": 1},
                ),
                Document(
                    page_content="General revenue commentary and forecast",
                    metadata={"source": "commentary.pdf", "page": 2},
                ),
                Document(
                    page_content="Alice critical incident ticket ERR-9042-X remediation runbook",
                    metadata={"source": "runbook.pdf", "page": 7},
                ),
            ]
        )

        # Query 'ERR-9042-X':
        # - Dense fake embedding gives query vec[0]=0.1 (no 'alice'), so 'Alice ... ERR-9042-X' (vec[0]=1.0) ranks last in dense search
        # - Hybrid RRF fuses BM25 exact token match on 'ERR-9042-X' so runbook.pdf ranks #1 with default equal weights
        dense_only_docs, dense_only_scored = similarity_search_with_scores(
            vs,
            "ERR-9042-X",
            k=2,
            bm25_weight=0.0,
        )
        self.assertEqual(len(dense_only_docs), 2)
        self.assertNotIn("ERR-9042-X", dense_only_docs[0].page_content)
        self.assertTrue(0.0 < dense_only_scored[0][1] <= 1.0)

        hybrid_docs, hybrid_scored = similarity_search_with_scores(
            vs,
            "ERR-9042-X",
            k=2,
        )
        self.assertEqual(len(hybrid_docs), 2)
        self.assertIn("ERR-9042-X", hybrid_docs[0].page_content)
        for _doc, score in hybrid_scored:
            self.assertIsInstance(score, float)
            self.assertGreater(score, 0.0)
            self.assertLessEqual(score, 1.0)

        # When a chunk ranks #1 in both dense and BM25, its normalized RRF score equals 1.0
        top_docs, top_scored = similarity_search_with_scores(
            vs,
            "Alice ERR-9042-X remediation runbook",
            k=2,
        )
        self.assertIn("ERR-9042-X", top_docs[0].page_content)
        self.assertEqual(top_scored[0][1], 1.0)


if __name__ == "__main__":
    logger.info("Running Per-User GCS <-> ChromaDB Storage unit test suite...")
    unittest.main(verbosity=2)
