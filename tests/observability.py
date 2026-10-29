"""Automated offline unit test suite for Layer 5: Observability & AI Monitoring."""

import os
import unittest

from fastapi import Depends
from fastapi.testclient import TestClient

from src.core.app_factory import create_app
from src.core.auth import get_current_user
from src.core import context as context_module
from src.core.compliance.state import (
    GUARDRAIL_FILTER_LABELS,
    get_guardrail_actions,
    get_retrieved_chunks_count,
    mark_model_armor_active,
    mark_pii_filtered,
    record_guardrail_action,
    record_retrieved_chunks,
    was_model_armor_active,
    was_pii_filtered,
)
from src.core.context import (
    RequestContext,
    RequestCorrelationMiddleware,
    bind_request_context,
    extract_span_id,
    extract_trace_id,
    get_active_span_id,
    get_request_context,
    get_request_id,
    get_request_uid,
    get_trace_id,
    reset_request_context,
    set_active_span_id,
    set_request_uid,
)
from src.core.observability.state import (
    get_elapsed_latency_ms,
    get_recorded_spans,
    get_retrieval_scores_summary,
    get_stage_latency_ms,
    get_token_usage_summary,
    get_ttft_ms,
    record_retrieval_scores,
    record_span,
    record_token_usage,
    record_ttft,
)


