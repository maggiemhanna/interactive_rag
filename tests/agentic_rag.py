import os
import json
import requests

from langchain_core.messages import HumanMessage, AIMessage, ToolMessage, SystemMessage
from utils.logging import setup_logging
from utils.messages_pretty_print import pretty_print_event

logger = setup_logging(name="test_agentic_rag")


def dict_to_message(msg_dict: dict):
    """Converts a dictionary representation of a message into a LangChain BaseMessage instance."""
    if not isinstance(msg_dict, dict):
        return None

    msg_type = str(msg_dict.get("type", "")).lower()
    content = msg_dict.get("content", "")

    if msg_type in ("human", "user"):
        return HumanMessage(content=content)
    elif msg_type in ("ai", "assistant"):
        tool_calls = msg_dict.get("tool_calls", [])
        return AIMessage(content=content, tool_calls=tool_calls)
    elif msg_type in ("tool", "function"):
        tool_name = msg_dict.get("name", msg_dict.get("tool_name", "Unknown Tool"))
        return ToolMessage(content=content, name=tool_name, tool_call_id=msg_dict.get("tool_call_id", "1"))
    elif msg_type == "system":
        return SystemMessage(content=content)
    return None


def test_standard_endpoint(url: str):
    logger.info("--- Testing Standard Endpoint ---")
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
                                pretty_print_event(msg_obj)
    else:
        res_data = response.json()
        logger.info(f"Status Code: {response.status_code}")
        logger.info("Printing all message components from standard response:")
        for msg in res_data.get("messages", []):
            msg_obj = dict_to_message(msg)
            if msg_obj:
                pretty_print_event(msg_obj)


def test_streaming_endpoint(url: str):
    logger.info("--- Testing Streaming Endpoint (SSE) ---")
    payload = {
        "query": "What is ARM Holdings?",
        "collection_name": "stock_market_vs",
        "stream": True,
    }
    logger.info(f"Sending streaming request to '{url}': {payload}")
    response = requests.post(url, json=payload, stream=True)
    response.raise_for_status()

    logger.info("Receiving and pretty printing stream events:")
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
                            pretty_print_event(msg_obj)
                except Exception as e:
                    logger.warning(f"Could not parse stream event: {e}")


if __name__ == "__main__":
    port = os.getenv("PORT", "8001")
    url = f"http://localhost:{port}/agentic_rag"

    try:
        test_standard_endpoint(url)
        print("\n" + "=" * 80 + "\n")
        test_streaming_endpoint(url)
    except Exception as e:
        logger.error(f"Error testing agentic_rag endpoint: {e}")
        if hasattr(e, "response") and e.response is not None:
            logger.error(f"Error response details: {e.response.text}")
