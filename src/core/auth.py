"""Google Identity Platform / Firebase JWT authentication and tenant UID validation.

Implements Pillar 1 & Pillar 2 identity enforcement for FastAPI endpoints:
- Verifies RS256 Bearer ID tokens via the Firebase Admin SDK using Application Default Credentials.
- Validates user identifiers (`UID_PATTERN`) to prevent filesystem and GCS prefix path traversal.
- Supports local-only developer bypass (`AUTH_DISABLED=true`) while hard-blocking bypass in Cloud Run.
- Records structured `auth.failure` compliance audit events and binds verified UIDs to request state.
"""

import re
import threading
from typing import Any, Dict, Optional

import firebase_admin
from fastapi import Depends, Header, HTTPException, status
from firebase_admin import auth as firebase_auth
from pydantic import BaseModel

from src.core.compliance.audit import emit_audit_event
from src.core.config import (
    get_allowed_origins,
    get_dev_user_id,
    get_firebase_project_id,
    is_auth_disabled,
    is_cloud_run,
)
from src.core.context import set_request_uid
from src.core.logger import setup_logging

logger = setup_logging(name="core.auth")

# Strict allowlist pattern preventing path traversal when constructing `/tmp/chroma_db/{uid}` or GCS prefixes
UID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")

_firebase_init_lock = threading.Lock()

WWW_AUTHENTICATE_HEADER = {"WWW-Authenticate": "Bearer"}


class AuthenticatedUser(BaseModel):
    """Verified user identity extracted from a Google Identity Platform / Firebase ID token.

    Attributes:
        uid: Path-safe unique user subject identifier.
        email: Optional primary email claim from the verified token.
        name: Optional display name claim from the verified token.
    """

    uid: str
    email: Optional[str] = None
    name: Optional[str] = None


def validate_uid(uid: Optional[str]) -> str:
    """Validates that a user ID is safe for filesystem directory and GCS object prefix construction.

    Args:
        uid: Candidate user identifier extracted from JWT claims or local dev headers.

    Returns:
        Validated `uid` string matching `UID_PATTERN`.

    Raises:
        HTTPException: HTTP 401 if `uid` is missing, empty, overlong, or contains unsafe characters.
    """
    if not uid or not isinstance(uid, str) or not UID_PATTERN.match(uid):
        logger.warning("Rejected unsafe or malformed uid format.")
        emit_audit_event(
            "auth.failure",
            severity="WARNING",
            status="denied",
            reason="invalid_uid_format",
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid user identifier format.",
            headers=WWW_AUTHENTICATE_HEADER,
        )
    return uid


def is_auth_bypassed() -> bool:
    """Checks whether local-only authentication bypass (`AUTH_DISABLED=true`) is active.

    Never allows bypass when `K_SERVICE` is set (Cloud Run), even if `AUTH_DISABLED=true`
    is accidentally configured in container environment variables.

    Returns:
        True only when `AUTH_DISABLED` is truthy and the service is not running in Cloud Run.
    """
    if not is_auth_disabled():
        return False

    if is_cloud_run():
        logger.error(
            "AUTH_DISABLED=true is set inside Cloud Run (K_SERVICE detected). "
            "Ignoring bypass and enforcing strict JWT verification."
        )
        return False

    return True


def init_firebase() -> firebase_admin.App:
    """Initializes the Firebase Admin SDK once per process using Application Default Credentials.

    Returns:
        Initialized `firebase_admin.App` singleton bound to the configured GCP project.

    Raises:
        HTTPException: HTTP 500 if neither `GOOGLE_CLOUD_PROJECT` nor `FIREBASE_PROJECT_ID` is set.
    """
    if firebase_admin._apps:
        return firebase_admin.get_app()

    with _firebase_init_lock:
        if firebase_admin._apps:
            return firebase_admin.get_app()

        project_id = get_firebase_project_id()
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
    """Extracts the raw JWT string from an `'Authorization: Bearer <ID_TOKEN>'` HTTP header.

    Args:
        authorization: Raw `Authorization` request header value, if present.

    Returns:
        Trimmed raw JWT token string.

    Raises:
        HTTPException: HTTP 401 if the header is missing or does not follow `Bearer <token>` syntax.
    """
    if not authorization:
        emit_audit_event(
            "auth.failure",
            severity="WARNING",
            status="denied",
            reason="missing_authorization_header",
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header. Expected 'Bearer <ID_TOKEN>'.",
            headers=WWW_AUTHENTICATE_HEADER,
        )

    parts = authorization.strip().split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        emit_audit_event(
            "auth.failure",
            severity="WARNING",
            status="denied",
            reason="malformed_authorization_header",
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Malformed Authorization header. Expected 'Bearer <ID_TOKEN>'.",
            headers=WWW_AUTHENTICATE_HEADER,
        )

    return parts[1].strip()