class TestPhase1ContextAndPackageSeparation(unittest.TestCase):
    """Phase 1 unit tests: neutral `src.core.context`, `src.core.compliance.state`, and `src.core.observability.state`."""

    def test_legacy_compliance_context_file_removed_and_domain_functions_separated(self) -> None:
        """Verifies `src/core/compliance/context.py` is deleted and domain functions live in their `state.py` modules."""
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        legacy_path = os.path.join(repo_root, "src", "core", "compliance", "context.py")
        neutral_path = os.path.join(repo_root, "src", "core", "context.py")
        comp_state_path = os.path.join(repo_root, "src", "core", "compliance", "state.py")
        obs_pkg_path = os.path.join(repo_root, "src", "core", "observability", "__init__.py")
        obs_state_path = os.path.join(repo_root, "src", "core", "observability", "state.py")

        self.assertFalse(
            os.path.exists(legacy_path),
            "src/core/compliance/context.py must be removed so src/core/context.py is the single source of truth.",
        )
        self.assertTrue(os.path.isfile(neutral_path))
        self.assertTrue(os.path.isfile(comp_state_path))
        self.assertTrue(os.path.isfile(obs_pkg_path))
        self.assertTrue(os.path.isfile(obs_state_path))

        # Verify domain-specific compliance and observability helpers are NOT defined on src.core.context
        for domain_attr in (
            "GUARDRAIL_FILTER_LABELS",
            "mark_pii_filtered",
            "record_guardrail_action",
            "get_elapsed_latency_ms",
            "record_ttft",
            "record_token_usage",
            "record_retrieval_scores",
            "record_span",
        ):
            self.assertFalse(
                hasattr(context_module, domain_attr),
                f"{domain_attr} is domain-specific and must not live in src.core.context",
            )

    def test_extract_trace_and_span_id_from_cloud_trace_header(self) -> None:
        """Verifies `X-Cloud-Trace-Context: TRACE_ID/SPAN_ID;o=1` parses both trace_id and 16-char hex span_id."""
        header = "105445aa7843bc8bf206b12000100000/255;o=1"
        self.assertEqual(extract_trace_id(header), "105445aa7843bc8bf206b12000100000")
        self.assertEqual(extract_span_id(header), "00000000000000ff")

        hex_span_header = "105445aa7843bc8bf206b12000100000/a1b2c3d4e5f60001;o=1"
        self.assertEqual(extract_span_id(hex_span_header), "a1b2c3d4e5f60001")
        self.assertEqual(extract_span_id(""), "")
        self.assertEqual(extract_span_id("invalid"), "")

    def test_request_context_accumulators_and_lifecycle(self) -> None:
        """Verifies `RequestContext` binds/resets cleanly and supports compliance and observability `state.py` helpers."""
        self.assertIsNone(get_request_context())

        ctx = RequestContext(
            request_id="req-phase1-unit-001",
            trace_id="105445aa7843bc8bf206b12000100000",
            root_span_id="0000000000000001",
            service_name="agent-api",
            method="POST",
            path="/agent",
        )
        token = bind_request_context(ctx)
        try:
            self.assertEqual(get_request_id(), "req-phase1-unit-001")
            self.assertEqual(get_trace_id(), "105445aa7843bc8bf206b12000100000")
            self.assertEqual(get_active_span_id(), "0000000000000001")

            set_request_uid("tenant_alpha")
            self.assertEqual(get_request_uid(), "tenant_alpha")

            prev_span = set_active_span_id("a1b2c3d400000002")
            self.assertEqual(prev_span, "0000000000000001")
            self.assertEqual(get_active_span_id(), "a1b2c3d400000002")

            # Compliance guardrail & retrieval state helpers in src.core.compliance.state
            self.assertIn("sdp", GUARDRAIL_FILTER_LABELS)
            mark_pii_filtered(True)
            mark_model_armor_active(True)
            record_guardrail_action("sdp", "redacted", "input")
            record_retrieved_chunks(4)
            self.assertTrue(was_pii_filtered())
            self.assertTrue(was_model_armor_active())
            self.assertEqual(
                get_guardrail_actions(),
                [
                    {
                        "filter": "sdp",
                        "label": GUARDRAIL_FILTER_LABELS["sdp"],
                        "action": "redacted",
                        "stage": "input",
                        "details": [],
                    }
                ],
            )
            self.assertEqual(get_retrieved_chunks_count(), 4)

            # Observability state helpers in src.core.observability.state
            record_token_usage(input_tokens=400, output_tokens=100, total_tokens=500)
            record_token_usage(input_tokens=600, output_tokens=150)
            self.assertEqual(
                get_token_usage_summary(),
                {
                    "input_tokens": 1000,
                    "output_tokens": 250,
                    "total_tokens": 1250,
                    "llm_calls": 2,
                },
            )

            record_retrieval_scores([0.92, 0.80, 0.68])
            self.assertEqual(
                get_retrieval_scores_summary(),
                {"top_similarity_score": 0.92, "mean_similarity_score": 0.8},
            )

            record_span(
                {
                    "name": "agent.input_guard",
                    "span_id": "a1b2c3d400000002",
                    "parent_span_id": "0000000000000001",
                    "stage": "input_guard",
                    "duration_ms": 42.5,
                    "status": "ok",
                }
            )
            record_span(
                {
                    "name": "agent.model_call",
                    "span_id": "a1b2c3d400000003",
                    "parent_span_id": "0000000000000001",
                    "stage": "llm",
                    "duration_ms": 510.25,
                    "status": "ok",
                }
            )
            self.assertEqual(len(get_recorded_spans()), 2)
            self.assertEqual(
                get_stage_latency_ms(),
                {"input_guard_ms": 42.5, "llm_ms": 510.25},
            )

            ttft = record_ttft()
            self.assertIsNotNone(ttft)
            self.assertEqual(get_ttft_ms(), ttft)
            self.assertIsNotNone(get_elapsed_latency_ms())
        finally:
            reset_request_context(token)

        self.assertIsNone(get_request_context())

    def test_request_correlation_middleware_propagates_request_and_trace_context(self) -> None:
        """Verifies `RequestCorrelationMiddleware` binds `RequestContext` and echoes `X-Request-Id`."""
        app = create_app(title="Test Service", service_name="test-api")

        @app.get("/inspect-context")
        def inspect_context(uid: str = Depends(get_current_user)):
            return {
                "uid": uid,
                "context_uid": get_request_uid(),
                "request_id": get_request_id(),
                "trace_id": get_trace_id(),
                "span_id": get_active_span_id(),
            }

        self.assertTrue(
            any(m.cls is RequestCorrelationMiddleware for m in app.user_middleware),
            "create_app must wire RequestCorrelationMiddleware from src.core.context",
        )

        client = TestClient(app)
        with unittest.mock.patch.dict(os.environ, {"AUTH_DISABLED": "true"}, clear=False):
            resp = client.get(
                "/inspect-context",
                headers={
                    "X-Dev-User-Id": "obs_user",
                    "X-Request-Id": "req-middleware-check-01",
                    "X-Cloud-Trace-Context": "abcdef0123456789abcdef0123456789/16;o=1",
                },
            )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers.get("X-Request-Id"), "req-middleware-check-01")
        body = resp.json()
        self.assertEqual(body["uid"], "obs_user")
        self.assertEqual(body["context_uid"], "obs_user")
        self.assertEqual(body["request_id"], "req-middleware-check-01")
        self.assertEqual(body["trace_id"], "abcdef0123456789abcdef0123456789")
        self.assertEqual(body["span_id"], "0000000000000010")


