#!/usr/bin/env python3
"""Verifies that the configured deployment region adheres to the live GCP
`constraints/gcp.resourceLocations` Organization Policy on the target project.
"""

import argparse
import json
import re
import subprocess
import sys
from typing import Any, Dict, List, Set, Tuple

GCP_SINGLE_REGION_RE = re.compile(r"^[a-z]+-[a-z]+[0-9]+$")
LOCATION_GROUP_SUFFIX = "-locations"


def _normalize_policy_values(values: List[str]) -> Set[str]:
    """Normalizes GCP Organization Policy location strings by stripping `in:` prefixes.

    Args:
        values: Raw `allowedValues` or `deniedValues` list from `listPolicy`.

    Returns:
        Lowercased set of location tokens without `in:` prefixes.
    """
    normalized: Set[str] = set()
    for raw in values or []:
        cleaned = (raw or "").strip().lower()
        if cleaned.startswith("in:"):
            cleaned = cleaned[3:]
        if cleaned:
            normalized.add(cleaned)
    return normalized


def _matches_location_set(region: str, normalized_values: Set[str]) -> bool:
    """Checks whether `region` matches an exact region or macro location group in `normalized_values`.

    Args:
        region: Lowercased single GCP region (e.g., `"us-central1"`).
        normalized_values: Normalized Organization Policy location tokens.

    Returns:
        `True` if `region` is covered by `normalized_values`, otherwise `False`.
    """
    if region in normalized_values or f"{region}{LOCATION_GROUP_SUFFIX}" in normalized_values:
        return True
    if region.startswith("us-") and (
        "us-locations" in normalized_values or "americas-locations" in normalized_values
    ):
        return True
    if region.startswith("europe-") and (
        "eu-locations" in normalized_values or "europe-locations" in normalized_values
    ):
        return True
    if region.startswith("asia-") and (
        "asia-locations" in normalized_values or "asia-pacific-locations" in normalized_values
    ):
        return True
    return False


def _summarize_allowed_regions(normalized_allowed: Set[str]) -> str:
    """Formats normalized Organization Policy `allowedValues` into a human-readable summary.

    Args:
        normalized_allowed: Normalized Organization Policy `allowedValues` tokens.

    Returns:
        Comma-separated string of concrete regions and macro location groups.
    """
    regions: Set[str] = set()
    macro_groups: Set[str] = set()
    for val in normalized_allowed:
        if GCP_SINGLE_REGION_RE.match(val):
            regions.add(val)
        elif val.endswith(LOCATION_GROUP_SUFFIX):
            candidate = val[: -len(LOCATION_GROUP_SUFFIX)]
            if GCP_SINGLE_REGION_RE.match(candidate):
                regions.add(candidate)
            else:
                macro_groups.add(f"in:{val}")
    summary = sorted(regions) + sorted(macro_groups)
    return ", ".join(summary) if summary else ", ".join(sorted(normalized_allowed))


def evaluate_effective_location_policy(
    policy_payload: Dict[str, Any],
    region: str,
    project_id: str = "",
) -> Tuple[bool, str]:
    """Evaluates whether `region` is a concrete single GCP region permitted by `policy_payload`."""
    cleaned_region = (region or "").strip().lower()
    if not GCP_SINGLE_REGION_RE.match(cleaned_region):
        return (
            False,
            f"Invalid region '{region}'. Data residency requires a concrete single GCP region "
            f"(e.g., 'us-central1'), never 'global' or a multi-region.",
        )

    list_policy = (policy_payload or {}).get("listPolicy") or {}
    all_values = str(list_policy.get("allValues", "")).upper()
    raw_allowed = list_policy.get("allowedValues") or []
    raw_denied = list_policy.get("deniedValues") or []

    if all_values == "ALLOW" or not raw_allowed:
        target = f"project '{project_id}'" if project_id else "target project"
        return (
            False,
            f"Organization Policy 'constraints/gcp.resourceLocations' is not restricting locations "
            f"on {target} (allValues={all_values or 'UNSET'}). Configure allowed regions in GCP Console.",
        )

    normalized_denied = _normalize_policy_values(raw_denied)
    if _matches_location_set(cleaned_region, normalized_denied):
        return (
            False,
            f"Region '{cleaned_region}' is explicitly denied by effective GCP Organization Policy "
            f"'constraints/gcp.resourceLocations'.",
        )

    normalized_allowed = _normalize_policy_values(raw_allowed)
    allowed_summary = _summarize_allowed_regions(normalized_allowed)
    if not _matches_location_set(cleaned_region, normalized_allowed):
        return (
            False,
            f"Region '{cleaned_region}' violates effective GCP Organization Policy "
            f"'constraints/gcp.resourceLocations'. Allowed regions: {allowed_summary}.",
        )

    return (
        True,
        f"Region '{cleaned_region}' adheres to effective GCP Organization Policy "
        f"'constraints/gcp.resourceLocations' (allowed: {allowed_summary}).",
    )


def fetch_effective_location_policy(project_id: str) -> Dict[str, Any]:
    """Fetches effective `constraints/gcp.resourceLocations` via ADC REST API or `gcloud` fallback."""
    adc_error = None
    try:
        import google.auth
        import google.auth.transport.requests
        import requests

        creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        creds.refresh(google.auth.transport.requests.Request())
        resp = requests.post(
            f"https://cloudresourcemanager.googleapis.com/v1/projects/{project_id}:getEffectiveOrgPolicy",
            headers={
                "Authorization": f"Bearer {creds.token}",
                "X-Goog-User-Project": project_id,
            },
            json={"constraint": "constraints/gcp.resourceLocations"},
            timeout=15,
        )
        if resp.status_code == 200:
            return resp.json()
        adc_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
    except Exception as exc:
        adc_error = str(exc)

    proc = subprocess.run(
        [
            "gcloud",
            "resource-manager",
            "org-policies",
            "describe",
            "gcp.resourceLocations",
            f"--project={project_id}",
            "--effective",
            "--format=json",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        detail = proc.stderr.strip() or adc_error or "Unknown authentication error"
        raise RuntimeError(
            f"Unable to read effective GCP Organization Policy for project '{project_id}'. "
            f"Run 'gcloud auth application-default login' or 'gcloud auth login'. Details: {detail}"
        )
    return json.loads(proc.stdout)


def main() -> int:
    """CLI entrypoint verifying `--region` against the live project Organization Policy.

    Returns:
        Exit status `0` when compliant, or `1` when invalid or non-compliant.
    """
    parser = argparse.ArgumentParser(
        description="Verify deployment region against live GCP Organization Policy (gcp.resourceLocations)."
    )
    parser.add_argument("--project", required=True, help="GCP Project ID")
    parser.add_argument("--region", required=True, help="Configured deployment GCP region")
    args = parser.parse_args()

    if not GCP_SINGLE_REGION_RE.match((args.region or "").strip().lower()):
        _, msg = evaluate_effective_location_policy({}, args.region, project_id=args.project)
        print(f"ERROR: {msg}", file=sys.stderr)
        return 1

    try:
        payload = fetch_effective_location_policy(args.project)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    ok, message = evaluate_effective_location_policy(payload, args.region, project_id=args.project)
    if not ok:
        print(f"ERROR: {message}", file=sys.stderr)
        return 1

    print(f"OK: {message}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
