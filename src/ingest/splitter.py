"""PDF filename sanitization and document chunk splitting utilities (`src.ingest.splitter`).

Provides pure document-processing helpers independent of FastAPI HTTP request objects:
- `sanitize_filename`: Strips path traversal components and enforces `.pdf` extensions.
- `split_pdfs`: Loads PDF pages via `PyPDFLoader` and chunks text with `RecursiveCharacterTextSplitter`.
"""

import os
from typing import List, Union

from fastapi import HTTPException
from langchain_community.document_loaders import PyPDFLoader
from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter

from src.core.logger import setup_logging
from src.core.observability.tracing import start_span
from src.core.storage.paths import SAFE_FILENAME_RE

logger = setup_logging(name="ingest.splitter")


def sanitize_filename(raw_filename: str) -> str:
    """Strips directory components and enforces a safe `.pdf` filename.

    Args:
        raw_filename: Client-supplied multipart upload filename.

    Returns:
        Sanitized basename containing only safe characters and ending with `.pdf`.

    Raises:
        HTTPException: HTTP 400 if the filename does not have a `.pdf` extension.
    """
    basename = os.path.basename((raw_filename or "").replace("\\", "/")).strip()
    cleaned = SAFE_FILENAME_RE.sub("_", basename).strip("._")
    if not cleaned:
        cleaned = "document.pdf"
    if not cleaned.lower().endswith(".pdf"):
        raise HTTPException(
            status_code=400,
            detail=f"Only PDF uploads are supported (received '{basename or raw_filename}').",
        )
    return cleaned


def split_pdfs(
    pdf_paths: Union[str, List[str]],
    chunk_size: int = 1000,
    chunk_overlap: int = 200,
) -> List[Document]:
    """Loads one or more PDF files from disk and splits pages into overlapping text chunks.

    Args:
        pdf_paths: Single PDF filesystem path or list of PDF filesystem paths.
        chunk_size: Target character count per chunk (defaults to `1000`).
        chunk_overlap: Overlap character count between consecutive chunks (defaults to `200`).

    Returns:
        List of LangChain `Document` chunks carrying `source` and 0-indexed `page` metadata.

    Raises:
        FileNotFoundError: If any path in `pdf_paths` does not exist on disk.
    """
    if isinstance(pdf_paths, str):
        pdf_paths = [pdf_paths]

    with start_span("ingest.split_pdfs", stage="chunking", file_count=len(pdf_paths)) as span:
        all_chunks: List[Document] = []
        text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=chunk_size, chunk_overlap=chunk_overlap
        )

        for pdf_path in pdf_paths:
            if not os.path.exists(pdf_path):
                raise FileNotFoundError(f"PDF file not found: {pdf_path}")

            try:
                pdf_loader = PyPDFLoader(pdf_path)
                pages = pdf_loader.load()
                logger.info(f"Loaded PDF '{pdf_path}' ({len(pages)} pages).")
                chunks = text_splitter.split_documents(pages)
                logger.info(f"Split PDF '{pdf_path}' into {len(chunks)} chunks.")
                all_chunks.extend(chunks)
            except Exception as e:
                logger.error(f"Error loading PDF '{pdf_path}': {e}")
                raise

        span["attributes"]["chunks_count"] = len(all_chunks)
        return all_chunks


__all__ = ["sanitize_filename", "split_pdfs"]