class TestPhase2StructuredLoggingAndTraceCorrelation(unittest.TestCase):
    """Phase 2 unit tests: `is_structured_logging_enabled`, `GCPJsonFormatter`, `RequestContextLogFilter`, and query scrubbing."""

    def tearDown(self) -> None:
        from src.core.logger import setup_logging

        with unittest.mock.patch.dict(
            os.environ,
            {"STRUCTURED_LOGGING": "false", "K_SERVICE": ""},
            clear=False,
        ):
            setup_logging(force=True)

    def test_is_structured_logging_enabled_env_and_cloud_run_defaults(self) -> None:
        """Verifies `is_structured_logging_enabled()` defaults to `is_cloud_run()` and honors `STRUCTURED_LOGGING`."""
        from src.core.config import is_structured_logging_enabled

        with unittest.mock.patch.dict(
            os.environ,
            {"STRUCTURED_LOGGING": "", "K_SERVICE": ""},
            clear=False,
        ):
            self.assertFalse(is_structured_logging_enabled())

        with unittest.mock.patch.dict(
            os.environ,
            {"STRUCTURED_LOGGING": "", "K_SERVICE": "interactive-rag-agent"},
            clear=False,
        ):
            self.assertTrue(is_structured_logging_enabled())

        with unittest.mock.patch.dict(
            os.environ,
            {"STRUCTURED_LOGGING": "true", "K_SERVICE": ""},
            clear=False,
        ):
            self.assertTrue(is_structured_logging_enabled())

        with unittest.mock.patch.dict(
            os.environ,
            {"STRUCTURED_LOGGING": "false", "K_SERVICE": "interactive-rag-agent"},
            clear=False,
        ):
            self.assertFalse(is_structured_logging_enabled())

    def test_gcp_json_formatter_emits_single_line_json_with_trace_and_source_location(self) -> None:
        """Verifies `GCPJsonFormatter` produces single-line GCP structured JSON enriched from `RequestContext`."""
        import io
        import json
        import logging

        from src.core.observability import GCPJsonFormatter, RequestContextLogFilter

        ctx = RequestContext(
            request_id="req-phase2-json-001",
            trace_id="105445aa7843bc8bf206b12000100000",
            root_span_id="000000000000002a",
            service_name="agent-api",
            uid="alice_logger",
            method="POST",
            path="/agent",
        )
        token = bind_request_context(ctx)
        try:
            stream = io.StringIO()
            handler = logging.StreamHandler(stream)
            handler.setFormatter(GCPJsonFormatter())
            handler.addFilter(RequestContextLogFilter())

            test_logger = logging.getLogger("test.phase2.json")
            test_logger.handlers = [handler]
            test_logger.setLevel(logging.INFO)
            test_logger.propagate = False

            with unittest.mock.patch.dict(
                os.environ,
                {"GOOGLE_CLOUD_PROJECT": "interactive-rag-proj"},
                clear=False,
            ):
                test_logger.info("Completed agent turn successfully")

            raw_line = stream.getvalue().strip()
            self.assertNotIn("\n", raw_line, "GCPJsonFormatter must emit single-line JSON")
            parsed = json.loads(raw_line)

            self.assertEqual(parsed["severity"], "INFO")
            self.assertEqual(parsed["message"], "Completed agent turn successfully")
            self.assertEqual(parsed["logger"], "test.phase2.json")
            self.assertEqual(parsed["service"], "agent-api")
            self.assertEqual(parsed["request_id"], "req-phase2-json-001")
            self.assertEqual(parsed["uid"], "alice_logger")
            self.assertEqual(parsed["http_method"], "POST")
            self.assertEqual(parsed["http_path"], "/agent")
            self.assertEqual(
                parsed["logging.googleapis.com/trace"],
                "projects/interactive-rag-proj/traces/105445aa7843bc8bf206b12000100000",
            )
            self.assertEqual(parsed["logging.googleapis.com/spanId"], "000000000000002a")
            self.assertTrue(parsed["logging.googleapis.com/trace_sampled"])
            self.assertIn("logging.googleapis.com/sourceLocation", parsed)
            self.assertEqual(
                parsed["logging.googleapis.com/sourceLocation"]["file"],
                "observability.py",
            )
        finally:
            reset_request_context(token)

    def test_request_context_log_filter_scrubs_raw_queries_in_extra_and_inline_messages(self) -> None:
        """Verifies `RequestContextLogFilter` scrubs raw `query` / `prompt` strings into `query_sha256` and `query_chars`."""
        import io
        import json
        import logging

        from src.core.context import hash_query
        from src.core.observability import GCPJsonFormatter, RequestContextLogFilter

        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(GCPJsonFormatter())
        handler.addFilter(RequestContextLogFilter())

        test_logger = logging.getLogger("test.phase2.scrub")
        test_logger.handlers = [handler]
        test_logger.setLevel(logging.INFO)
        test_logger.propagate = False

        secret_query = "What is CEO Jane Doe's home address and SSN?"
        expected_hash = hash_query(secret_query)

        # 1. Raw query passed in extra={...}
        test_logger.info("Executing vector search", extra={"query": secret_query})
        # 2. Raw query accidentally interpolated inline as query='...'
        test_logger.warning(f"Slow query detected: query='{secret_query}'")

        lines = [line for line in stream.getvalue().splitlines() if line.strip()]
        self.assertEqual(len(lines), 2)

        first_payload = json.loads(lines[0])
        self.assertNotIn("query", first_payload)
        self.assertNotIn(secret_query, lines[0])
        self.assertEqual(first_payload["query_sha256"], expected_hash)
        self.assertEqual(first_payload["query_chars"], len(secret_query))

        second_payload = json.loads(lines[1])
        self.assertNotIn(secret_query, lines[1])
        self.assertIn(f"query_sha256='{expected_hash}'", second_payload["message"])
        self.assertIn(f"query_chars={len(secret_query)}", second_payload["message"])

    def test_setup_logging_switches_between_structured_json_and_coloredlogs(self) -> None:
        """Verifies `setup_logging()` installs `GCPJsonFormatter` when `STRUCTURED_LOGGING=true` and `coloredlogs` when `false`."""
        import logging

        from src.core.logger import setup_logging
        from src.core.observability import GCPJsonFormatter, RequestContextLogFilter

        with unittest.mock.patch.dict(
            os.environ,
            {"STRUCTURED_LOGGING": "true", "K_SERVICE": ""},
            clear=False,
        ):
            logger_json = setup_logging(name="phase2.mode.test", force=True)
            root_handlers = logging.getLogger().handlers
            self.assertTrue(
                any(isinstance(getattr(h, "formatter", None), GCPJsonFormatter) for h in root_handlers),
                "setup_logging() must install GCPJsonFormatter when STRUCTURED_LOGGING=true",
            )
            self.assertTrue(
                any(isinstance(f, RequestContextLogFilter) for f in logger_json.filters),
                "setup_logging() must attach RequestContextLogFilter to returned logger",
            )

        with unittest.mock.patch.dict(
            os.environ,
            {"STRUCTURED_LOGGING": "false", "K_SERVICE": ""},
            clear=False,
        ):
            setup_logging(name="phase2.mode.test", force=True)
            root_handlers = logging.getLogger().handlers
            self.assertFalse(
                any(isinstance(getattr(h, "formatter", None), GCPJsonFormatter) for h in root_handlers),
                "setup_logging() must use coloredlogs formatter when STRUCTURED_LOGGING=false",
            )


