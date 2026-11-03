"""Composite router for the `src.manage` control-plane package.

Combines `collections_router` (`GET /collections`, `DELETE /collections/{collection_name}`)
and `compliance_router` (`GET /compliance/my-data`, `DELETE /compliance/my-data`) into a
single `manage_router` mounted on `agent-api`.
"""

from fastapi import APIRouter

from src.manage.collections import collections_router
from src.manage.compliance import compliance_router

manage_router = APIRouter()
manage_router.include_router(collections_router)
manage_router.include_router(compliance_router)

__all__ = ["manage_router"]