def verify_bearer_token(token: str) -> Dict[str, Any]:
    """Verifies a Firebase / Google Identity Platform ID token and returns decoded claims.

    Args:
        token: Raw RS256 JWT extracted from the `Authorization: Bearer` header.

    Returns:
        Dictionary of verified JWT claims (including `uid`/`sub`, `email`, and `name`).

    Raises:
        HTTPException: HTTP 401 on expired, revoked, or invalid tokens; HTTP 503 if signing
            certificates cannot be fetched.
    """
    app = init_firebase()
    try:
        decoded_claims = firebase_auth.verify_id_token(
            token,
            app=app,
            check_revoked=False,
        )
        return decoded_claims
    except firebase_auth.ExpiredIdTokenError:
        emit_audit_event(
            "auth.failure",
            severity="WARNING",
            status="denied",
            reason="expired_token",
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication token has expired.",
            headers=WWW_AUTHENTICATE_HEADER,
        )
    except firebase_auth.RevokedIdTokenError:
        emit_audit_event(
            "auth.failure",
            severity="WARNING",
            status="denied",
            reason="revoked_token",
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication token has been revoked.",
            headers=WWW_AUTHENTICATE_HEADER,
        )
    except firebase_auth.CertificateFetchError as e:
        logger.error(f"Failed to fetch Google public signing certificates: {e}")
        emit_audit_event(
            "auth.failure",
            severity="ERROR",
            status="error",
            reason="certificate_fetch_error",
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Unable to verify authentication token at this time.",
        )
    except (firebase_auth.InvalidIdTokenError, ValueError) as e:
        logger.warning(f"Rejected invalid ID token: {e}")
        emit_audit_event(
            "auth.failure",
            severity="WARNING",
            status="denied",
            reason="invalid_token",
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication token.",
            headers=WWW_AUTHENTICATE_HEADER,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Unexpected error during ID token verification: {e}")
        emit_audit_event(
            "auth.failure",
            severity="WARNING",
            status="denied",
            reason="verification_error",
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication failed.",
            headers=WWW_AUTHENTICATE_HEADER,
        )


def get_current_user_info(
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
    x_dev_user_id: Optional[str] = Header(default=None, alias="X-Dev-User-Id"),
) -> AuthenticatedUser:
    """FastAPI dependency returning full verified user metadata (`uid`, `email`, `name`).

    Binds the verified `uid` into the active request's compliance audit context so
    downstream storage, guardrail, and endpoint events carry tenant attribution.

    Args:
        authorization: Optional `Authorization: Bearer <ID_TOKEN>` header.
        x_dev_user_id: Optional `X-Dev-User-Id` header honored only when `is_auth_bypassed()` is True.

    Returns:
        Verified `AuthenticatedUser` model instance.
    """
    if is_auth_bypassed():
        raw_dev_uid = (x_dev_user_id or get_dev_user_id()).strip()
        uid = validate_uid(raw_dev_uid)
        set_request_uid(uid)
        return AuthenticatedUser(
            uid=uid,
            email=f"{uid}@local.dev",
            name=f"Local Dev ({uid})",
        )

    token = _extract_bearer_token(authorization)
    claims = verify_bearer_token(token)
    raw_uid = claims.get("uid") or claims.get("sub")
    uid = validate_uid(raw_uid)
    set_request_uid(uid)

    return AuthenticatedUser(
        uid=uid,
        email=claims.get("email"),
        name=claims.get("name"),
    )


def get_current_user(
    user: AuthenticatedUser = Depends(get_current_user_info),
) -> str:
    """FastAPI dependency returning the verified, path-safe tenant `uid` string.

    Args:
        user: Verified `AuthenticatedUser` injected by `get_current_user_info`.

    Returns:
        Path-safe tenant `uid` string.
    """
    return user.uid


__all__ = [
    "AuthenticatedUser",
    "UID_PATTERN",
    "WWW_AUTHENTICATE_HEADER",
    "get_allowed_origins",
    "get_current_user",
    "get_current_user_info",
    "init_firebase",
    "is_auth_bypassed",
    "is_cloud_run",
    "validate_uid",
    "verify_bearer_token",
]
