import os
import re
import threading
from typing import Any, Dict, Optional

import firebase_admin
from fastapi import Depends, Header, HTTPException, status
from firebase_admin import auth as firebase_auth
from pydantic import BaseModel

from utils.logging import setup_logging

logger = setup_logging(name="auth")

# Strict allowlist pattern preventing path traversal when constructing `/tmp/chroma_db/{uid}` or GCS prefixes
UID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")

_firebase_init_lock = threading.Lock()

WWW_AUTHENTICATE_HEADER = {"WWW-Authenticate": "Bearer"}


class AuthenticatedUser(BaseModel):
    """Verified user identity extracted from a Google Identity Platform / Firebase ID token."""

    uid: str
    email: Optional[str] = None
    name: Optional[str] = None


def validate_uid(uid: Optional[str]) -> str:
    """Validates that a user ID is safe for filesystem and GCS object prefix construction."""
    if not uid or not isinstance(uid, str) or not UID_PATTERN.match(uid):
        logger.warning(f"Rejected unsafe or malformed uid: {uid!r}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid user identifier format.",
            headers=WWW_AUTHENTICATE_HEADER,
        )
    return uid


def is_cloud_run_environment() -> bool:
    """Returns True when running inside a Google Cloud Run container (K_SERVICE is injected by Cloud Run)."""
    return bool(os.getenv("K_SERVICE", "").strip())


def is_local_auth_bypass_enabled() -> bool:
    """Checks whether local-only authentication bypass is active.

    Never allows bypass when K_SERVICE is set (Cloud Run), even if AUTH_DISABLED=true is misconfigured.
    """
    auth_disabled = os.getenv("AUTH_DISABLED", "").strip().lower() in {"1", "true", "yes"}
    if not auth_disabled:
        return False

    if is_cloud_run_environment():
        logger.error(
            "AUTH_DISABLED=true is set inside Cloud Run (K_SERVICE detected). "
            "Ignoring bypass and enforcing strict JWT verification."
        )
        return False

    return True


def get_allowed_origins() -> list[str]:
    """Parses ALLOWED_ORIGINS from the environment (comma-separated).

    Defaults to standard localhost dev ports when running locally, and fails closed ([])
    in Cloud Run if ALLOWED_ORIGINS is omitted.
    """
    raw = os.getenv("ALLOWED_ORIGINS", "").strip()
    if raw:
        origins: list[str] = []
        for item in raw.split(","):
            cleaned = item.strip()
            if not cleaned:
                continue
            origins.append("*" if cleaned == "*" else cleaned.rstrip("/"))
        return origins

    if is_cloud_run_environment():
        return []

    return [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]


def ensure_firebase_initialized() -> firebase_admin.App:
    """Initializes the Firebase Admin SDK once per process using Application Default Credentials."""
    if firebase_admin._apps:
        return firebase_admin.get_app()

    with _firebase_init_lock:
        if firebase_admin._apps:
            return firebase_admin.get_app()

        project_id = (
            os.getenv("GOOGLE_CLOUD_PROJECT", "").strip()
            or os.getenv("FIREBASE_PROJECT_ID", "").strip()
        )
        if not project_id:
            logger.error(
                "GOOGLE_CLOUD_PROJECT (or FIREBASE_PROJECT_ID) must be configured to verify ID tokens."
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Server authentication configuration error: missing project ID.",
            )

        app = firebase_admin.initialize_app(options={"projectId": project_id})
        logger.info(f"Initialized Firebase Admin SDK for project '{project_id}'.")
        return app


def _extract_bearer_token(authorization: Optional[str]) -> str:
    """Extracts the raw JWT from an 'Authorization: Bearer <token>' header."""
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header. Expected 'Bearer <ID_TOKEN>'.",
            headers=WWW_AUTHENTICATE_HEADER,
        )

    parts = authorization.strip().split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Malformed Authorization header. Expected 'Bearer <ID_TOKEN>'.",
            headers=WWW_AUTHENTICATE_HEADER,
        )

    return parts[1].strip()


def verify_bearer_token(token: str) -> Dict[str, Any]:
    """Verifies a Firebase / Google Identity Platform ID token and returns decoded claims."""
    app = ensure_firebase_initialized()
    try:
        decoded_claims = firebase_auth.verify_id_token(
            token,
            app=app,
            check_revoked=False,
        )
        return decoded_claims
    except firebase_auth.ExpiredIdTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication token has expired.",
            headers=WWW_AUTHENTICATE_HEADER,
        )
    except firebase_auth.RevokedIdTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication token has been revoked.",
            headers=WWW_AUTHENTICATE_HEADER,
        )
    except firebase_auth.CertificateFetchError as e:
        logger.error(f"Failed to fetch Google public signing certificates: {e}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Unable to verify authentication token at this time.",
        )
    except (firebase_auth.InvalidIdTokenError, ValueError) as e:
        logger.warning(f"Rejected invalid ID token: {e}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication token.",
            headers=WWW_AUTHENTICATE_HEADER,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Unexpected error during ID token verification: {e}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication failed.",
            headers=WWW_AUTHENTICATE_HEADER,
        )


def get_current_user_info(
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
    x_dev_user_id: Optional[str] = Header(default=None, alias="X-Dev-User-Id"),
) -> AuthenticatedUser:
    """FastAPI dependency returning full verified user metadata (`uid`, `email`, `name`)."""
    if is_local_auth_bypass_enabled():
        raw_dev_uid = (x_dev_user_id or os.getenv("DEV_USER_ID", "local-dev")).strip()
        uid = validate_uid(raw_dev_uid)
        return AuthenticatedUser(
            uid=uid,
            email=f"{uid}@local.dev",
            name=f"Local Dev ({uid})",
        )

    token = _extract_bearer_token(authorization)
    claims = verify_bearer_token(token)
    raw_uid = claims.get("uid") or claims.get("sub")
    uid = validate_uid(raw_uid)

    return AuthenticatedUser(
        uid=uid,
        email=claims.get("email"),
        name=claims.get("name"),
    )


def get_current_user(
    user: AuthenticatedUser = Depends(get_current_user_info),
) -> str:
    """FastAPI dependency returning the verified, path-safe user `uid` string."""
    return user.uid
