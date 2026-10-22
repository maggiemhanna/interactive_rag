import os
import json
import re
from typing import Annotated, List, Optional, Sequence, TypedDict, Generator, Dict, Any

from dotenv import load_dotenv, find_dotenv
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
import uvicorn
from langchain_chroma import Chroma
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools.retriever import create_retriever_tool
from langchain_google_genai import ChatGoogleGenerativeAI, GoogleGenerativeAIEmbeddings
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

from utils.auth import get_allowed_origins, get_current_user
from utils.chroma_storage import (
    ChromaLockTimeoutError,
    SQLITE_FILENAME,
    user_chroma_read_context,
    user_chroma_write_lock,
)
from utils.logging import setup_logging
from utils.messages_pretty_print import print_agent_header, pretty_print_event

# Load environment variables from .env file (searches parent directories and current directory / src)
load_dotenv(find_dotenv())
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

logger = setup_logging(name="agentic_rag")

# Module-scoped checkpointer persists multi-turn conversation history across warm requests;
# every HTTP request namespaces thread_id as f"{uid}:{request.thread_id}".
_MEMORY_SAVER = InMemorySaver()

app = FastAPI(title="Agentic RAG API")

_allowed_origins = get_allowed_origins()
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials="*" not in _allowed_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health_check():
    """Unauthenticated liveness/readiness probe endpoint for Cloud Run."""
    return {"status": "ok", "service": "agent-api"}


class AgenticRAGRequest(BaseModel):
    query: str = Field(..., description="Query or question for the Agentic RAG assistant")
    collection_name: str = Field(
        default="stock_market_vs", description="Name of the Chroma vectorstore collection"
    )
    thread_id: str = Field(default="1", description="LangGraph thread ID for session state")
    model_name: str = Field(
        default="gemini-2.5-flash", description="Vertex AI model name"
    )
    embedding_model: str = Field(
        default="gemini-embedding-001", description="Name of the Vertex AI embedding model"
    )
    stream: bool = Field(default=False, description="Whether to stream message events via Server-Sent Events (SSE)")


class AgentState(TypedDict):
    """State definition for the RAG Agent workflow."""

    messages: Annotated[Sequence[BaseMessage], add_messages]


def _verify_user_collection_exists(persist_directory: str, collection_name: str) -> None:
    """Verifies that the user's Chroma SQLite database and requested collection exist."""
    sqlite_path = os.path.join(persist_directory, SQLITE_FILENAME)
    if not os.path.exists(persist_directory) or not os.path.exists(sqlite_path):
        raise FileNotFoundError(f"Collection '{collection_name}' not found.")

    import chromadb

    client = chromadb.PersistentClient(path=persist_directory)
    existing = [col.name for col in client.list_collections()]
    if collection_name not in existing:
        raise FileNotFoundError(f"Collection '{collection_name}' not found.")


def _safe_tool_name(collection_name: str) -> str:
    """Constructs a Vertex AI / Gemini-compatible function tool name for the collection."""
    cleaned = re.sub(r"[^a-zA-Z0-9_]", "_", collection_name or "collection").strip("_")
    if not cleaned or cleaned[0].isdigit():
        cleaned = f"col_{cleaned}"
    return f"{cleaned}_retriever"[:64]


def get_retriever_tool(
    collection_name: str = "stock_market_vs",
    persist_directory: str = "chroma_db",
    k: int = 5,
    embedding_model: str = "gemini-embedding-001",
    description: str = "Search and retrieve relevant document chunks from collection '{collection_name}'."
):
    """Loads a Chroma vectorstore collection and creates a LangChain retriever tool."""
    _verify_user_collection_exists(persist_directory, collection_name)

    embeddings = GoogleGenerativeAIEmbeddings(
        model=embedding_model,
        project=os.getenv("GOOGLE_CLOUD_PROJECT"),
        vertexai=True,
    )

    vectorstore = Chroma(
        persist_directory=persist_directory,
        embedding_function=embeddings,
        collection_name=collection_name,
    )

    retriever = vectorstore.as_retriever(
        search_type="similarity", search_kwargs={"k": k}
    )

    tool_description = (
        description.format(collection_name=collection_name)
        if "{collection_name}" in description
        else description
    )

    return create_retriever_tool(
        retriever,
        name=_safe_tool_name(collection_name),
        description=tool_description,
    )


