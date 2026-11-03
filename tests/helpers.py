"""Shared offline test doubles, fake models, and lifecycle utilities for `tests/`.

Centralizes deterministic test fixtures used across multiple test suites:
- `FakeGCSBlob` & `FakeGCSBucket`: Thread-safe in-memory Google Cloud Storage
  doubles supporting atomic `if_generation_match` preconditions, MD5 hashes,
  and full object versioning (`versions=True`, `delete_blob(..., generation=...)`).
- `make_test_pdf`: Minimal valid single-page PDF 1.4 byte generator.
- `DeterministicFakeEmbeddings`: Offline 4-dimensional embeddings double for ChromaDB.
- `GroundedCitingChatModel`: Offline tool-calling chat model double emitting
  grounded `[report.pdf, p.1]` citations and token `usage_metadata`.
- Model Armor & Cloud DLP response builders (`make_clean_prompt_response`,
  `make_blocked_pi_prompt_response`, `make_clean_output_response`,
  `make_dlp_redacted_output_response`).
- Lifecycle and assertion helpers (`cleanup_chroma_tmpdir`, `reset_memory_saver`,
  `parse_sse_frames`, `create_synthetic_repo_copy`).
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import json
import os
import shutil
import tempfile
import threading
from typing import Any, Callable, Dict, List, Optional

from google.api_core import exceptions as gcs_exceptions
from google.cloud import modelarmor_v1
from langchain_core.embeddings import Embeddings
from langchain_core.messages import AIMessage

from src.agent.memory import MEMORY_SAVER
from src.core.storage import release_client_cache

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class FakeGCSBlob:
    """In-memory GCS Blob double supporting atomic `if_generation_match`, MD5 hashes, and object versioning."""

    def __init__(
        self,
        bucket: "FakeGCSBucket",
        name: str,
        generation: Optional[int] = None,
        size: Optional[int] = None,
        updated: Optional[datetime] = None,
        time_deleted: Optional[datetime] = None,
    ) -> None:
        self.bucket = bucket
        self.name = name
        self._generation = generation
        self._size = size
        self.updated = updated
        self.time_deleted = time_deleted

    @property
    def generation(self) -> Optional[int]:
        """Returns the pinned object generation or current live generation."""
        if self._generation is not None:
            return self._generation
        live = self.bucket._live.get(self.name)
        return live["generation"] if live else None

    @property
    def md5_hash(self) -> Optional[str]:
        """Returns the base64-encoded MD5 digest of the live object payload."""
        live = self.bucket._live.get(self.name)
        if not live:
            return None
        return base64.b64encode(hashlib.md5(live["data"]).digest()).decode("ascii")

    @property
    def size(self) -> int:
        """Returns the byte length of the pinned or live object payload."""
        if self._size is not None:
            return self._size
        live = self.bucket._live.get(self.name)
        return int(live["size"]) if live else 0

    def exists(self) -> bool:
        """Returns `True` if a live version of this blob currently exists in the bucket."""
        return self.name in self.bucket._live

    def upload_from_string(
        self,
        data: str | bytes,
        content_type: str = "application/octet-stream",
        if_generation_match: Optional[int] = None,
    ) -> None:
        """Uploads string or bytes content with optional `if_generation_match` precondition."""
        raw = data.encode("utf-8") if isinstance(data, str) else bytes(data)
        self._generation = self.bucket._put(
            self.name, raw, if_generation_match=if_generation_match
        )
        self._size = len(raw)

    def upload_from_filename(
        self,
        filename: str,
        content_type: str = "application/octet-stream",
        if_generation_match: Optional[int] = None,
    ) -> None:
        """Uploads file content from disk with optional `if_generation_match` precondition."""
        with open(filename, "rb") as fh:
            raw = fh.read()
        self._generation = self.bucket._put(
            self.name, raw, if_generation_match=if_generation_match
        )
        self._size = len(raw)

    def download_as_text(self) -> str:
        """Downloads the live object content decoded as UTF-8 text."""
        entry = self.bucket._get_live(self.name)
        self._generation = entry["generation"]
        return entry["data"].decode("utf-8")

    def download_to_filename(self, filename: str) -> None:
        """Downloads the live object content to a local file path."""
        entry = self.bucket._get_live(self.name)
        self._generation = entry["generation"]
        with open(filename, "wb") as fh:
            fh.write(entry["data"])

    def delete(self, if_generation_match: Optional[int] = None) -> None:
        """Deletes the live object (archiving it to noncurrent versions) with optional generation check."""
        self.bucket._soft_delete_live(
            self.name, if_generation_match=if_generation_match
        )


class FakeGCSBucket:
    """Thread-safe in-memory GCS Bucket double supporting atomic preconditions and object versioning."""

    def __init__(self, name: str = "test-chroma-bucket") -> None:
        self.name = name
        self._lock = threading.Lock()
        self._next_gen = 1
        self._live: Dict[str, Dict[str, Any]] = {}
        self._noncurrent: List[Dict[str, Any]] = []
        self._now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)

    def blob(self, name: str) -> FakeGCSBlob:
        """Returns a `FakeGCSBlob` handle for `name`."""
        return FakeGCSBlob(self, name)

    def _put(
        self,
        name: str,
        data: bytes,
        if_generation_match: Optional[int] = None,
    ) -> int:
        with self._lock:
            existing = self._live.get(name)
            current_gen = existing["generation"] if existing else 0
            if if_generation_match is not None and current_gen != if_generation_match:
                raise gcs_exceptions.PreconditionFailed(
                    f"Precondition failed on {name}: expected gen {if_generation_match}, got {current_gen}"
                )
            now = self._now()
            if existing is not None:
                archived = dict(existing)
                archived["time_deleted"] = now
                self._noncurrent.append(archived)
            gen = self._next_gen
            self._next_gen += 1
            self._live[name] = {
                "name": name,
                "data": data,
                "size": len(data),
                "generation": gen,
                "updated": now,
                "time_deleted": None,
            }
            return gen

    def _get_live(self, name: str) -> Dict[str, Any]:
        with self._lock:
            if name not in self._live:
                raise gcs_exceptions.NotFound(f"Object {name} not found")
            return dict(self._live[name])

    def _soft_delete_live(
        self, name: str, if_generation_match: Optional[int] = None
    ) -> None:
        with self._lock:
            if name not in self._live:
                raise gcs_exceptions.NotFound(f"Object {name} not found")
            existing = self._live[name]
            if (
                if_generation_match is not None
                and existing["generation"] != if_generation_match
            ):
                raise gcs_exceptions.PreconditionFailed(
                    f"Delete precondition failed on {name}: expected {if_generation_match}, got {existing['generation']}"
                )
            archived = dict(existing)
            archived["time_deleted"] = self._now()
            self._noncurrent.append(archived)
            del self._live[name]

    def delete_blob(
        self,
        blob_name: str,
        generation: Optional[int] = None,
        **_kwargs: Any,
    ) -> None:
        """Deletes either the live object (`generation=None`) or a specific versioned generation."""
        if generation is None:
            self._soft_delete_live(blob_name)
            return

        with self._lock:
            live = self._live.get(blob_name)
            if live is not None and live["generation"] == generation:
                del self._live[blob_name]
                return
            for idx, ver in enumerate(self._noncurrent):
                if ver["name"] == blob_name and ver["generation"] == generation:
                    self._noncurrent.pop(idx)
                    return
            raise gcs_exceptions.NotFound(
                f"Object {blob_name} generation={generation} not found"
            )

    def list_blobs(
        self, prefix: str = "", versions: bool = False
    ) -> List[FakeGCSBlob]:
        """Lists live blobs matching `prefix`, optionally including noncurrent object versions."""
        with self._lock:
            results: List[FakeGCSBlob] = []
            for name in sorted(self._live.keys()):
                if name.startswith(prefix):
                    entry = self._live[name]
                    results.append(
                        FakeGCSBlob(
                            self,
                            name=name,
                            generation=entry["generation"],
                            size=entry["size"],
                            updated=entry["updated"],
                            time_deleted=None,
                        )
                    )
            if versions:
                for entry in self._noncurrent:
                    if entry["name"].startswith(prefix):
                        results.append(
                            FakeGCSBlob(
                                self,
                                name=entry["name"],
                                generation=entry["generation"],
                                size=entry["size"],
                                updated=entry["updated"],
                                time_deleted=entry["time_deleted"],
                            )
                        )
            return results


def make_test_pdf(text: str) -> bytes:
    """Builds a minimal valid single-page PDF 1.4 document containing `text`."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("latin-1")
    objs = [
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n",
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n",
        b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>\nendobj\n",
        f"4 0 obj\n<< /Length {len(stream)} >>\nstream\n".encode("latin-1")
        + stream
        + b"\nendstream\nendobj\n",
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
        f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n".encode(
            "latin-1"
        )
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
        """Embeds a list of document texts deterministically."""
        return [self._vec(t) for t in texts]

    def embed_query(self, text: str) -> List[float]:
        """Embeds a query string deterministically."""
        return self._vec(text)


