"""InteractiveRAG backend source package.

Organizes the system into five modular packages deployed across two Cloud Run services:
- `src.core`: Shared configuration, authentication, multi-tenant storage, AI security, compliance, and observability.
- `src.ingest`: Dedicated write-path PDF upload parsing, chunking, and Chroma batch indexing (`ingest-api` service).
- `src.manage`: Tenant collection management (`/collections`) and GDPR DSAR (`/compliance/my-data`) routes (mounted on `agent-api`).
- `src.query`: Read-path Chroma similarity search (`POST /query`, mounted on `agent-api`).
- `src.agent`: LangGraph Agentic RAG workflow, XML-spotlighted retrieval tool, and SSE streaming (`agent-api` service).
"""

