import os
import json
from typing import Annotated, List, Optional, Sequence, TypedDict, Generator, Dict, Any

from dotenv import load_dotenv, find_dotenv
from fastapi import FastAPI, HTTPException
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

from utils.logging import setup_logging
from utils.messages_pretty_print import print_agent_header, pretty_print_event

# Load environment variables from .env file (searches parent directories and current directory / src)
load_dotenv(find_dotenv())
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

logger = setup_logging(name="agentic_rag")

app = FastAPI(title="Agentic RAG API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class AgenticRAGRequest(BaseModel):
    query: str = Field(..., description="Query or question for the Agentic RAG assistant")
    collection_name: str = Field(
        default="stock_market_vs", description="Name of the Chroma vectorstore collection"
    )
    thread_id: str = Field(default="1", description="LangGraph thread ID for session state")
    model_name: str = Field(
        default="gemini-2.5-flash", description="Vertex AI model name"
    )
    stream: bool = Field(default=False, description="Whether to stream message events via Server-Sent Events (SSE)")


class AgentState(TypedDict):
    """State definition for the RAG Agent workflow."""

    messages: Annotated[Sequence[BaseMessage], add_messages]


def get_retriever_tool(
    collection_name: str = "stock_market_vs",
    persist_directory: str = "chroma_db",
    k: int = 5,
    description: str = "Search and retrieve relevant document chunks from collection '{collection_name}'."
):
    """Loads a Chroma vectorstore collection and creates a LangChain retriever tool."""
    if not os.path.exists(persist_directory):
        raise FileNotFoundError(f"Chroma DB directory '{persist_directory}' not found.")

    embeddings = GoogleGenerativeAIEmbeddings(
        model="gemini-embedding-001",
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
        name=f"{collection_name}_retriever",
        description=tool_description,
    )


def setup_agent(retriever_tool, model_name: str = "gemini-2.5-flash"):
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

    checkpointer = InMemorySaver()
    return graph.compile(checkpointer=checkpointer)


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


def run_agent_stream(
    query: str,
    collection_name: str = "stock_market_vs",
    thread_id: str = "1",
    model_name: str = "gemini-2.5-flash",
) -> Generator[Dict[str, Any], None, None]:
    """Streams graph message output, printing each message component using pretty_print_event."""
    retriever_tool = get_retriever_tool(collection_name=collection_name)
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



@app.post("/agentic_rag")
@app.post("/agent")
def agentic_rag_endpoint(request: AgenticRAGRequest):
    """FastAPI endpoint to run the Agentic RAG assistant with a query."""
    try:
        logger.info(f"Received Agentic RAG query: '{request.query}' for collection '{request.collection_name}' (stream={request.stream})")
        
        if request.stream:
            def event_generator():
                for evt in run_agent_stream(
                    query=request.query,
                    collection_name=request.collection_name,
                    thread_id=request.thread_id,
                    model_name=request.model_name,
                ):
                    data = {
                        "event": evt["event"],
                        "message": evt["message"],
                    }
                    yield f"data: {json.dumps(data)}\n\n"
                yield "data: [DONE]\n\n"

            return StreamingResponse(event_generator(), media_type="text/event-stream")

        events = list(
            run_agent_stream(
                query=request.query,
                collection_name=request.collection_name,
                thread_id=request.thread_id,
                model_name=request.model_name,
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
        logger.error(f"Vector store directory not found: {e}")
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error(f"Error running Agentic RAG: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/agentic_rag/stream")
def agentic_rag_stream_endpoint(request: AgenticRAGRequest):
    """Dedicated streaming endpoint using Server-Sent Events (SSE)."""
    request.stream = True
    return agentic_rag_endpoint(request)


def run_agent_interactive(query: str = "What is ARM Holdings?", collection_name: str = "stock_market_vs"):
    """CLI/Interactive runner to stream message output and pretty print message components."""
    print_agent_header("Agentic RAG Assistant Initialized", "magenta")
    logger.info(f"Running agent with query: '{query}'")
    for _ in run_agent_stream(query=query, collection_name=collection_name):
        pass


def main():
    print_agent_header("Agentic RAG Assistant Server Initialized", "magenta")
    port = int(os.getenv("PORT", "8001"))
    uvicorn.run(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
