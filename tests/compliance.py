import os
import unittest
from unittest.mock import MagicMock, patch

from langchain_core.tools import tool

from scripts.check_region_policy import evaluate_effective_location_policy
from src.agent import graph as agent_graph_module
from src.core import chroma as chroma_module
from src.core import config as config_module
from src.core import security as security_module
from src.core.config import get_google_cloud_location, get_model_armor_location
from src.core.security import validate_gcp_region
from tests.ai_security import GroundedCitingChatModel


@tool
def _noop_retriever_tool(query: str) -> str:
    """No-op retriever tool for graph construction unit tests."""
    return query


class TestPhase1DataResidency(unittest.TestCase):
    """Phase 1 unit tests: single GOOGLE_CLOUD_LOCATION unification, regional pinning, and Org Policy gate."""

    def test_default_location_constant_removed_and_unified_on_google_cloud_location(self):
        self.assertFalse(
            hasattr(config_module, "DEFAULT_LOCATION"),
            "Legacy DEFAULT_LOCATION constant must not exist on src.core.config",
        )
        self.assertTrue(hasattr(config_module, "GOOGLE_CLOUD_LOCATION"))

        with patch.dict(os.environ, {"GOOGLE_CLOUD_LOCATION": ""}, clear=False):
            self.assertEqual(get_google_cloud_location(), "us-central1")

        with patch.dict(os.environ, {"GOOGLE_CLOUD_LOCATION": "us-west2"}, clear=False):
            self.assertEqual(get_google_cloud_location(), "us-west2")

    def test_model_armor_location_defaults_to_google_cloud_location(self):
        with patch.dict(
            os.environ,
            {"GOOGLE_CLOUD_LOCATION": "us-west3", "MODEL_ARMOR_LOCATION": ""},
            clear=False,
        ):
            self.assertEqual(get_model_armor_location(), "us-west3")

        with patch.dict(
            os.environ,
            {"GOOGLE_CLOUD_LOCATION": "us-west3", "MODEL_ARMOR_LOCATION": "us-central1"},
            clear=False,
        ):
            self.assertEqual(get_model_armor_location(), "us-central1")

    def test_validate_gcp_region_accepts_concrete_single_regions(self):
        for valid_region in (
            "us-central1",
            "us-west1",
            "us-west2",
            "us-west3",
            "europe-west1",
            "asia-northeast1",
        ):
            self.assertEqual(validate_gcp_region(valid_region), valid_region)

        self.assertEqual(validate_gcp_region("  US-CENTRAL1 "), "us-central1")

    def test_validate_gcp_region_rejects_global_and_multi_regions(self):
        for invalid_location in (
            "global",
            "GLOBAL",
            "us",
            "eu",
            "asia",
            "nam4",
            "eur4",
            "us-central",
            "us_central1",
            "",
        ):
            with self.subTest(invalid_location=invalid_location):
                with self.assertRaises(ValueError):
                    validate_gcp_region(invalid_location)

    def test_model_armor_target_resolution_rejects_global_location(self):
        with patch.dict(
            os.environ,
            {
                "GOOGLE_CLOUD_PROJECT": "interactive-rag",
                "MODEL_ARMOR_TEMPLATE_ID": "projects/interactive-rag/locations/global/templates/tmpl",
            },
            clear=False,
        ):
            with self.assertRaises(ValueError):
                security_module._resolve_model_armor_target()

        with patch.dict(
            os.environ,
            {
                "GOOGLE_CLOUD_PROJECT": "interactive-rag",
                "MODEL_ARMOR_TEMPLATE_ID": "short-template-id",
                "MODEL_ARMOR_LOCATION": "global",
            },
            clear=False,
        ):
            with self.assertRaises(ValueError):
                security_module._resolve_model_armor_target()

    def test_get_embeddings_pins_google_cloud_location_and_rejects_global(self):
        mock_embeddings_cls = MagicMock()
        with patch.object(chroma_module, "GoogleGenerativeAIEmbeddings", mock_embeddings_cls):
            with patch.dict(
                os.environ,
                {
                    "GOOGLE_CLOUD_PROJECT": "interactive-rag",
                    "GOOGLE_CLOUD_LOCATION": "us-west2",
                },
                clear=False,
            ):
                chroma_module.get_embeddings("gemini-embedding-001")

        mock_embeddings_cls.assert_called_once_with(
            model="gemini-embedding-001",
            project="interactive-rag",
            location="us-west2",
            vertexai=True,
        )

        with patch.object(chroma_module, "GoogleGenerativeAIEmbeddings", MagicMock()):
            with patch.dict(os.environ, {"GOOGLE_CLOUD_LOCATION": "global"}, clear=False):
                with self.assertRaises(ValueError):
                    chroma_module.get_embeddings("gemini-embedding-001")

    def test_build_agent_pins_google_cloud_location_and_rejects_global(self):
        captured = {}

        def fake_chat_factory(*args, **kwargs):
            model = GroundedCitingChatModel(*args, **kwargs)
            captured["kwargs"] = kwargs
            return model

        with patch.object(agent_graph_module, "ChatGoogleGenerativeAI", side_effect=fake_chat_factory):
            with patch.dict(
                os.environ,
                {
                    "GOOGLE_CLOUD_PROJECT": "interactive-rag",
                    "GOOGLE_CLOUD_LOCATION": "us-west1",
                },
                clear=False,
            ):
                agent_graph_module.build_agent(
                    retriever_tool=_noop_retriever_tool,
                    model_name="gemini-2.5-flash",
                )

        self.assertEqual(captured["kwargs"]["location"], "us-west1")
        self.assertEqual(captured["kwargs"]["project"], "interactive-rag")
        self.assertEqual(captured["kwargs"]["model"], "gemini-2.5-flash")

        with patch.object(agent_graph_module, "ChatGoogleGenerativeAI", side_effect=fake_chat_factory):
            with patch.dict(os.environ, {"GOOGLE_CLOUD_LOCATION": "global"}, clear=False):
                with self.assertRaises(ValueError):
                    agent_graph_module.build_agent(
                        retriever_tool=_noop_retriever_tool,
                        model_name="gemini-2.5-flash",
                    )

    def test_org_policy_evaluator_enforces_effective_resource_locations(self):
        sample_policy = {
            "constraint": "constraints/gcp.resourceLocations",
            "listPolicy": {
                "allowedValues": [
                    "in:us-central1-locations",
                    "in:us-west1-locations",
                    "in:us-west2-locations",
                    "in:us-west3-locations",
                ]
            },
        }

        for allowed in ("us-central1", "us-west1", "us-west2", "us-west3"):
            ok, msg = evaluate_effective_location_policy(
                sample_policy, allowed, project_id="interactive-rag"
            )
            self.assertTrue(ok, msg)
            self.assertIn(allowed, msg)

        ok_unapproved, msg_unapproved = evaluate_effective_location_policy(
            sample_policy, "us-east1", project_id="interactive-rag"
        )
        self.assertFalse(ok_unapproved)
        self.assertIn("us-central1, us-west1, us-west2, us-west3", msg_unapproved)

        for non_regional in ("global", "us", "eu"):
            ok_non_reg, msg_non_reg = evaluate_effective_location_policy(
                sample_policy, non_regional, project_id="interactive-rag"
            )
            self.assertFalse(ok_non_reg)
            self.assertIn("concrete single GCP region", msg_non_reg)

        unrestricted_policy = {
            "constraint": "constraints/gcp.resourceLocations",
            "listPolicy": {"allValues": "ALLOW"},
        }
        ok_unrestricted, msg_unrestricted = evaluate_effective_location_policy(
            unrestricted_policy, "us-central1", project_id="interactive-rag"
        )
        self.assertFalse(ok_unrestricted)
        self.assertIn("not restricting locations", msg_unrestricted)

        denied_policy = {
            "constraint": "constraints/gcp.resourceLocations",
            "listPolicy": {
                "allowedValues": ["in:us-locations"],
                "deniedValues": ["in:us-west2-locations"],
            },
        }
        ok_denied, msg_denied = evaluate_effective_location_policy(
            denied_policy, "us-west2", project_id="interactive-rag"
        )
        self.assertFalse(ok_denied)
        self.assertIn("explicitly denied", msg_denied)

    def test_single_source_of_truth_model_allowlists_and_required_collection_name(self):
        for removed_const in (
            "DEFAULT_COLLECTION",
            "DEFAULT_LLM_MODEL",
            "DEFAULT_EMBEDDING_MODEL",
            "DEFAULT_ALLOWED_LLM_MODELS",
            "DEFAULT_ALLOWED_EMBEDDING_MODELS",
        ):
            self.assertFalse(
                hasattr(config_module, removed_const),
                f"Redundant constant {removed_const} must not exist on src.core.config",
            )

        # Fail closed when env allowlists are missing or blank
        with patch.dict(os.environ, {"ALLOWED_LLM_MODELS": " , "}, clear=False):
            with self.assertRaises(ValueError):
                config_module.get_allowed_llm_models()
            with self.assertRaises(ValueError):
                security_module.validate_llm_model(None)

        with patch.dict(os.environ, {"ALLOWED_EMBEDDING_MODELS": ""}, clear=False):
            with self.assertRaises(ValueError):
                config_module.get_allowed_embedding_models()
            with self.assertRaises(ValueError):
                security_module.validate_embedding_model(None)

        # Default selection dynamically tracks the first configured model in each allowlist
        with patch.dict(
            os.environ,
            {
                "ALLOWED_LLM_MODELS": "gemini-2.5-pro,gemini-2.5-flash",
                "ALLOWED_EMBEDDING_MODELS": "text-embedding-005,gemini-embedding-001",
            },
            clear=False,
        ):
            self.assertEqual(security_module.validate_llm_model(None), "gemini-2.5-pro")
            self.assertEqual(security_module.validate_llm_model(""), "gemini-2.5-pro")
            self.assertEqual(security_module.validate_embedding_model(None), "text-embedding-005")
            self.assertEqual(security_module.validate_embedding_model(""), "text-embedding-005")

        # collection_name is mandatory (no fallback default collection)
        for missing_col in (None, "", "   "):
            with self.assertRaises(ValueError):
                security_module.validate_collection_name(missing_col)  # type: ignore[arg-type]

        # Verify .env and Terraform variables stay aligned on the regional model catalogs
        expected_llms = "gemini-2.5-flash,gemini-2.5-pro,gemini-2.5-flash-lite"
        expected_embeddings = "gemini-embedding-001,text-embedding-005"
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(repo_root, "infra", "variables.tf"), encoding="utf-8") as fh:
            variables_tf = fh.read()
        self.assertIn(f'default     = "{expected_llms}"', variables_tf)
        self.assertIn(f'default     = "{expected_embeddings}"', variables_tf)

        with open(os.path.join(repo_root, ".env"), encoding="utf-8") as fh:
            dotenv_content = fh.read()
        self.assertIn(f'ALLOWED_LLM_MODELS="{expected_llms}"', dotenv_content)
        self.assertIn(f'ALLOWED_EMBEDDING_MODELS="{expected_embeddings}"', dotenv_content)



if __name__ == "__main__":
    unittest.main()
