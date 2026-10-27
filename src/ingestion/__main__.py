"""CLI entrypoint for launching the Document Ingestion & Vector Search FastAPI server (`python -m src.ingestion`)."""

from src.ingestion.app import main

if __name__ == "__main__":
    main()
