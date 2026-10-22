import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from typing import Dict, List, Optional
from unittest.mock import patch

import chromadb
from google.api_core import exceptions as gcs_exceptions

from src.core import storage as chroma_storage
from src.core.logger import setup_logging
from src.core.storage import (
    LockTimeoutError,
    evict_tmp_cache,
    release_client_cache,
    sync_user_dir,
    user_read_lock,
    user_write_lock,
)

logger = setup_logging(name="test_storage")


class FakeGCSBlob:
    """In-memory GCS Blob double supporting atomic `if_generation_match` preconditions."""

    def __init__(self, bucket: "FakeGCSBucket", name: str):
        self.bucket = bucket
        self.name = name
        self._generation: Optional[int] = None

    @property
    def generation(self) -> Optional[int]:
        if self._generation is not None:
            return self._generation
        entry = self.bucket._objects.get(self.name)
        return entry["generation"] if entry else None

    def exists(self) -> bool:
        return self.name in self.bucket._objects

    def upload_from_string(
        self,
        data: str | bytes,
        content_type: str = "application/octet-stream",
        if_generation_match: Optional[int] = None,
    ) -> None:
        raw = data.encode("utf-8") if isinstance(data, str) else bytes(data)
        self._generation = self.bucket._put(self.name, raw, if_generation_match=if_generation_match)

    def upload_from_filename(
        self,
        filename: str,
        content_type: str = "application/octet-stream",
        if_generation_match: Optional[int] = None,
    ) -> None:
        with open(filename, "rb") as f:
            raw = f.read()
        self._generation = self.bucket._put(self.name, raw, if_generation_match=if_generation_match)

    def download_as_text(self) -> str:
        entry = self.bucket._get(self.name)
        self._generation = entry["generation"]
        return entry["data"].decode("utf-8")

    def download_to_filename(self, filename: str) -> None:
        entry = self.bucket._get(self.name)
        self._generation = entry["generation"]
        with open(filename, "wb") as f:
            f.write(entry["data"])

    def delete(self, if_generation_match: Optional[int] = None) -> None:
        self.bucket._delete(self.name, if_generation_match=if_generation_match)


class FakeGCSBucket:
    """Thread-safe in-memory GCS Bucket double."""

    def __init__(self, name: str = "test-chroma-bucket"):
        self.name = name
        self._lock = threading.Lock()
        self._next_gen = 1
        self._objects: Dict[str, Dict] = {}

    def blob(self, name: str) -> FakeGCSBlob:
        return FakeGCSBlob(self, name)

    def list_blobs(self, prefix: str = "") -> List[FakeGCSBlob]:
        with self._lock:
            names = [k for k in sorted(self._objects.keys()) if k.startswith(prefix)]
            blobs = []
            for n in names:
                b = FakeGCSBlob(self, n)
                b._generation = self._objects[n]["generation"]
                blobs.append(b)
            return blobs

    def _put(self, name: str, data: bytes, if_generation_match: Optional[int] = None) -> int:
        with self._lock:
            existing = self._objects.get(name)
            current_gen = existing["generation"] if existing else 0
            if if_generation_match is not None and current_gen != if_generation_match:
                raise gcs_exceptions.PreconditionFailed(
                    f"Precondition failed on {name}: expected gen {if_generation_match}, got {current_gen}"
                )
            gen = self._next_gen
            self._next_gen += 1
            self._objects[name] = {"data": data, "generation": gen}
            return gen

    def _get(self, name: str) -> Dict:
        with self._lock:
            if name not in self._objects:
                raise gcs_exceptions.NotFound(f"Object {name} not found")
            return dict(self._objects[name])

    def _delete(self, name: str, if_generation_match: Optional[int] = None) -> None:
        with self._lock:
            if name not in self._objects:
                raise gcs_exceptions.NotFound(f"Object {name} not found")
            current_gen = self._objects[name]["generation"]
            if if_generation_match is not None and current_gen != if_generation_match:
                raise gcs_exceptions.PreconditionFailed(
                    f"Delete precondition failed on {name}: expected {if_generation_match}, got {current_gen}"
                )
            del self._objects[name]