class GroundedCitingChatModel:
    """Deterministic LLM double that calls the bound retriever tool once and returns a cited answer with `usage_metadata`."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self._tool_name = "retriever"
        self.invocations = 0

    def bind_tools(self, tools: List[Any]) -> "GroundedCitingChatModel":
        """Binds the first tool's name for subsequent tool-call generation."""
        if tools:
            self._tool_name = tools[0].name
        return self

    def invoke(self, messages: List[Any]) -> AIMessage:
        """Emits a retriever tool call on the first turn and a grounded `[report.pdf, p.1]` answer after tool output."""
        self.invocations += 1
        last = messages[-1]
        if getattr(last, "type", "") == "tool":
            return AIMessage(
                content=f"Grounded summary from retrieved context: {last.content} [report.pdf, p.1]",
                usage_metadata={
                    "input_tokens": 350,
                    "output_tokens": 80,
                    "total_tokens": 430,
                },
            )
        query_text = getattr(last, "content", "query")
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "id": f"call_{self.invocations}",
                    "name": self._tool_name,
                    "args": {"query": str(query_text)},
                }
            ],
            usage_metadata={
                "input_tokens": 120,
                "output_tokens": 25,
                "total_tokens": 145,
            },
        )


def make_clean_prompt_response() -> modelarmor_v1.SanitizeUserPromptResponse:
    """Builds a Model Armor `SanitizeUserPromptResponse` with `NO_MATCH_FOUND`."""
    return modelarmor_v1.SanitizeUserPromptResponse(
        sanitization_result=modelarmor_v1.SanitizationResult(
            filter_match_state=modelarmor_v1.FilterMatchState.NO_MATCH_FOUND,
        )
    )


