import os
from typing import Optional

from langchain_core.messages import SystemMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from src.agent.schemas import AgentState
from src.core.config import DEFAULT_LLM_MODEL, DEFAULT_LOCATION

# Module-scoped checkpointer persists multi-turn conversation history across warm requests;
# every HTTP request namespaces thread_id as f"{uid}:{request.thread_id}".
MEMORY_SAVER = InMemorySaver()


def build_agent(
    retriever_tool,
    model_name: str = DEFAULT_LLM_MODEL,
    checkpointer: Optional[InMemorySaver] = None,
):
    """Constructs and compiles the LangGraph Agentic RAG graph."""
    llm = ChatGoogleGenerativeAI(
        model=model_name,
        temperature=0,
        project=os.getenv("GOOGLE_CLOUD_PROJECT"),
        location=os.getenv("GOOGLE_CLOUD_LOCATION", DEFAULT_LOCATION),
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

    return graph.compile(checkpointer=checkpointer or MEMORY_SAVER)
