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


if __name__ == "__main__":
    unittest.main()