def make_blocked_pi_prompt_response() -> modelarmor_v1.SanitizeUserPromptResponse:
    """Builds a Model Armor `SanitizeUserPromptResponse` blocking on `pi_and_jailbreak`."""
    return modelarmor_v1.SanitizeUserPromptResponse(
        sanitization_result=modelarmor_v1.SanitizationResult(
            filter_match_state=modelarmor_v1.FilterMatchState.MATCH_FOUND,
            filter_results={
                "pi_and_jailbreak": modelarmor_v1.FilterResult(
                    pi_and_jailbreak_filter_result=modelarmor_v1.PiAndJailbreakFilterResult(
                        match_state=modelarmor_v1.FilterMatchState.MATCH_FOUND,
                    )
                )
            },
        )
    )


def make_clean_output_response() -> modelarmor_v1.SanitizeModelResponseResponse:
    """Builds a Model Armor `SanitizeModelResponseResponse` with `NO_MATCH_FOUND`."""
    return modelarmor_v1.SanitizeModelResponseResponse(
        sanitization_result=modelarmor_v1.SanitizationResult(
            filter_match_state=modelarmor_v1.FilterMatchState.NO_MATCH_FOUND,
        )
    )


def make_dlp_redacted_output_response(
    redacted_text: str,
) -> modelarmor_v1.SanitizeModelResponseResponse:
    """Builds a Model Armor `SanitizeModelResponseResponse` containing SDP de-identified `redacted_text`."""
    return modelarmor_v1.SanitizeModelResponseResponse(
        sanitization_result=modelarmor_v1.SanitizationResult(
            filter_match_state=modelarmor_v1.FilterMatchState.MATCH_FOUND,
            filter_results={
                "sdp": modelarmor_v1.FilterResult(
                    sdp_filter_result=modelarmor_v1.SdpFilterResult(
                        deidentify_result=modelarmor_v1.SdpDeidentifyResult(
                            match_state=modelarmor_v1.FilterMatchState.MATCH_FOUND,
                            data=modelarmor_v1.DataItem(text=redacted_text),
                        )
                    )
                )
            },
        )
    )


def cleanup_chroma_tmpdir(*roots: str) -> None:
    """Releases cached Chroma `PersistentClient` instances under each root directory and removes the directory tree."""
    for root in roots:
        if root and os.path.exists(root):
            for entry in os.scandir(root):
                if entry.is_dir():
                    release_client_cache(entry.path)
            shutil.rmtree(root, ignore_errors=True)


def reset_memory_saver() -> None:
    """Clears all in-memory checkpoints, writes, and blobs from the shared LangGraph `MEMORY_SAVER`."""
    MEMORY_SAVER.storage.clear()
    MEMORY_SAVER.writes.clear()
    MEMORY_SAVER.blobs.clear()


def parse_sse_frames(raw_sse_text: str) -> List[Dict[str, Any]]:
    """Parses JSON `data: ...` frames (excluding `[DONE]`) from an SSE response body."""
    frames: List[Dict[str, Any]] = []
    for raw_line in raw_sse_text.splitlines():
        line = raw_line.strip()
        if line.startswith("data: "):
            payload = line[6:].strip()
            if payload and payload != "[DONE]":
                frames.append(json.loads(payload))
    return frames


def create_synthetic_repo_copy(
    repo_root: str = REPO_ROOT, prefix: str = "static_policy_"
) -> str:
    """Creates a temporary copy of `infra/` and `src/core/config.py` (without `terraform.tfvars`) for static policy mutation tests."""
    syn_root = tempfile.mkdtemp(prefix=prefix)
    os.makedirs(os.path.join(syn_root, "src", "core"), exist_ok=True)
    shutil.copytree(
        os.path.join(repo_root, "infra"), os.path.join(syn_root, "infra")
    )
    shutil.copy2(
        os.path.join(repo_root, "src", "core", "config.py"),
        os.path.join(syn_root, "src", "core", "config.py"),
    )
    syn_tfvars = os.path.join(syn_root, "infra", "terraform.tfvars")
    if os.path.exists(syn_tfvars):
        os.remove(syn_tfvars)
    return syn_root
