import os
import unittest
from unittest.mock import patch

from dotenv import find_dotenv, load_dotenv
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from firebase_admin import auth as firebase_auth

from src.core.auth import (
    AuthenticatedUser,
    get_current_user,
    get_current_user_info,
    verify_bearer_token,
)
from src.core.logger import setup_logging

load_dotenv(find_dotenv())
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

logger = setup_logging(name="test_auth")


def _build_test_app() -> FastAPI:
    app = FastAPI()

    @app.get("/whoami")
    def whoami(uid: str = Depends(get_current_user)):
        return {"uid": uid}

    @app.get("/me")
    def me(user: AuthenticatedUser = Depends(get_current_user_info)):
        return user.model_dump()

    return app


class TestBackendAuthModule(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(_build_test_app())
        self.base_env = {
            "GOOGLE_CLOUD_PROJECT": os.getenv("GOOGLE_CLOUD_PROJECT", "interactive-rag"),
            "AUTH_DISABLED": "false",
            "K_SERVICE": "",
        }

    def test_01_missing_authorization_header_returns_401(self):
        with patch.dict(os.environ, self.base_env, clear=False):
            resp = self.client.get("/whoami")
            self.assertEqual(resp.status_code, 401)
            self.assertEqual(resp.headers.get("WWW-Authenticate"), "Bearer")

    def test_02_malformed_authorization_headers_return_401(self):
        with patch.dict(os.environ, self.base_env, clear=False):
            for header_val in ["Basic dXNlcjpwYXNz", "Bearer", "Bearer    ", "Token abc123"]:
                resp = self.client.get("/whoami", headers={"Authorization": header_val})
                self.assertEqual(resp.status_code, 401, f"Expected 401 for header {header_val!r}")

    def test_03_forged_jwt_rejected_by_firebase_sdk(self):
        forged_jwt = (
            "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9."
            "eyJzdWIiOiJhdHRhY2tlciIsImF1ZCI6InRlc3QtZ2NwLXByb2plY3QifQ."
            "invalidsignature"
        )
        with patch.dict(os.environ, self.base_env, clear=False):
            resp = self.client.get("/whoami", headers={"Authorization": f"Bearer {forged_jwt}"})
            self.assertEqual(resp.status_code, 401)

    def test_04_valid_verified_token_extracts_uid_and_profile(self):
        mock_claims = {
            "uid": "user_alice_01",
            "email": "alice@example.com",
            "name": "Alice",
        }
        with patch.dict(os.environ, self.base_env, clear=False), patch(
            "src.core.auth.firebase_auth.verify_id_token", return_value=mock_claims
        ) as mock_verify:
            resp = self.client.get("/me", headers={"Authorization": "Bearer valid.jwt.token"})
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(
                resp.json(),
                {"uid": "user_alice_01", "email": "alice@example.com", "name": "Alice"},
            )
            mock_verify.assert_called_once()

    def test_05_expired_token_returns_401(self):
        with patch.dict(os.environ, self.base_env, clear=False), patch(
            "src.core.auth.firebase_auth.verify_id_token",
            side_effect=firebase_auth.ExpiredIdTokenError("Token expired", cause=None),
        ):
            resp = self.client.get("/whoami", headers={"Authorization": "Bearer expired.jwt.token"})
            self.assertEqual(resp.status_code, 401)
            self.assertIn("expired", resp.json()["detail"].lower())

    def test_06_path_traversal_uids_are_blocked(self):
        unsafe_uids = [
            "../other_user",
            "../../etc/passwd",
            "alice/bob",
            "alice\\bob",
            "user with spaces",
            "",
            "a" * 129,
        ]
        for bad_uid in unsafe_uids:
            with patch.dict(os.environ, self.base_env, clear=False), patch(
                "src.core.auth.firebase_auth.verify_id_token", return_value={"uid": bad_uid}
            ):
                resp = self.client.get("/whoami", headers={"Authorization": "Bearer token"})
                self.assertEqual(resp.status_code, 401, f"Unsafe uid {bad_uid!r} should fail")

    def test_07_local_auth_bypass_works_only_outside_cloud_run(self):
        local_bypass_env = {
            **self.base_env,
            "AUTH_DISABLED": "true",
            "DEV_USER_ID": "local-tester",
            "K_SERVICE": "",
        }
        with patch.dict(os.environ, local_bypass_env, clear=False):
            # Uses default DEV_USER_ID
            resp_default = self.client.get("/whoami")
            self.assertEqual(resp_default.status_code, 200)
            self.assertEqual(resp_default.json()["uid"], "local-tester")

            # Supports per-request tenant simulation via X-Dev-User-Id (still validated against path traversal)
            resp_bob = self.client.get("/whoami", headers={"X-Dev-User-Id": "tenant_bob"})
            self.assertEqual(resp_bob.status_code, 200)
            self.assertEqual(resp_bob.json()["uid"], "tenant_bob")

            resp_traversal = self.client.get("/whoami", headers={"X-Dev-User-Id": "../tenant_alice"})
            self.assertEqual(resp_traversal.status_code, 401)

    def test_08_cloud_run_blocks_auth_bypass_even_if_misconfigured(self):
        cloud_run_env = {
            **self.base_env,
            "AUTH_DISABLED": "true",
            "K_SERVICE": "interactive-rag-ingestion-prod",
        }
        with patch.dict(os.environ, cloud_run_env, clear=False):
            resp = self.client.get("/whoami", headers={"X-Dev-User-Id": "attacker"})
            self.assertEqual(resp.status_code, 401)


if __name__ == "__main__":
    live_token = os.getenv("ID_TOKEN", "").strip()
    if live_token:
        logger.info("ID_TOKEN detected in environment — verifying live Firebase/Identity Platform token...")
        claims = verify_bearer_token(live_token)
        logger.info(
            f"Live token verified successfully! uid={claims.get('uid')!r}, email={claims.get('email')!r}"
        )

    logger.info("Running Phase 2 authentication test suite...")
    unittest.main(verbosity=2)