def setup_agent(
    retriever_tool,
    model_name: str = "gemini-2.5-flash",
    checkpointer: Optional[InMemorySaver] = None,
):
    """Constructs and compiles the LangGraph Agentic RAG graph."""

    llm = ChatGoogleGenerativeAI(
        model=model_name, 
        temperature=0,
        project=os.getenv("GOOGLE_CLOUD_PROJECT"),
        location=os.getenv("GOOGLE_CLOUD_LOCATION")
    )

    tools = [retriever_tool]
    model_with_tools = llm.bind_tools(tools)

    def model_call(state: AgentState) -> AgentState:
        system_prompt = SystemMessage(
            content=(
                "You are an expert AI assistant specializing in document analysis and retrieval. "
                "Use the available retriever tool to fetch relevant context when answering queries. "
            )
        )
        response = model_with_tools.invoke([system_prompt] + list(state["messages"]))
        return {"messages": [response]}

    def should_continue(state: AgentState) -> str:
        last_message = state["messages"][-1]
        if getattr(last_message, "tool_calls", None):
            return "continue"
        return "end"

    # Build LangGraph StateGraph
    graph = StateGraph(AgentState)
    graph.add_node("model_call", model_call)
    graph.add_node("tools", ToolNode(tools=tools))

    graph.add_edge(START, "model_call")
    graph.add_conditional_edges(
        "model_call",
        should_continue,
        {"continue": "tools", "end": END},
    )
    graph.add_edge("tools", "model_call")

    return graph.compile(checkpointer=checkpointer or _MEMORY_SAVER)


def extract_text_content(content) -> str:
    if isinstance(content, str):
        return content
    elif isinstance(content, list):
        text_parts = []
        for part in content:
            if isinstance(part, str):
                text_parts.append(part)
            elif isinstance(part, dict) and "text" in part:
                text_parts.append(part["text"])
        return "\n".join(text_parts)
    return str(content)


def _stream_agent_events(
    query: str,
    persist_directory: str,
    collection_name: str,
    thread_id: str,
    model_name: str,
    embedding_model: str,
) -> Generator[Dict[str, Any], None, None]:
    retriever_tool = get_retriever_tool(
        collection_name=collection_name,
        persist_directory=persist_directory,
        embedding_model=embedding_model,
    )
    agent = setup_agent(retriever_tool, model_name=model_name)

    config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
    input_messages = [HumanMessage(content=query)]

    for event in agent.stream(
        {"messages": input_messages}, config, stream_mode="values"
    ):
        messages = event.get("messages", [])
        if messages:
            last_msg = messages[-1]
            # Print all components of the message using utils/messages_pretty_print.py
            pretty_print_event(last_msg)

            msg_dict = {
                "type": getattr(last_msg, "type", "unknown"),
                "content": extract_text_content(getattr(last_msg, "content", "")),
            }
            if getattr(last_msg, "tool_calls", None):
                msg_dict["tool_calls"] = last_msg.tool_calls
            if getattr(last_msg, "name", None):
                msg_dict["name"] = last_msg.name

            yield {
                "event": "message",
                "message": msg_dict,
                "raw_message": last_msg,
            }


def run_agent_stream(
    query: str,
    collection_name: str = "stock_market_vs",
    thread_id: str = "1",
    model_name: str = "gemini-2.5-flash",
    embedding_model: str = "gemini-embedding-001",
    persist_directory: Optional[str] = None,
) -> Generator[Dict[str, Any], None, None]:
    """Streams graph message output, printing each message component using pretty_print_event."""
    if persist_directory is not None:
        yield from _stream_agent_events(
            query=query,
            persist_directory=persist_directory,
            collection_name=collection_name,
            thread_id=thread_id,
            model_name=model_name,
            embedding_model=embedding_model,
        )
        return

    cli_uid = os.getenv("DEV_USER_ID", "local-dev").strip() or "local-dev"
    with user_chroma_read_context(cli_uid) as resolved_dir:
        target_dir = (
            resolved_dir
            if os.path.exists(os.path.join(resolved_dir, SQLITE_FILENAME))
            else "chroma_db"
        )
        yield from _stream_agent_events(
            query=query,
            persist_directory=target_dir,
            collection_name=collection_name,
            thread_id=f"{cli_uid}:{thread_id}",
            model_name=model_name,
            embedding_model=embedding_model,
        )


