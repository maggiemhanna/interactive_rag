import os
import sys
from typing import List, Union, Optional

from dotenv import load_dotenv, find_dotenv
from fastapi import FastAPI, HTTPException, Request, File, UploadFile, Form
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import uvicorn
from langchain_community.document_loaders import PyPDFLoader
from langchain_core.documents import Document
from langchain_google_genai import GoogleGenerativeAIEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_chroma import Chroma

from utils.logging import setup_logging

# Load environment variables from .env file (searches parent directories and current directory / src)
load_dotenv(find_dotenv())
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

logger = setup_logging(name="add_documents")

app = FastAPI(title="Document Ingestion API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class AddDocumentsRequest(BaseModel):
    pdf_paths: Union[str, List[str]] = Field(
        ..., description="Path or list of paths (or a single path) to PDF files to ingest"
    )
    collection_name: str = Field(
        default="stock_market_vs", description="Name of the Chroma vectorstore collection"
    )
    embedding_model: str = Field(
        default="gemini-embedding-001", description="Name of the Vertex AI embedding model"
    )


class QueryRequest(BaseModel):
    query: str = Field(..., description="Query text to search for in vector store")
    collection_name: str = Field(
        default="stock_market_vs", description="Name of the Chroma vectorstore collection"
    )
    k: int = Field(default=5, description="Number of results to retrieve")
    embedding_model: str = Field(
        default="gemini-embedding-001", description="Name of the Vertex AI embedding model"
    )



def load_and_split_pdfs(
    pdf_paths: Union[str, List[str]], chunk_size: int = 1000, chunk_overlap: int = 200
) -> List[Document]:
    """Loads PDF documents and splits them into text chunks.

    Args:
        pdf_paths: Path or list of paths to PDF files.
        chunk_size: Target character count per chunk.
        chunk_overlap: Overlap character count between consecutive chunks.

    Returns:
        List of document chunks across all PDFs.
    """
    if isinstance(pdf_paths, str):
        pdf_paths = [pdf_paths]

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

    return all_chunks


# Alias for backward compatibility
load_and_split_pdf = load_and_split_pdfs


def add_documents_to_vectorstore(
    documents: List[Document],
    persist_directory: str = "chroma_db",
    collection_name: str = "stock_market_vs",
    embedding_model: str = "gemini-embedding-001",
) -> Chroma:
    """Initializes or loads a Chroma vectorstore and adds documents to it.

    Args:
        documents: List of document chunks to add.
        persist_directory: Path to store Chroma DB.
        collection_name: Name of the vector collection.
        embedding_model: Name of the Vertex AI embedding model.

    Returns:
        Chroma vectorstore instance.
    """
    embeddings = GoogleGenerativeAIEmbeddings(
        model=embedding_model,
        project=os.getenv("GOOGLE_CLOUD_PROJECT"),
        vertexai=True,
    )


    if not os.path.exists(persist_directory):
        os.makedirs(persist_directory)

    try:
        vectorstore = Chroma(
            persist_directory=persist_directory,
            embedding_function=embeddings,
            collection_name=collection_name,
        )

        batch_size = 100
        total_chunks = len(documents)
        for i in range(0, total_chunks, batch_size):
            batch = documents[i : i + batch_size]
            vectorstore.add_documents(documents=batch)
            logger.info(
                f"Ingested batch {i // batch_size + 1} / {(total_chunks - 1) // batch_size + 1} ({len(batch)} chunks) into collection '{collection_name}'."
            )

        logger.info(
            f"Successfully added {total_chunks} documents to collection '{collection_name}' in '{persist_directory}'."
        )
        return vectorstore
    except Exception as e:
        logger.error(f"Error updating vectorstore: {e}")
        raise


@app.get("/collections")
def get_collections():
    """Returns a list of existing collection details from Chroma vectorstore."""
    persist_directory = "chroma_db"
    if not os.path.exists(persist_directory):
        return {"collections": [], "details": []}
    try:
        import chromadb
        client = chromadb.PersistentClient(path=persist_directory)
        collections = client.list_collections()
        collection_names = [col.name for col in collections]
        details = []
        for col in collections:
            try:
                count = col.count()
            except Exception:
                count = 0
            details.append({
                "name": col.name,
                "count": count,
            })
        return {"collections": collection_names, "details": details}
    except Exception as e:
        logger.error(f"Error fetching collections: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/collections/{collection_name}")
def delete_collection(collection_name: str):
    """Deletes an existing collection from Chroma vectorstore."""
    persist_directory = "chroma_db"
    if not os.path.exists(persist_directory):
        raise HTTPException(status_code=404, detail="Vector store database directory not found.")
    try:
        import chromadb
        client = chromadb.PersistentClient(path=persist_directory)
        # Check if collection exists
        existing = [col.name for col in client.list_collections()]
        if collection_name not in existing:
            raise HTTPException(status_code=404, detail=f"Collection '{collection_name}' not found.")
        
        client.delete_collection(name=collection_name)
        logger.info(f"Successfully deleted Chroma collection '{collection_name}'.")
        return {
            "status": "success",
            "message": f"Successfully deleted collection '{collection_name}'.",
            "collection_name": collection_name,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting collection '{collection_name}': {e}")
        raise HTTPException(status_code=500, detail=str(e))



@app.post("/add_documents")
async def add_documents(request: Request):
    """FastAPI endpoint to load, split, and add PDF documents (via file upload or file paths) to Chroma vectorstore."""
    content_type = request.headers.get("content-type", "")
    pdf_paths_to_process: List[str] = []
    collection_name = "stock_market_vs"
    embedding_model = "gemini-embedding-001"

    try:
        if "application/json" in content_type:
            body = await request.json()
            raw_paths = body.get("pdf_paths")
            if isinstance(raw_paths, str):
                pdf_paths_to_process.append(raw_paths)
            elif isinstance(raw_paths, list):
                pdf_paths_to_process.extend(raw_paths)
            collection_name = body.get("collection_name", collection_name)
            embedding_model = body.get("embedding_model", embedding_model)
        elif "multipart/form-data" in content_type or "application/x-www-form-urlencoded" in content_type:
            form = await request.form()
            collection_name = form.get("collection_name", collection_name)
            if isinstance(collection_name, list):
                collection_name = collection_name[0]
            collection_name = collection_name.strip() if collection_name else "stock_market_vs"

            raw_embedding = form.get("embedding_model", embedding_model)
            if isinstance(raw_embedding, list):
                raw_embedding = raw_embedding[0]
            embedding_model = raw_embedding.strip() if raw_embedding else "gemini-embedding-001"

            # Handle uploaded files
            files = form.getlist("files")
            if not files and "file" in form:
                files = form.getlist("file")

            if files:
                uploads_dir = os.path.join("data", "uploads")
                os.makedirs(uploads_dir, exist_ok=True)
                for file_item in files:
                    if hasattr(file_item, "filename") and file_item.filename:
                        save_path = os.path.join(uploads_dir, file_item.filename)
                        content = await file_item.read()
                        with open(save_path, "wb") as f:
                            f.write(content)
                        pdf_paths_to_process.append(save_path)

            # Handle pdf_paths field if provided in form
            paths_param = form.getlist("pdf_paths")
            for p in paths_param:
                if isinstance(p, str) and p.strip():
                    for single_p in p.replace("\r\n", "\n").split("\n"):
                        single_p = single_p.strip()
                        if single_p:
                            pdf_paths_to_process.append(single_p)
        else:
            try:
                body = await request.json()
                raw_paths = body.get("pdf_paths")
                if isinstance(raw_paths, str):
                    pdf_paths_to_process.append(raw_paths)
                elif isinstance(raw_paths, list):
                    pdf_paths_to_process.extend(raw_paths)
                collection_name = body.get("collection_name", collection_name)
                embedding_model = body.get("embedding_model", embedding_model)
            except Exception:
                raise HTTPException(
                    status_code=400,
                    detail="Unsupported Content-Type. Please send application/json or multipart/form-data."
                )

        if not pdf_paths_to_process:
            raise HTTPException(
                status_code=400,
                detail="No PDF files uploaded or PDF paths provided."
            )

        logger.info(f"Processing PDF documents for collection '{collection_name}' (embedding_model: '{embedding_model}'): {pdf_paths_to_process}")
        chunks = load_and_split_pdfs(pdf_paths_to_process)
        vectorstore = add_documents_to_vectorstore(
            documents=chunks,
            collection_name=collection_name,
            embedding_model=embedding_model,
        )
        return {
            "status": "success",
            "message": f"Successfully added {len(chunks)} document chunks to collection '{collection_name}'.",
            "chunks_added": len(chunks),
            "collection_name": collection_name,
            "processed_files": pdf_paths_to_process,
        }
    except FileNotFoundError as e:
        logger.error(f"File not found error: {e}")
        raise HTTPException(status_code=404, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to add documents: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/query")
def query_collection(request: QueryRequest):
    """FastAPI endpoint to search/query documents in a specific Chroma vectorstore collection."""
    persist_directory = "chroma_db"
    if not os.path.exists(persist_directory):
        raise HTTPException(status_code=404, detail="No Chroma vectorstore database found.")

    try:
        embeddings = GoogleGenerativeAIEmbeddings(
            model=request.embedding_model,
            project=os.getenv("GOOGLE_CLOUD_PROJECT"),
            vertexai=True,
        )
        vectorstore = Chroma(
            persist_directory=persist_directory,
            embedding_function=embeddings,
            collection_name=request.collection_name,
        )

        retriever = vectorstore.as_retriever(
            search_type="similarity", search_kwargs={"k": request.k}
        )
        docs = retriever.invoke(request.query)

        results = []
        for doc in docs:
            results.append({
                "page_content": doc.page_content,
                "metadata": doc.metadata,
            })

        logger.info(
            f"Query '{request.query}' against collection '{request.collection_name}' returned {len(results)} results."
        )

        return {
            "status": "success",
            "query": request.query,
            "collection_name": request.collection_name,
            "results_count": len(results),
            "results": results,
        }
    except Exception as e:
        logger.error(f"Failed to query vectorstore collection '{request.collection_name}': {e}")
        raise HTTPException(status_code=500, detail=str(e))


def main():
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("src.add_documents:app", host="0.0.0.0", port=port, reload=True)


if __name__ == "__main__":
    main()