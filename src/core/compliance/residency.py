"""Single-region GCP data residency validation (Pillar 4: Compliance & Governance).

Enforces at runtime that Vertex AI generative models, Vertex AI embeddings, and Google Cloud
Model Armor endpoints always execute inside a concrete single GCP region (such as `'us-central1'`),
rejecting `'global'` and multi-region identifiers (`'us'`, `'eu'`, `'asia'`, `'nam4'`, `'eur4'`).
"""

from typing import Optional

from src.core.config import GCP_REGION_PATTERN, get_google_cloud_location


def validate_gcp_region(location: Optional[str] = None) -> str:
    """Validates that a GCP location is a concrete single region, never `'global'` or a multi-region.

    Args:
        location: Optional GCP location override; defaults to `get_google_cloud_location()`.

    Returns:
        Lowercase, trimmed single-region identifier (e.g., `'us-central1'`).

    Raises:
        ValueError: If the location is empty, `'global'`, a multi-region, or malformed.
    """
    raw = location if location is not None else get_google_cloud_location()
    cleaned = (raw or "").strip().lower()
    if not GCP_REGION_PATTERN.match(cleaned):
        raise ValueError(
            f"Invalid GOOGLE_CLOUD_LOCATION '{raw}'. Data residency requires a concrete single GCP "
            f"region (e.g., 'us-central1'), never 'global' or multi-regions ('us', 'eu', 'asia')."
        )
    return cleaned


__all__ = ["validate_gcp_region"]
