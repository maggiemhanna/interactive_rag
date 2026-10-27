"""InteractiveRAG backend source package.

Organizes the system into three top-level domains:
- `src.core`: Shared configuration, authentication, multi-tenant storage, AI security, and compliance.
- `src.ingestion`: Document upload parsing, PDF chunking, Chroma indexing, and similarity search API.
- `src.agent`: LangGraph Agentic RAG workflow, XML-spotlighted retrieval tool, and SSE streaming API.
"""
