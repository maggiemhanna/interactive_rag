#!/usr/bin/env python3
"""Static pre-deploy policy gate for InteractiveRAG Python config and Terraform HCL.

Verifies offline compliance & security invariants before `make plan` and `make deploy`:
1. Neither `src/core/config.py` nor Terraform (`infra/*.tf`, optional `infra/terraform.tfvars`)
   defaults to or hardcodes `'global'` or multi-region (`'us'`, `'eu'`, `'asia'`) locations.
2. Cloud Run Terraform (`infra/cloud_run.tf` & `infra/variables.tf`) enforces fail-closed
   Model Armor screening (`MODEL_ARMOR_FAIL_CLOSED` enabled by default, never disabled on backend services).
3. All three Cloud Run services (`ingestion_api`, `agent_api`, `frontend`) guard `prod` with
   `deletion_protection = var.environment == "prod"`.
"""

import argparse
import os
import re
import sys
from typing import List, Optional, Tuple

GCP_SINGLE_REGION_RE = re.compile(r"^[a-z]+-[a-z]+[0-9]+$")
FORBIDDEN_MULTI_LOCATIONS = {"global", "us", "eu", "asia", "nam4", "eur4"}

_TF_VAR_BLOCK_RE = re.compile(
    r'variable\s+"(?P<name>[^"]+)"\s*\{(?P<body>.*?)\n\}',
    re.DOTALL,
)
_CLOUD_RUN_SERVICE_RE = re.compile(
    r'resource\s+"google_cloud_run_v2_service"\s+"(?P<name>[^"]+)"\s*\{(?P<body>.*?)\n\}',
    re.DOTALL,
)
_ENV_BLOCK_RE = re.compile(
    r"env\s*\{\s*name\s*=\s*\"(?P<name>[^\"]+)\"\s*value\s*=\s*(?P<value>[^\n\}]+?)\s*\}",
    re.DOTALL,
)


def _extract_tf_default(var_body: str) -> Optional[str]:
    """Extracts the raw `default = ...` expression from a Terraform `variable` block body.

    Args:
        var_body: Inner HCL text of a `variable "<name>" { ... }` block.

    Returns:
        Stripped default value token (with surrounding quotes preserved if string), or `None`.
    """
    match = re.search(r"^\s*default\s*=\s*(.+?)\s*$", var_body, re.MULTILINE)
    return match.group(1).strip() if match else None


def _check_python_config(repo_root: str) -> List[str]:
    """Checks `src/core/config.py` for single-region `GOOGLE_CLOUD_LOCATION` defaults.

    Args:
        repo_root: Repository root directory path.

    Returns:
        List of human-readable policy violation messages (empty when compliant).
    """
    violations: List[str] = []
    config_path = os.path.join(repo_root, "src", "core", "config.py")
    if not os.path.isfile(config_path):
        return [f"Missing required Python config file: {config_path}"]

    with open(config_path, encoding="utf-8") as fh:
        content = fh.read()

    if re.search(r"\bDEFAULT_LOCATION\b", content):
        violations.append(
            "src/core/config.py must not define legacy DEFAULT_LOCATION; unify on GOOGLE_CLOUD_LOCATION."
        )

    loc_fn_match = re.search(
        r"def\s+get_google_cloud_location\(\).*?return\s+([^\n]+)",
        content,
        re.DOTALL,
    )
    if not loc_fn_match:
        violations.append("src/core/config.py is missing get_google_cloud_location().")
    else:
        return_expr = loc_fn_match.group(1)
        fallback_literals = re.findall(r'or\s+["\']([^"\']+)["\']', return_expr)
        if not fallback_literals:
            violations.append(
                "src/core/config.py get_google_cloud_location() must declare an explicit regional fallback."
            )
        for literal in fallback_literals:
            if not GCP_SINGLE_REGION_RE.match(literal.strip().lower()):
                violations.append(
                    f"src/core/config.py get_google_cloud_location() fallback '{literal}' is not a concrete single GCP region."
                )

    return violations