class TestPhase3DistributedTracingAndStageSpans(unittest.TestCase):
    """Phase 3 unit tests: hierarchical span tracer (`start_span`), active span correlation, and pipeline spans."""

    def test_nested_spans_preserve_hierarchy_and_restore_active_span_id_and_log_span_id(self) -> None:
        """Verifies parent-child `span_id` hierarchy, `active_span_id` restoration, and log record span correlation."""
        import io
        import json
        import logging

        from src.core.observability import (
            GCPJsonFormatter,
            RequestContextLogFilter,
            register_span_listener,
            start_span,
        )

        ctx = RequestContext(
            request_id="req-phase3-001",
            trace_id="105445aa7843bc8bf206b12000100000",
            root_span_id="0000000000000001",
            service_name="agent-api",
            uid="alice_tracer",
        )
        token = bind_request_context(ctx)
        listener_spans = []
        unregister = register_span_listener(listener_spans.append)

        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(GCPJsonFormatter())
        handler.addFilter(RequestContextLogFilter())
        test_logger = logging.getLogger("test.phase3.span_log")
        test_logger.handlers = [handler]
        test_logger.setLevel(logging.INFO)
        test_logger.propagate = False

        try:
            self.assertEqual(get_active_span_id(), "0000000000000001")
            with start_span("agent.turn", stage="turn", model_name="gemini-2.5-flash") as turn_span:
                turn_span_id = turn_span["span_id"]
                self.assertEqual(get_active_span_id(), turn_span_id)
                self.assertEqual(turn_span["parent_span_id"], "0000000000000001")

                with start_span("agent.model_call", stage="llm", model_name="gemini-2.5-flash") as llm_span:
                    llm_span_id = llm_span["span_id"]
                    self.assertEqual(get_active_span_id(), llm_span_id)
                    self.assertEqual(llm_span["parent_span_id"], turn_span_id)
                    test_logger.info("Inside nested LLM span")

                # Exiting child span restores parent's span_id
                self.assertEqual(get_active_span_id(), turn_span_id)

                with start_span("agent.retriever_tool", stage="retrieval", k=5) as ret_span:
                    ret_span_id = ret_span["span_id"]
                    self.assertEqual(get_active_span_id(), ret_span_id)
                    self.assertEqual(ret_span["parent_span_id"], turn_span_id)
                    ret_span["attributes"]["retrieved_chunks"] = 3

            # Exiting outer span restores root_span_id
            self.assertEqual(get_active_span_id(), "0000000000000001")

            spans = get_recorded_spans()
            self.assertEqual(len(spans), 3)
            self.assertEqual(len(listener_spans), 3)

            # Spans finish in inner-to-outer order: model_call, retriever_tool, turn
            self.assertEqual([s["name"] for s in spans], ["agent.model_call", "agent.retriever_tool", "agent.turn"])
            self.assertEqual(spans[0]["parent_span_id"], turn_span_id)
            self.assertEqual(spans[1]["parent_span_id"], turn_span_id)
            self.assertEqual(spans[1]["attributes"]["retrieved_chunks"], 3)
            self.assertEqual(spans[2]["parent_span_id"], "0000000000000001")
            for s in spans:
                self.assertEqual(s["status"], "ok")
                self.assertGreaterEqual(s["duration_ms"], 0.0)

            stage_latencies = get_stage_latency_ms()
            self.assertIn("llm_ms", stage_latencies)
            self.assertIn("retrieval_ms", stage_latencies)
            self.assertIn("turn_ms", stage_latencies)

            # Log emitted inside child span must carry that child span's logging.googleapis.com/spanId
            log_payload = json.loads(stream.getvalue().strip())
            self.assertEqual(log_payload["logging.googleapis.com/spanId"], llm_span_id)
        finally:
            unregister()
            reset_request_context(token)

    def test_span_error_status_and_raw_query_attribute_scrubbing(self) -> None:
        """Verifies exceptions inside `start_span` mark `status='error'` and scrub raw `query` attributes."""
        from src.core.context import hash_query
        from src.core.observability import start_span

        ctx = RequestContext(
            request_id="req-phase3-err",
            trace_id="205445aa7843bc8bf206b12000100000",
            root_span_id="0000000000000009",
            service_name="ingestion-api",
        )
        token = bind_request_context(ctx)
        secret_query = "Confidential acquisition target name?"
        try:
            with self.assertRaises(RuntimeError):
                with start_span("ingestion.search_collection", stage="retrieval", query=secret_query):
                    raise RuntimeError("Simulated vector index error")

            # Active span ID restored even after exception
            self.assertEqual(get_active_span_id(), "0000000000000009")
            spans = get_recorded_spans()
            self.assertEqual(len(spans), 1)
            err_span = spans[0]
            self.assertEqual(err_span["status"], "error")
            self.assertEqual(err_span["attributes"]["error_type"], "RuntimeError")
            self.assertNotIn("query", err_span["attributes"])
            self.assertEqual(err_span["attributes"]["query_sha256"], hash_query(secret_query))
            self.assertEqual(err_span["attributes"]["query_chars"], len(secret_query))
        finally:
            reset_request_context(token)

    def test_end_to_end_pipeline_spans_across_ingestion_and_agent_execution(self) -> None:
        """Verifies `start_span` records stage latencies across PDF splitting, indexing, search, and LangGraph nodes."""
        import shutil
        import tempfile
        from unittest.mock import patch

        from langchain_core.messages import HumanMessage

        from src.agent import graph as agent_graph_module
        from src.agent.tools import get_retriever_tool
        from src.core.storage import chroma as chroma_module
        from src.core.storage import release_client_cache
        from src.ingestion.service import index_documents, search_collection
        from src.ingestion.splitter import split_pdfs
        from tests.ai_security import (
            DeterministicFakeEmbeddings,
            GroundedCitingChatModel,
            make_test_pdf,
        )

        temp_dir = tempfile.mkdtemp(prefix="obs_phase3_")
        pdf_dir = os.path.join(temp_dir, "pdfs")
        chroma_root = os.path.join(temp_dir, "chroma")
        user_chroma_dir = os.path.join(chroma_root, "alice_e2e")
        os.makedirs(pdf_dir, exist_ok=True)
        os.makedirs(user_chroma_dir, exist_ok=True)
        pdf_path = os.path.join(pdf_dir, "report.pdf")
        with open(pdf_path, "wb") as f:
            f.write(make_test_pdf("Annual revenue grew 24 percent in 2025."))

        ctx = RequestContext(
            request_id="req-phase3-e2e",
            trace_id="305445aa7843bc8bf206b12000100000",
            root_span_id="0000000000000010",
            service_name="agent-api",
            uid="alice_e2e",
        )
        token = bind_request_context(ctx)
        try:
            with patch.object(
                chroma_module,
                "get_embeddings",
                side_effect=lambda *args, **kwargs: DeterministicFakeEmbeddings(),
            ), patch.dict(
                os.environ,
                {
                    "GOOGLE_CLOUD_PROJECT": "interactive-rag",
                    "GOOGLE_CLOUD_LOCATION": "us-central1",
                    "MODEL_ARMOR_TEMPLATE_ID": "",
                    "LOCAL_CHROMA_ROOT": chroma_root,
                    "GCS_BUCKET_NAME": "",
                },
                clear=False,
            ):
                # 1. Split PDFs -> ingestion.split_pdfs (stage="chunking")
                docs = split_pdfs([pdf_path], chunk_size=300, chunk_overlap=30)
                self.assertGreaterEqual(len(docs), 1)

                # 2. Index documents -> ingestion.index_documents (stage="embedding")
                indexed = index_documents(
                    docs,
                    persist_directory=user_chroma_dir,
                    collection_name="obs_reports",
                    embedding_model="gemini-embedding-001",
                )
                self.assertIsNotNone(indexed)

                # 3. Direct vector search -> ingestion.search_collection (stage="retrieval")
                results = search_collection(
                    uid="alice_e2e",
                    query="Annual revenue growth",
                    collection_name="obs_reports",
                    k=2,
                    embedding_model="gemini-embedding-001",
                )
                self.assertGreaterEqual(len(results), 1)

                # 4. Build and run LangGraph agent -> agent.input_guard, agent.model_call, agent.retriever_tool, agent.output_guard
                retriever_tool = get_retriever_tool(
                    persist_directory=user_chroma_dir,
                    collection_name="obs_reports",
                    embedding_model="gemini-embedding-001",
                    k=2,
                )
                with patch.object(
                    agent_graph_module,
                    "ChatGoogleGenerativeAI",
                    side_effect=lambda *args, **kwargs: GroundedCitingChatModel(*args, **kwargs),
                ):
                    agent = agent_graph_module.build_agent(
                        retriever_tool=retriever_tool,
                        model_name="gemini-2.5-flash",
                    )
                    state = agent.invoke(
                        {"messages": [HumanMessage(content="What was annual revenue growth?")]},
                        config={"configurable": {"thread_id": "alice_e2e:obs-thread"}},
                    )
                    self.assertTrue(len(state["messages"]) >= 2)

            recorded_names = [s["name"] for s in get_recorded_spans()]
            for expected_span_name in (
                "ingestion.split_pdfs",
                "ingestion.index_documents",
                "ingestion.search_collection",
                "agent.input_guard",
                "agent.model_call",
                "agent.retriever_tool",
                "agent.output_guard",
            ):
                self.assertIn(
                    expected_span_name,
                    recorded_names,
                    f"Expected span '{expected_span_name}' in recorded spans {recorded_names}",
                )

            stage_latencies = get_stage_latency_ms()
            for expected_stage_key in (
                "chunking_ms",
                "embedding_ms",
                "retrieval_ms",
                "input_guard_ms",
                "llm_ms",
                "output_guard_ms",
            ):
                self.assertIn(
                    expected_stage_key,
                    stage_latencies,
                    f"Expected stage metric '{expected_stage_key}' in {stage_latencies}",
                )
                self.assertGreaterEqual(stage_latencies[expected_stage_key], 0.0)
        finally:
            reset_request_context(token)
            release_client_cache(user_chroma_dir)
            shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()

