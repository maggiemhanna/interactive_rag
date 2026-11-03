"""Live HTTP smoke-test script for the Agent API (`/agent` and `/agent/stream`).

Sends sample synchronous JSON and Server-Sent Events (SSE) streaming queries to
a locally running `agent-api` service and pretty-prints the returned LangChain
message trace using `src.agent.display.print_event`.
"""

import json
from typing import Any, Dict, Optional

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
import requests

from src.agent.display import print_event
from src.core.config import AGENT_PORT, get_service_port
from src.core.logger import setup_logging

logger = setup_logging(name="tests.agent")


def dict_to_message(msg_dict: Dict[str, Any]) -> Optional[BaseMessage]:
    """Converts a serialized message dictionary into a LangChain `BaseMessage` instance."""
    if not isinstance(msg_dict, dict):
        return None

    msg_type = str(msg_dict.get("type", "")).lower()
    content = msg_dict.get("content", "")

    if msg_type in ("human", "user"):
        return HumanMessage(content=content)
    if msg_type in ("ai", "assistant"):
        tool_calls = msg_dict.get("tool_calls", [])
        return AIMessage(content=content, tool_calls=tool_calls)
    if msg_type in ("tool", "function"):
        tool_name = msg_dict.get("name", msg_dict.get("tool_name", "Unknown Tool"))
        return ToolMessage(
            content=content,
            name=tool_name,
            tool_call_id=msg_dict.get("tool_call_id", "1"),
        )
    if msg_type == "system":
        return SystemMessage(content=content)
    return None


def test_standard_endpoint(url: str) -> None:
    """Sends a synchronous (`stream=False`) query to `POST /agent` and prints the response trace."""
    logger.info("--- Testing Standard Endpoint (/agent) ---")
    payload = {
        "query": "What is ARM Holdings?",
        "collection_name": "stock_market_vs",
        "stream": False,
    }
    logger.info(f"Sending request to '{url}': {payload}")
    response = requests.post(url, json=payload)
    response.raise_for_status()
    content_type = response.headers.get("content-type", "")

    if "text/event-stream" in content_type:
        logger.info("Received streaming response (text/event-stream):")
        for line in response.iter_lines():
            if line:
                decoded = line.decode("utf-8")
                if decoded.startswith("data: "):
                    data_str = decoded[6:].strip()
                    if data_str != "[DONE]":
                        event_data = json.loads(data_str)
                        if "message" in event_data:
                            msg_obj = dict_to_message(event_data["message"])
                            if msg_obj:
                                print_event(msg_obj)
    else:
        res_data = response.json()
        logger.info(f"Status Code: {response.status_code}")
        logger.info("Printing all message components from standard response:")
        for msg in res_data.get("messages", []):
            msg_obj = dict_to_message(msg)
            if msg_obj:
                print_event(msg_obj)


def test_streaming_endpoint(stream_url: str) -> None:
    """Sends a streaming SSE query to `POST /agent/stream` and prints each message frame."""
    logger.info("--- Testing Streaming Endpoint (/agent/stream SSE) ---")
    payload = {
        "query": "What is ARM Holdings?",
        "collection_name": "stock_market_vs",
    }
    logger.info(f"Sending streaming request to '{stream_url}': {payload}")
    response = requests.post(stream_url, json=payload, stream=True)
    response.raise_for_status()

    logger.info("Receiving and pretty-printing stream events:")
    for line in response.iter_lines():
        if line:
            decoded = line.decode("utf-8")
            if decoded.startswith("data: "):
                data_str = decoded[6:].strip()
                if data_str == "[DONE]":
                    break
                try:
                    event_data = json.loads(data_str)
                    if "message" in event_data:
                        msg_obj = dict_to_message(event_data["message"])
                        if msg_obj:
                            print_event(msg_obj)
                except Exception as exc:
                    logger.warning(f"Could not parse stream event: {exc}")


if __name__ == "__main__":
    port = get_service_port(AGENT_PORT)
    base_url = f"http://localhost:{port}"

    try:
        test_standard_endpoint(f"{base_url}/agent")
        print("\n" + "=" * 80 + "\n")
        test_streaming_endpoint(f"{base_url}/agent/stream")
    except Exception as exc:
        logger.error(f"Error testing agent endpoint: {exc}")
        if hasattr(exc, "response") and exc.response is not None:
            logger.error(f"Error response details: {exc.response.text}")