def _check_terraform_variables(infra_dir: str) -> List[str]:
    """Checks `infra/variables.tf` for single-region `region` and `model_armor_fail_closed = true`.

    Args:
        infra_dir: Path to the `infra/` Terraform directory.

    Returns:
        List of policy violation messages (empty when compliant).
    """
    violations: List[str] = []
    vars_path = os.path.join(infra_dir, "variables.tf")
    if not os.path.isfile(vars_path):
        return [f"Missing required Terraform variables file: {vars_path}"]

    with open(vars_path, encoding="utf-8") as fh:
        content = fh.read()

    var_blocks = {m.group("name"): m.group("body") for m in _TF_VAR_BLOCK_RE.finditer(content)}

    if "region" not in var_blocks:
        violations.append('infra/variables.tf is missing variable "region".')
    else:
        raw_default = _extract_tf_default(var_blocks["region"])
        unquoted = (raw_default or "").strip().strip('"').lower()
        if not GCP_SINGLE_REGION_RE.match(unquoted):
            violations.append(
                f'infra/variables.tf variable "region" default ({raw_default!r}) must be a concrete single GCP region.'
            )

    if "model_armor_fail_closed" not in var_blocks:
        violations.append('infra/variables.tf is missing variable "model_armor_fail_closed".')
    else:
        raw_fail_closed = (_extract_tf_default(var_blocks["model_armor_fail_closed"]) or "").lower()
        if raw_fail_closed != "true":
            violations.append(
                f'infra/variables.tf variable "model_armor_fail_closed" must default to true (found {raw_fail_closed!r}).'
            )

    return violations


def _check_cloud_run_tf(infra_dir: str) -> List[str]:
    """Checks `infra/cloud_run.tf` for prod deletion protection, regional pinning, and fail-closed Model Armor.

    Args:
        infra_dir: Path to the `infra/` Terraform directory.

    Returns:
        List of policy violation messages (empty when compliant).
    """
    violations: List[str] = []
    cloud_run_path = os.path.join(infra_dir, "cloud_run.tf")
    if not os.path.isfile(cloud_run_path):
        return [f"Missing required Terraform file: {cloud_run_path}"]

    with open(cloud_run_path, encoding="utf-8") as fh:
        content = fh.read()

    services = {m.group("name"): m.group("body") for m in _CLOUD_RUN_SERVICE_RE.finditer(content)}
    expected_services = ("ingestion_api", "agent_api", "frontend")
    for svc_name in expected_services:
        if svc_name not in services:
            violations.append(
                f'infra/cloud_run.tf is missing google_cloud_run_v2_service "{svc_name}".'
            )
            continue

        body = services[svc_name]
        del_match = re.search(r"^\s*deletion_protection\s*=\s*(.+?)\s*$", body, re.MULTILINE)
        if not del_match:
            violations.append(
                f'infra/cloud_run.tf service "{svc_name}" must explicitly configure deletion_protection.'
            )
        else:
            normalized_expr = re.sub(r"\s+", " ", del_match.group(1).strip())
            if normalized_expr != 'var.environment == "prod"':
                violations.append(
                    f'infra/cloud_run.tf service "{svc_name}" must set '
                    f'deletion_protection = var.environment == "prod" (found `{normalized_expr}`).'
                )

    for backend_svc in ("ingestion_api", "agent_api"):
        if backend_svc not in services:
            continue
        env_map = {
            m.group("name"): m.group("value").strip()
            for m in _ENV_BLOCK_RE.finditer(services[backend_svc])
        }
        for loc_env in ("GOOGLE_CLOUD_LOCATION", "MODEL_ARMOR_LOCATION"):
            raw_loc = env_map.get(loc_env)
            if not raw_loc:
                violations.append(
                    f'infra/cloud_run.tf service "{backend_svc}" must set {loc_env}.'
                )
            elif raw_loc.strip('"').lower() in FORBIDDEN_MULTI_LOCATIONS:
                violations.append(
                    f'infra/cloud_run.tf service "{backend_svc}" sets {loc_env}={raw_loc}, violating single-region residency.'
                )

        fail_closed_val = env_map.get("MODEL_ARMOR_FAIL_CLOSED")
        if not fail_closed_val:
            violations.append(
                f'infra/cloud_run.tf service "{backend_svc}" must set MODEL_ARMOR_FAIL_CLOSED.'
            )
        elif fail_closed_val.strip('"').lower() in {"false", "0", "no"}:
            violations.append(
                f'infra/cloud_run.tf service "{backend_svc}" must not disable MODEL_ARMOR_FAIL_CLOSED ({fail_closed_val}).'
            )

    return violations


