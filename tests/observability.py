"""Automated offline unit test suite for Layer 5: Observability & AI Monitoring (`src.core.observability`).

Verifies:
- Neutral request correlation (`src.core.context`) separated from compliance (`src.core.compliance.state`) and observability (`src.core.observability.state`).
- Structured single-line GCP JSON logging (`GCPJsonFormatter`), trace/span enrichment, and PII query scrubbing (`RequestContextLogFilter`).
- Hierarchical stage tracing (`start_span`), active span propagation, and end-to-end pipeline spans across ingestion, search, and LangGraph agent execution.
- Token usage accumulation, similarity score summaries, SSE Time-to-First-Token (`ttft_ms`), and `interactiverag-telemetry` emission.
- Regional observability Terraform resources (`infra/observability.tf`) and static pre-deploy policy checks.
"""

import io
import json
import logging
import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from fastapi import Depends
from fastapi.testclient import TestClient
from langchain_core.documents import Document
from langchain_core.messages import AIMessage, HumanMessage

from scripts.check_static_policies import evaluate_repo_static_policies
from src.agent import app as agent_app_module
from src.agent import graph as agent_graph_module
from src.agent.tools import get_retriever_tool
from src.core import context as context_module
from src.core.app_factory import create_app
from src.core.auth import get_current_user
from src.core.compliance.policy import reset_rate_limiters
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
from src.core.config import is_structured_logging_enabled
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
    hash_query,
    reset_request_context,
    set_active_span_id,
    set_request_uid,
)
from src.core.logger import setup_logging
from src.core.observability import (
    DEFAULT_TELEMETRY_LOG_NAME,
    GCPJsonFormatter,
    RequestContextLogFilter,
    get_telemetry_log_name,
    record_llm_usage,
    record_retrieval_metrics,
    register_span_listener,
    register_telemetry_listener,
    start_span,
    telemetry as telemetry_module,
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
from src.core.storage import chroma as chroma_module
from src.ingest import app as ingest_app_module
from src.ingest.service import ingest_documents
from src.ingest.splitter import split_pdfs
from src.query.service import search_collection
from tests.helpers import (
    DeterministicFakeEmbeddings,
    GroundedCitingChatModel,
    REPO_ROOT,
    cleanup_chroma_tmpdir,
    create_synthetic_repo_copy,
    make_test_pdf,
    reset_memory_saver,
)

logger = setup_logging(name="tests.observability")


class TestPhase1ContextAndPackageSeparation(unittest.TestCase):
    """Phase 1 unit tests: neutral `src.core.context`, `src.core.compliance.state`, and `src.core.observability.state`."""

    def test_legacy_compliance_context_file_removed_and_domain_functions_separated(self) -> None:
        """Verifies `src/core/compliance/context.py` is deleted and domain functions live in their `state.py` modules."""
        legacy_path = os.path.join(REPO_ROOT, "src", "core", "compliance", "context.py")
        neutral_path = os.path.join(REPO_ROOT, "src", "core", "context.py")
        comp_state_path = os.path.join(REPO_ROOT, "src", "core", "compliance", "state.py")
        obs_pkg_path = os.path.join(REPO_ROOT, "src", "core", "observability", "__init__.py")
        obs_state_path = os.path.join(REPO_ROOT, "src", "core", "observability", "state.py")

        self.assertFalse(
            os.path.exists(legacy_path),
            "src/core/compliance/context.py must be removed so src/core/context.py is the single source of truth.",
        )
        self.assertTrue(os.path.isfile(neutral_path))
        self.assertTrue(os.path.isfile(comp_state_path))
        self.assertTrue(os.path.isfile(obs_pkg_path))
        self.assertTrue(os.path.isfile(obs_state_path))

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
        with patch.dict(os.environ, {"AUTH_DISABLED": "true"}, clear=False):
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
        with patch.dict(
            os.environ,
            {"STRUCTURED_LOGGING": "false", "K_SERVICE": ""},
            clear=False,
        ):
            setup_logging(force=True)

    def test_is_structured_logging_enabled_env_and_cloud_run_defaults(self) -> None:
        """Verifies `is_structured_logging_enabled()` defaults to `is_cloud_run()` and honors `STRUCTURED_LOGGING`."""
        with patch.dict(
            os.environ,
            {"STRUCTURED_LOGGING": "", "K_SERVICE": ""},
            clear=False,
        ):
            self.assertFalse(is_structured_logging_enabled())

        with patch.dict(
            os.environ,
            {"STRUCTURED_LOGGING": "", "K_SERVICE": "interactive-rag-agent"},
            clear=False,
        ):
            self.assertTrue(is_structured_logging_enabled())

        with patch.dict(
            os.environ,
            {"STRUCTURED_LOGGING": "true", "K_SERVICE": ""},
            clear=False,
        ):
            self.assertTrue(is_structured_logging_enabled())

        with patch.dict(
            os.environ,
            {"STRUCTURED_LOGGING": "false", "K_SERVICE": "interactive-rag-agent"},
            clear=False,
        ):
            self.assertFalse(is_structured_logging_enabled())

    def test_gcp_json_formatter_emits_single_line_json_with_trace_and_source_location(self) -> None:
        """Verifies `GCPJsonFormatter` produces single-line GCP structured JSON enriched from `RequestContext`."""
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

            with patch.dict(
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

        test_logger.info("Executing vector search", extra={"query": secret_query})
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
        with patch.dict(
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

        with patch.dict(
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

                self.assertEqual(get_active_span_id(), turn_span_id)

                with start_span("agent.retriever_tool", stage="retrieval", k=5) as ret_span:
                    ret_span_id = ret_span["span_id"]
                    self.assertEqual(get_active_span_id(), ret_span_id)
                    self.assertEqual(ret_span["parent_span_id"], turn_span_id)
                    ret_span["attributes"]["retrieved_chunks"] = 3

            self.assertEqual(get_active_span_id(), "0000000000000001")

            spans = get_recorded_spans()
            self.assertEqual(len(spans), 3)
            self.assertEqual(len(listener_spans), 3)

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

            log_payload = json.loads(stream.getvalue().strip())
            self.assertEqual(log_payload["logging.googleapis.com/spanId"], llm_span_id)
        finally:
            unregister()
            reset_request_context(token)

    def test_span_error_status_and_raw_query_attribute_scrubbing(self) -> None:
        """Verifies exceptions inside `start_span` mark `status='error'` and scrub raw `query` attributes."""
        ctx = RequestContext(
            request_id="req-phase3-err",
            trace_id="205445aa7843bc8bf206b12000100000",
            root_span_id="0000000000000009",
            service_name="ingest-api",
        )
        token = bind_request_context(ctx)
        secret_query = "Confidential acquisition target name?"
        try:
            with self.assertRaises(RuntimeError):
                with start_span("query.search_collection", stage="retrieval", query=secret_query):
                    raise RuntimeError("Simulated vector index error")

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
                docs = split_pdfs([pdf_path], chunk_size=300, chunk_overlap=30)
                self.assertGreaterEqual(len(docs), 1)

                ingest_documents(
                    uid="alice_e2e",
                    documents=docs,
                    collection_name="obs_reports",
                    embedding_model="gemini-embedding-001",
                )

                results = search_collection(
                    uid="alice_e2e",
                    query="Annual revenue growth",
                    collection_name="obs_reports",
                    k=2,
                    embedding_model="gemini-embedding-001",
                )
                self.assertGreaterEqual(len(results), 1)

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
                "ingest.split_pdfs",
                "ingest.ingest_documents",
                "query.search_collection",
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
            cleanup_chroma_tmpdir(chroma_root)
            shutil.rmtree(temp_dir, ignore_errors=True)


class TestPhase4AIMonitoringAndTelemetryStream(unittest.TestCase):
    """Phase 4 unit tests: token usage extraction, similarity score tracking, SSE TTFT, and `interactiverag-telemetry`."""

    def test_record_llm_usage_and_retrieval_metrics_accumulation(self) -> None:
        """Verifies `record_llm_usage()` and `record_retrieval_metrics()` accumulate across multi-step turns."""
        ctx = RequestContext(
            request_id="req-phase4-unit",
            trace_id="405445aa7843bc8bf206b12000100000",
            service_name="agent-api",
        )
        token = bind_request_context(ctx)
        try:
            msg_1 = AIMessage(
                content="",
                usage_metadata={"input_tokens": 180, "output_tokens": 35, "total_tokens": 215},
            )
            u1 = record_llm_usage(msg_1)
            self.assertEqual(u1, {"input_tokens": 180, "output_tokens": 35, "total_tokens": 215})

            msg_2 = AIMessage(
                content="Grounded synthesis [report.pdf, p.1]",
                response_metadata={
                    "usage_metadata": {
                        "prompt_token_count": 420,
                        "candidates_token_count": 95,
                        "total_token_count": 515,
                    }
                },
            )
            u2 = record_llm_usage(msg_2)
            self.assertEqual(u2, {"input_tokens": 420, "output_tokens": 95, "total_tokens": 515})

            summary = get_token_usage_summary()
            self.assertEqual(summary["input_tokens"], 600)
            self.assertEqual(summary["output_tokens"], 130)
            self.assertEqual(summary["total_tokens"], 730)
            self.assertEqual(summary["llm_calls"], 2)

            d1 = Document(page_content="Chunk A", metadata={"source": "report.pdf", "page": 0})
            d2 = Document(page_content="Chunk B", metadata={"source": "report.pdf", "page": 1})
            m1 = record_retrieval_metrics([(d1, 0.90), (d2, 0.70)], is_tool_call=True)
            self.assertEqual(m1["tool_calls_count"], 1)
            self.assertAlmostEqual(m1["top_similarity_score"], 0.90, places=4)
            self.assertAlmostEqual(m1["mean_similarity_score"], 0.80, places=4)

            d3 = Document(page_content="Chunk C", metadata={"source": "report.pdf", "page": 2})
            m2 = record_retrieval_metrics([(d3, 0.80)], is_tool_call=True)
            self.assertEqual(m2["tool_calls_count"], 2)
            self.assertAlmostEqual(m2["top_similarity_score"], 0.90, places=4)
            self.assertAlmostEqual(m2["mean_similarity_score"], 0.80, places=4)
        finally:
            reset_request_context(token)

    def test_end_to_end_ingest_query_agent_and_sse_telemetry_emission(self) -> None:
        """Verifies `/ingest`, `/query`, `/agent`, and `/agent/stream` emit complete `interactiverag-telemetry` records."""
        self.assertEqual(get_telemetry_log_name(), DEFAULT_TELEMETRY_LOG_NAME)

        chroma_tmp = tempfile.mkdtemp(prefix="obs_p4_chroma_")
        uploads_tmp = tempfile.mkdtemp(prefix="obs_p4_uploads_")
        reset_memory_saver()
        reset_rate_limiters()

        ingest_client = TestClient(ingest_app_module.app)
        agent_client = TestClient(agent_app_module.app)
        telemetry_events = []
        unregister = register_telemetry_listener(telemetry_events.append)

        env = {
            "AUTH_DISABLED": "true",
            "K_SERVICE": "",
            "DEV_USER_ID": "obs_user",
            "GOOGLE_CLOUD_PROJECT": "interactive-rag",
            "GOOGLE_CLOUD_LOCATION": "us-central1",
            "ALLOWED_LLM_MODELS": "gemini-2.5-flash,gemini-2.5-pro,gemini-2.5-flash-lite",
            "ALLOWED_EMBEDDING_MODELS": "gemini-embedding-001,text-embedding-005",
            "LOCAL_CHROMA_ROOT": chroma_tmp,
            "UPLOAD_TMP_ROOT": uploads_tmp,
            "GCS_BUCKET_NAME": "",
            "ARCHIVE_UPLOADS_TO_GCS": "false",
            "MODEL_ARMOR_TEMPLATE_ID": "",
        }

        try:
            with patch.dict(os.environ, env, clear=False), patch.object(
                chroma_module,
                "get_embeddings",
                side_effect=lambda *args, **kwargs: DeterministicFakeEmbeddings(),
            ), patch.object(
                agent_graph_module,
                "ChatGoogleGenerativeAI",
                side_effect=lambda *args, **kwargs: GroundedCitingChatModel(*args, **kwargs),
            ):
                res_ingest = ingest_client.post(
                    "/ingest",
                    headers={
                        "X-Request-Id": "req-obs-ingest-001",
                        "X-Cloud-Trace-Context": "105445aa7843bc8bf206b12000100000/1;o=1",
                    },
                    data={"collection_name": "financial_reports"},
                    files=[
                        (
                            "files",
                            ("report.pdf", make_test_pdf("Q4 revenue reached 120 million USD."), "application/pdf"),
                        )
                    ],
                )
                self.assertEqual(res_ingest.status_code, 200)
                ingest_tel = [e for e in telemetry_events if e["event_type"] == "document.ingest"]
                self.assertEqual(len(ingest_tel), 1)
                self.assertEqual(ingest_tel[0]["request_id"], "req-obs-ingest-001")
                self.assertEqual(ingest_tel[0]["trace_id"], "105445aa7843bc8bf206b12000100000")
                self.assertEqual(ingest_tel[0]["collection_name"], "financial_reports")
                self.assertGreaterEqual(ingest_tel[0]["chunks_added"], 1)
                self.assertIn("chunking_ms", ingest_tel[0]["stage_latency_ms"])
                self.assertIn("embedding_ms", ingest_tel[0]["stage_latency_ms"])

                telemetry_events.clear()
                res_query = agent_client.post(
                    "/query",
                    headers={
                        "X-Request-Id": "req-obs-query-001",
                        "X-Cloud-Trace-Context": "205445aa7843bc8bf206b12000100000/2;o=1",
                    },
                    json={"query": "What was Q4 revenue?", "collection_name": "financial_reports", "k": 2},
                )
                self.assertEqual(res_query.status_code, 200)
                query_tel = [e for e in telemetry_events if e["event_type"] == "vectorstore.query"]
                self.assertEqual(len(query_tel), 1)
                self.assertEqual(query_tel[0]["request_id"], "req-obs-query-001")
                self.assertEqual(query_tel[0]["retrieved_chunks"], 1)
                self.assertIsNotNone(query_tel[0]["top_similarity_score"])
                self.assertIsNotNone(query_tel[0]["mean_similarity_score"])
                self.assertIn("retrieval_ms", query_tel[0]["stage_latency_ms"])
                self.assertNotIn("What was Q4 revenue?", str(query_tel[0]))
                self.assertIn("query_sha256", query_tel[0])

                telemetry_events.clear()
                res_agent = agent_client.post(
                    "/agent",
                    headers={
                        "X-Request-Id": "req-obs-agent-001",
                        "X-Cloud-Trace-Context": "305445aa7843bc8bf206b12000100000/3;o=1",
                    },
                    json={
                        "query": "Summarize Q4 revenue.",
                        "collection_name": "financial_reports",
                        "model_name": "gemini-2.5-flash",
                        "thread_id": "t-sync",
                        "stream": False,
                    },
                )
                self.assertEqual(res_agent.status_code, 200)
                agent_tel = [e for e in telemetry_events if e["event_type"] == "agent.query"]
                self.assertEqual(len(agent_tel), 1)
                rec = agent_tel[0]
                self.assertEqual(rec["request_id"], "req-obs-agent-001")
                self.assertEqual(rec["trace_id"], "305445aa7843bc8bf206b12000100000")
                self.assertEqual(rec["model_name"], "gemini-2.5-flash")
                self.assertEqual(rec["input_tokens"], 470)
                self.assertEqual(rec["output_tokens"], 105)
                self.assertEqual(rec["total_tokens"], 575)
                self.assertEqual(rec["llm_calls"], 2)
                self.assertEqual(rec["tool_calls_count"], 1)
                self.assertEqual(rec["retrieved_chunks"], 1)
                self.assertIsNotNone(rec["top_similarity_score"])
                self.assertIsNotNone(rec["mean_similarity_score"])
                self.assertTrue(rec["grounded"])
                self.assertGreaterEqual(rec["citation_count"], 1)
                for stage_key in ("input_guard_ms", "llm_ms", "retrieval_ms", "output_guard_ms"):
                    self.assertIn(stage_key, rec["stage_latency_ms"])

                telemetry_events.clear()
                res_stream = agent_client.post(
                    "/agent/stream",
                    headers={
                        "X-Request-Id": "req-obs-stream-001",
                        "X-Cloud-Trace-Context": "405445aa7843bc8bf206b12000100000/4;o=1",
                    },
                    json={
                        "query": "Stream Q4 revenue summary.",
                        "collection_name": "financial_reports",
                        "model_name": "gemini-2.5-flash",
                        "thread_id": "t-stream",
                    },
                )
                self.assertEqual(res_stream.status_code, 200)
                stream_tel = [e for e in telemetry_events if e["event_type"] == "agent.query"]
                self.assertEqual(len(stream_tel), 1)
                srec = stream_tel[0]
                self.assertTrue(srec["stream"])
                self.assertIn("ttft_ms", srec)
                self.assertIsNotNone(srec["ttft_ms"])
                self.assertGreaterEqual(srec["ttft_ms"], 0.0)
                self.assertLessEqual(srec["ttft_ms"], srec["total_latency_ms"])

                mock_cloud_logger = MagicMock()
                telemetry_module._cloud_loggers.clear()
                with patch.dict(
                    os.environ,
                    {"TELEMETRY_CLOUD_LOGGING_ENABLED": "true"},
                    clear=False,
                ), patch.object(
                    telemetry_module,
                    "_get_cloud_logger",
                    return_value=mock_cloud_logger,
                ):
                    telemetry_module.emit_telemetry_event(
                        "agent.query",
                        uid="obs_user",
                        model_name="gemini-2.5-flash",
                        grounded=True,
                    )
                    mock_cloud_logger.log_struct.assert_called_once()
        finally:
            unregister()
            cleanup_chroma_tmpdir(chroma_tmp)
            shutil.rmtree(uploads_tmp, ignore_errors=True)
            reset_memory_saver()
            reset_rate_limiters()


class TestPhase5ObservabilityInfrastructureAndStaticPolicies(unittest.TestCase):
    """Phase 5: Regional Observability Infrastructure in Terraform & Static Policy Checks."""

    def test_terraform_observability_infrastructure_definitions(self) -> None:
        """Verifies `infra/` declares regional observability log buckets, sinks, log-based metrics, and dashboards."""
        infra_dir = os.path.join(REPO_ROOT, "infra")

        with open(os.path.join(infra_dir, "apis.tf"), encoding="utf-8") as fh:
            apis_tf = fh.read()
        self.assertIn('"monitoring.googleapis.com"', apis_tf)
        self.assertIn('"cloudtrace.googleapis.com"', apis_tf)

        with open(os.path.join(infra_dir, "iam.tf"), encoding="utf-8") as fh:
            iam_tf = fh.read()
        self.assertIn('resource "google_project_iam_member" "backend_metric_writer"', iam_tf)
        self.assertIn('role    = "roles/monitoring.metricWriter"', iam_tf)
        self.assertIn('resource "google_project_iam_member" "backend_trace_agent"', iam_tf)
        self.assertIn('role    = "roles/cloudtrace.agent"', iam_tf)

        with open(os.path.join(infra_dir, "cloud_run.tf"), encoding="utf-8") as fh:
            cloud_run_tf = fh.read()
        self.assertEqual(cloud_run_tf.count('name  = "TELEMETRY_LOG_NAME"'), 2)
        self.assertEqual(cloud_run_tf.count('value = "interactiverag-telemetry"'), 2)
        self.assertEqual(cloud_run_tf.count('name  = "STRUCTURED_LOGGING"'), 2)
        self.assertEqual(cloud_run_tf.count('name  = "CLOUD_TRACE_ENABLED"'), 2)

        with open(os.path.join(infra_dir, "observability.tf"), encoding="utf-8") as fh:
            obs_tf = fh.read()
        self.assertIn('resource "google_logging_project_bucket_config" "observability_telemetry"', obs_tf)
        self.assertIn("retention_days   = var.observability_log_retention_days", obs_tf)
        self.assertIn('resource "google_logging_project_sink" "observability_sink"', obs_tf)
        for metric_name in (
            "agent_turn_latency_ms",
            "agent_ttft_ms",
            "agent_llm_stage_latency_ms",
            "agent_retrieval_stage_latency_ms",
            "agent_input_tokens",
            "agent_output_tokens",
            "agent_total_tokens",
            "retrieval_mean_similarity_score",
            "agent_ungrounded_answers",
            "ingest_chunks_added",
        ):
            self.assertIn(f'resource "google_logging_metric" "{metric_name}"', obs_tf)
        self.assertIn('resource "google_monitoring_dashboard" "observability_dashboard"', obs_tf)

        with open(os.path.join(infra_dir, "variables.tf"), encoding="utf-8") as fh:
            vars_tf = fh.read()
        self.assertIn('variable "observability_log_retention_days"', vars_tf)

        with open(os.path.join(infra_dir, "outputs.tf"), encoding="utf-8") as fh:
            outputs_tf = fh.read()
        for out_name in (
            "observability_log_bucket_id",
            "observability_log_sink_name",
            "application_telemetry_log_name",
            "observability_dashboard_id",
        ):
            self.assertIn(f'output "{out_name}"', outputs_tf)

    def test_static_policy_verifier_enforces_observability_and_regional_rules(self) -> None:
        """Verifies `evaluate_repo_static_policies` catches non-regional observability bucket or disabled structured logging."""
        ok, violations = evaluate_repo_static_policies(REPO_ROOT)
        self.assertTrue(ok, f"Expected static policy check to pass, got: {violations}")
        self.assertEqual(violations, [])

        syn_root = create_synthetic_repo_copy(REPO_ROOT, prefix="obs_static_policy_")
        try:
            obs_path = os.path.join(syn_root, "infra", "observability.tf")
            with open(obs_path, encoding="utf-8") as fh:
                orig_obs = fh.read()
            with open(obs_path, "w", encoding="utf-8") as fh:
                fh.write(orig_obs.replace("location         = var.region", 'location         = "global"', 1))
            ok_obs, v_obs = evaluate_repo_static_policies(syn_root)
            self.assertFalse(ok_obs)
            self.assertTrue(any("observability_telemetry" in m for m in v_obs), v_obs)
            with open(obs_path, "w", encoding="utf-8") as fh:
                fh.write(orig_obs)

            cr_path = os.path.join(syn_root, "infra", "cloud_run.tf")
            with open(cr_path, encoding="utf-8") as fh:
                orig_cr = fh.read()
            with open(cr_path, "w", encoding="utf-8") as fh:
                fh.write(
                    orig_cr.replace(
                        'name  = "STRUCTURED_LOGGING"\n        value = "true"',
                        'name  = "STRUCTURED_LOGGING"\n        value = "false"',
                        1,
                    )
                )
            ok_cr, v_cr = evaluate_repo_static_policies(syn_root)
            self.assertFalse(ok_cr)
            self.assertTrue(any("STRUCTURED_LOGGING" in m for m in v_cr), v_cr)
        finally:
            shutil.rmtree(syn_root, ignore_errors=True)


if __name__ == "__main__":
    logger.info("Running Layer 5 Observability & AI Monitoring unit test suite...")
    unittest.main(verbosity=2)