class TestPerUserChromaStorage(unittest.TestCase):
    def setUp(self):
        self.container1_tmp = tempfile.mkdtemp(prefix="chroma_c1_")
        self.container2_tmp = tempfile.mkdtemp(prefix="chroma_c2_")
        self.fake_bucket = FakeGCSBucket("interactive-rag-chroma-test")

    def tearDown(self):
        for root in (self.container1_tmp, self.container2_tmp):
            if os.path.exists(root):
                for entry in os.scandir(root):
                    if entry.is_dir():
                        release_client_cache(entry.path)
                shutil.rmtree(root, ignore_errors=True)

    def _env(self, local_root: str, **overrides) -> Dict[str, str]:
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

    def test_01_strict_per_user_isolation_same_collection_name(self):
        with patch.object(chroma_storage, "_get_bucket", return_value=self.fake_bucket), patch.dict(
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
        with patch.object(chroma_storage, "_get_bucket", return_value=self.fake_bucket), patch.dict(
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

    def test_02_cross_service_version_invalidation_refreshes_cached_chroma_client(self):
        # Step 1: Container 1 (ingestion-api) writes 1 doc
        with patch.object(chroma_storage, "_get_bucket", return_value=self.fake_bucket), patch.dict(
            os.environ, self._env(self.container1_tmp), clear=False
        ):
            with user_write_lock("user_alice") as dir_c1:
                col = chromadb.PersistentClient(path=dir_c1).get_or_create_collection("reports")
                col.add(ids=["1"], documents=["Doc 1"], embeddings=[[0.1, 0.2, 0.3]])

        # Step 2: Container 2 (agent-api) syncs and opens PersistentClient (populates Chroma internal cache)
        with patch.object(chroma_storage, "_get_bucket", return_value=self.fake_bucket), patch.dict(
            os.environ, self._env(self.container2_tmp), clear=False
        ):
            dir_c2 = sync_user_dir("user_alice")
            self.assertEqual(chromadb.PersistentClient(path=dir_c2).get_collection("reports").count(), 1)

        # Step 3: Container 1 adds a second doc -> bumps version.json in GCS
        with patch.object(chroma_storage, "_get_bucket", return_value=self.fake_bucket), patch.dict(
            os.environ, self._env(self.container1_tmp), clear=False
        ):
            with user_write_lock("user_alice") as dir_c1:
                col = chromadb.PersistentClient(path=dir_c1).get_collection("reports")
                col.add(ids=["2"], documents=["Doc 2"], embeddings=[[0.4, 0.5, 0.6]])

        # Step 4: Container 2 syncs again -> detects version bump, invalidates Chroma system cache, sees 2 docs!
        with patch.object(chroma_storage, "_get_bucket", return_value=self.fake_bucket), patch.dict(
            os.environ, self._env(self.container2_tmp), clear=False
        ):
            dir_c2_refreshed = sync_user_dir("user_alice")
            self.assertEqual(
                chromadb.PersistentClient(path=dir_c2_refreshed).get_collection("reports").count(),
                2,
            )

    def test_03_deleted_collection_cleans_up_remote_gcs_blobs(self):
        with patch.object(chroma_storage, "_get_bucket", return_value=self.fake_bucket), patch.dict(
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
        with patch.object(chroma_storage, "_get_bucket", return_value=self.fake_bucket), patch.dict(
            os.environ, self._env(self.container2_tmp), clear=False
        ):
            restored_dir = sync_user_dir("user_alice")
            cols = [c.name for c in chromadb.PersistentClient(path=restored_dir).list_collections()]
            self.assertEqual(cols, ["keep_me"])

    def test_04_stale_gcs_lock_ttl_recovery_and_active_lock_contention(self):
        # Plant a crashed container's expired lock in GCS
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

        with patch.object(chroma_storage, "_get_bucket", return_value=self.fake_bucket), patch.dict(
            os.environ, self._env(self.container1_tmp), clear=False
        ):
            # Should recover expired lock automatically and release it afterward
            with user_write_lock("user_alice") as alice_dir:
                chromadb.PersistentClient(path=alice_dir).create_collection("recovered_col")

            self.assertFalse(
                self.fake_bucket.blob("users/user_alice/locks/chroma.lock").exists(),
                "Lock must be deleted after write context exits",
            )

        # Plant a LIVE non-expired lock held by another container -> verify timeout raises cleanly
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

        with patch.object(chroma_storage, "_get_bucket", return_value=self.fake_bucket), patch.dict(
            os.environ,
            self._env(self.container1_tmp, CHROMA_LOCK_TIMEOUT_SECONDS="0.4"),
            clear=False,
        ):
            with self.assertRaises(LockTimeoutError):
                with user_write_lock("user_bob"):
                    pass

    def test_05_lru_tmp_eviction_preserves_active_users_and_evicts_oldest_inactive(self):
        with patch.object(chroma_storage, "_get_bucket", return_value=self.fake_bucket), patch.dict(
            os.environ, self._env(self.container1_tmp), clear=False
        ):
            for uid in ("u_oldest", "u_middle", "u_newest"):
                with user_write_lock(uid) as udir:
                    col = chromadb.PersistentClient(path=udir).create_collection("notes")
                    col.add(ids=["1"], documents=[f"Data for {uid}"], embeddings=[[0.2, 0.3, 0.5]])
                time.sleep(0.02)

            # Pin `u_oldest` as actively reading so LRU must skip `u_oldest` and evict `u_middle` first!
            with user_read_lock("u_oldest"):
                with patch.dict(os.environ, {"CHROMA_TMP_MAX_BYTES": "1"}, clear=False):
                    evicted_while_pinned = evict_tmp_cache(exclude_uid="u_newest")
                    self.assertNotIn("u_oldest", evicted_while_pinned)
                    self.assertIn("u_middle", evicted_while_pinned)

            # Evicted `u_middle` still restores cleanly from GCS whenever requested again
            restored_middle = sync_user_dir("u_middle")
            self.assertEqual(
                chromadb.PersistentClient(path=restored_middle).get_collection("notes").count(),
                1,
            )


if __name__ == "__main__":
    logger.info("Running Phase 3 Per-User GCS <-> ChromaDB Storage test suite...")
    unittest.main(verbosity=2)
