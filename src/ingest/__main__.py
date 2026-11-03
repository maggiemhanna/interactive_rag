"""CLI entrypoint for launching the Document Ingestion FastAPI server (`python -m src.ingest`)."""

from src.ingest.app import main

if __name__ == "__main__":
    main()