def _check_all_tf_locations_and_tfvars(infra_dir: str) -> List[str]:
    """Scans all `infra/*.tf` files and optional `terraform.tfvars` for forbidden global/multi-region or fail-open overrides.

    Args:
        infra_dir: Path to the `infra/` Terraform directory.

    Returns:
        List of policy violation messages (empty when compliant).
    """
    violations: List[str] = []
    if not os.path.isdir(infra_dir):
        return [f"Missing Terraform directory: {infra_dir}"]

    for entry in sorted(os.listdir(infra_dir)):
        if not entry.endswith(".tf"):
            continue
        tf_path = os.path.join(infra_dir, entry)
        with open(tf_path, encoding="utf-8") as fh:
            tf_text = fh.read()
        for loc_match in re.finditer(
            r'^\s*location\s*=\s*"(?P<loc>[^"]+)"', tf_text, re.MULTILINE
        ):
            loc_val = loc_match.group("loc").strip().lower()
            if loc_val in FORBIDDEN_MULTI_LOCATIONS:
                violations.append(
                    f'infra/{entry} hardcodes non-regional location = "{loc_match.group("loc")}".'
                )

    tfvars_path = os.path.join(infra_dir, "terraform.tfvars")
    if os.path.isfile(tfvars_path):
        with open(tfvars_path, encoding="utf-8") as fh:
            tfvars_text = fh.read()

        region_match = re.search(
            r'^\s*region\s*=\s*"([^"]*)"', tfvars_text, re.MULTILINE
        )
        if region_match:
            tfvars_region = region_match.group(1).strip().lower()
            if not GCP_SINGLE_REGION_RE.match(tfvars_region):
                violations.append(
                    f"infra/terraform.tfvars sets invalid non-single-region region = {region_match.group(1)!r}."
                )

        fail_closed_match = re.search(
            r"^\s*model_armor_fail_closed\s*=\s*(false|0)\b",
            tfvars_text,
            re.MULTILINE | re.IGNORECASE,
        )
        if fail_closed_match:
            violations.append(
                "infra/terraform.tfvars disables model_armor_fail_closed; Cloud Run deployments must remain fail-closed."
            )

    return violations


def evaluate_repo_static_policies(repo_root: str) -> Tuple[bool, List[str]]:
    """Evaluates all static residency, fail-closed screening, and deletion-protection policies for `repo_root`.

    Args:
        repo_root: Root directory of the InteractiveRAG repository.

    Returns:
        Tuple `(ok, violations)` where `ok` is `True` iff `violations` is empty.
    """
    infra_dir = os.path.join(repo_root, "infra")
    violations: List[str] = []
    violations.extend(_check_python_config(repo_root))
    violations.extend(_check_terraform_variables(infra_dir))
    violations.extend(_check_cloud_run_tf(infra_dir))
    violations.extend(_check_all_tf_locations_and_tfvars(infra_dir))
    return len(violations) == 0, violations


def main() -> int:
    """CLI entrypoint running static compliance and security policy checks against the repository.

    Returns:
        Exit status `0` when all static checks pass, or `1` when any violation is found.
    """
    default_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    parser = argparse.ArgumentParser(
        description="Verify static single-region residency, fail-closed Model Armor, and prod deletion protection policies."
    )
    parser.add_argument(
        "--repo-root",
        default=default_root,
        help="Path to the InteractiveRAG repository root.",
    )
    args = parser.parse_args()

    ok, violations = evaluate_repo_static_policies(args.repo_root)
    if not ok:
        for item in violations:
            print(f"ERROR: {item}", file=sys.stderr)
        return 1

    print(
        "OK: Static policy check passed (single-region defaults, fail-closed Model Armor, and prod deletion_protection verified)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