@app.get("/collections")
def get_collections(uid: str = Depends(get_current_user)):
    """Returns a list of existing collection details from Chroma vectorstore."""
    try:
        with user_chroma_read_context(uid) as persist_directory:
            sqlite_path = os.path.join(persist_directory, SQLITE_FILENAME)
            if not os.path.exists(persist_directory) or not os.path.exists(sqlite_path):
                return {"collections": [], "details": []}

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
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching collections for uid='{uid}': {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/collections/{collection_name}")
def delete_collection(collection_name: str, uid: str = Depends(get_current_user)):
    """Deletes an existing collection from Chroma vectorstore."""
    try:
        with user_chroma_write_lock(uid) as persist_directory:
            sqlite_path = os.path.join(persist_directory, SQLITE_FILENAME)
            if not os.path.exists(persist_directory) or not os.path.exists(sqlite_path):
                raise HTTPException(
                    status_code=404,
                    detail=f"Collection '{collection_name}' not found.",
                )

            import chromadb

            client = chromadb.PersistentClient(path=persist_directory)
            existing = [col.name for col in client.list_collections()]
            if collection_name not in existing:
                raise HTTPException(
                    status_code=404,
                    detail=f"Collection '{collection_name}' not found.",
                )

            client.delete_collection(name=collection_name)
            logger.info(f"Successfully deleted Chroma collection '{collection_name}' for uid='{uid}'.")
            return {
                "status": "success",
                "message": f"Successfully deleted collection '{collection_name}'.",
                "collection_name": collection_name,
            }
    except ChromaLockTimeoutError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting collection '{collection_name}' for uid='{uid}': {e}")
        raise HTTPException(status_code=500, detail=str(e))


SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}


@app.post("/agentic_rag")
@app.post("/agent")
def agentic_rag_endpoint(
    request: AgenticRAGRequest,
    uid: str = Depends(get_current_user),
):
    """FastAPI endpoint to run the Agentic RAG assistant with a query."""
    scoped_thread_id = f"{uid}:{request.thread_id}"
    try:
        logger.info(
            f"Received Agentic RAG query from uid='{uid}': '{request.query}' "
            f"for collection '{request.collection_name}' (stream={request.stream})"
        )

        # Pre-validate user's synced ChromaDB & collection so missing collections return 404 even when stream=True
        with user_chroma_read_context(uid) as persist_directory:
            _verify_user_collection_exists(persist_directory, request.collection_name)

            if request.stream:
                def event_generator():
                    with user_chroma_read_context(uid) as stream_persist_dir:
                        for evt in run_agent_stream(
                            query=request.query,
                            collection_name=request.collection_name,
                            thread_id=scoped_thread_id,
                            model_name=request.model_name,
                            embedding_model=request.embedding_model,
                            persist_directory=stream_persist_dir,
                        ):
                            data = {
                                "event": evt["event"],
                                "message": evt["message"],
                            }
                            yield f"data: {json.dumps(data)}\n\n"
                        yield "data: [DONE]\n\n"

                return StreamingResponse(
                    event_generator(),
                    media_type="text/event-stream",
                    headers=SSE_HEADERS,
                )

            events = list(
                run_agent_stream(
                    query=request.query,
                    collection_name=request.collection_name,
                    thread_id=scoped_thread_id,
                    model_name=request.model_name,
                    embedding_model=request.embedding_model,
                    persist_directory=persist_directory,
                )
            )

        formatted_messages = [evt["message"] for evt in events]
        final_answer = ""
        for msg in reversed(formatted_messages):
            if msg["type"] == "ai" and msg.get("content"):
                final_answer = msg["content"]
                break

        return {
            "status": "success",
            "query": request.query,
            "answer": final_answer,
            "collection_name": request.collection_name,
            "messages": formatted_messages,
        }
    except FileNotFoundError as e:
        logger.error(f"Collection not found for uid='{uid}': {e}")
        raise HTTPException(status_code=404, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error running Agentic RAG for uid='{uid}': {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/agentic_rag/stream")
def agentic_rag_stream_endpoint(
    request: AgenticRAGRequest,
    uid: str = Depends(get_current_user),
):
    """Dedicated streaming endpoint using Server-Sent Events (SSE)."""
    request.stream = True
    return agentic_rag_endpoint(request=request, uid=uid)


def run_agent_interactive(query: str = "What is ARM Holdings?", collection_name: str = "stock_market_vs"):
    """CLI/Interactive runner to stream message output and pretty print message components."""
    print_agent_header("Agentic RAG Assistant Initialized", "magenta")
    logger.info(f"Running agent with query: '{query}'")
    for _ in run_agent_stream(query=query, collection_name=collection_name):
        pass


def main():
    print_agent_header("Agentic RAG Assistant Server Initialized", "magenta")
    port = int(os.getenv("PORT", "8001"))
    reload_enabled = os.getenv("UVICORN_RELOAD", "").strip().lower() in {"1", "true", "yes"}
    uvicorn.run("src.agentic_rag:app", host="0.0.0.0", port=port, reload=reload_enabled)


if __name__ == "__main__":
    main()
