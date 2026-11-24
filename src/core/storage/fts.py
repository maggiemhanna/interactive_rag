"""Persisted per-collection SQLite FTS5 inverted index helpers (`src.core.storage.fts`).

Maintains one standalone SQLite FTS5 sidecar database per collection under
`<user_dir>/fts/<collection_name>.sqlite3` (synced to `gs://<bucket>/users/{uid}/fts/`),
enabling sub-linear lexical BM25 retrieval (`O(log V + matching_docs)`) without rebuilding
an in-memory `BM25Retriever` over the entire collection on every query.
"""

import contextlib
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Any, List, Sequence

from langchain_core.documents import Document

from src.core.ai_security.guardrails import validate_collection_name
from src.core.storage.paths import get_fts_dir

FTS_TABLE_NAME = "chunks_fts"
_LEXICAL_TOKEN_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def tokenize_lexical(text: str) -> List[str]:
    """Tokenizes text into lowercase alphanumeric and hyphenated terms for lexical BM25 matching."""
    return _LEXICAL_TOKEN_RE.findall((text or "").lower())


def fts_index_path(user_dir: str, collection_name: str) -> str:
    """Returns the validated filesystem path `<user_dir>/fts/<collection_name>.sqlite3`.

    Args:
        user_dir: Local tenant root working directory.
        collection_name: Validated collection identifier (`^[a-zA-Z0-9_-]{1,64}$`).

    Returns:
        Absolute filesystem path to the collection's SQLite FTS5 sidecar file.

    Raises:
        ValueError: If `collection_name` is invalid or escapes `<user_dir>/fts/`.
    """
    safe_collection = validate_collection_name(collection_name)
    fts_dir = os.path.abspath(get_fts_dir(user_dir))
    index_path = os.path.abspath(os.path.join(fts_dir, f"{safe_collection}.sqlite3"))
    if os.path.commonpath([fts_dir, index_path]) != fts_dir:
        raise ValueError(f"Unsafe FTS index path for collection '{safe_collection}'.")
    return index_path


def has_fts_index(user_dir: str, collection_name: str) -> bool:
    """Checks whether a persisted SQLite FTS5 sidecar exists for `collection_name`."""
    try:
        return os.path.exists(fts_index_path(user_dir, collection_name))
    except ValueError:
        return False


def add_documents_to_fts(
    user_dir: str,
    collection_name: str,
    documents: Sequence[Document],
) -> int:
    """Appends document chunks into `<user_dir>/fts/<collection_name>.sqlite3`.

    Uses SQLite's default `DELETE` journal mode and closes the connection explicitly so
    the resulting `.sqlite3` file is immediately clean for GCS snapshot upload without
    leftover `-wal`/`-shm` sidecars.

    Args:
        user_dir: Local tenant root working directory.
        collection_name: Validated target collection identifier.
        documents: Sequence of `Document` chunks to index.

    Returns:
        Number of chunks inserted into the FTS5 inverted index.
    """
    if not documents:
        return 0

    path = fts_index_path(user_dir, collection_name)
    os.makedirs(os.path.dirname(path), exist_ok=True)

    rows = [
        (
            str(doc.page_content or ""),
            json.dumps(
                doc.metadata if isinstance(getattr(doc, "metadata", None), dict) else {},
                sort_keys=True,
                default=str,
            ),
        )
        for doc in documents
    ]

    conn = sqlite3.connect(path, timeout=10.0)
    try:
        with conn:
            conn.execute(
                f"""
                CREATE VIRTUAL TABLE IF NOT EXISTS {FTS_TABLE_NAME} USING fts5(
                    page_content,
                    metadata_json UNINDEXED,
                    tokenize = "unicode61 tokenchars '-'"
                )
                """
            )
            conn.executemany(
                f"INSERT INTO {FTS_TABLE_NAME} (page_content, metadata_json) VALUES (?, ?)",
                rows,
            )
    finally:
        conn.close()

    return len(rows)


def search_fts(
    user_dir: str,
    collection_name: str,
    query: str,
    limit: int = 10,
) -> List[Document]:
    """Queries `<user_dir>/fts/<collection_name>.sqlite3` via SQLite FTS5 `bm25()`.

    Extracts safe alphanumeric/hyphenated query terms, quotes each term to prevent FTS5
    operator injection (`OR`/`NOT`/`*`/`:`), and joins them with `OR` so chunks matching
    any query terms are ranked by SQLite's native BM25 score (more negative = stronger match).

    Args:
        user_dir: Local tenant root working directory.
        collection_name: Validated target collection identifier.
        query: Search query string.
        limit: Maximum number of ranked documents to return.

    Returns:
        Top-`limit` matching `Document` instances ordered by descending BM25 relevance.
    """
    safe_limit = int(limit)
    if safe_limit <= 0:
        return []

    path = fts_index_path(user_dir, collection_name)
    if not os.path.exists(path):
        return []

    tokens = list(dict.fromkeys(tokenize_lexical(query)))
    if not tokens:
        return []

    fts_query = " OR ".join(f'"{tok}"' for tok in tokens)
    ro_uri = f"{Path(os.path.abspath(path)).as_uri()}?mode=ro"

    conn = sqlite3.connect(ro_uri, uri=True, timeout=10.0)
    try:
        rows = conn.execute(
            f"""
            SELECT page_content, metadata_json
            FROM {FTS_TABLE_NAME}
            WHERE {FTS_TABLE_NAME} MATCH ?
            ORDER BY bm25({FTS_TABLE_NAME}) ASC
            LIMIT ?
            """,
            (fts_query, safe_limit),
        ).fetchall()
    finally:
        conn.close()

    docs: List[Document] = []
    for text, meta_json in rows:
        metadata: Any = {}
        if isinstance(meta_json, str) and meta_json:
            with contextlib.suppress(json.JSONDecodeError, TypeError):
                parsed = json.loads(meta_json)
                if isinstance(parsed, dict):
                    metadata = parsed
        docs.append(Document(page_content=str(text or ""), metadata=metadata))
    return docs


def remove_fts_index(user_dir: str, collection_name: str) -> bool:
    """Deletes `<user_dir>/fts/<collection_name>.sqlite3` if it exists.

    Args:
        user_dir: Local tenant root working directory.
        collection_name: Validated collection identifier to delete.

    Returns:
        True when an existing FTS5 index file was deleted, else False.
    """
    path = fts_index_path(user_dir, collection_name)
    if not os.path.exists(path):
        return False

    os.remove(path)
    fts_dir = os.path.dirname(path)
    with contextlib.suppress(OSError):
        if os.path.isdir(fts_dir) and not os.listdir(fts_dir):
            os.rmdir(fts_dir)
    return True


__all__ = [
    "FTS_TABLE_NAME",
    "add_documents_to_fts",
    "fts_index_path",
    "has_fts_index",
    "remove_fts_index",
    "search_fts",
    "tokenize_lexical",
]
