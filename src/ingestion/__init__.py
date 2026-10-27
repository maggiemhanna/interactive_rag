"""Document Ingestion & Vectorstore Similarity Search service package (`src.ingestion`).

Submodules:
- `app`: FastAPI `ingestion-api` service exposing `POST /ingest` and `POST /query`.
- `schemas`: Pydantic request validation models (`IngestRequest`, `QueryRequest`).
- `splitter`: Pure PDF filename sanitization (`sanitize_filename`) and text chunking (`split_pdfs`).
- `upload_parser`: Multipart/JSON upload staging, `%PDF-` verification, and ingestion ceiling enforcement.
- `service`: ChromaDB batch indexing and Cloud DLP-sanitized similarity search orchestration.
"""
