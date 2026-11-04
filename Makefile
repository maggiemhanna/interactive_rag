# ==============================================================================
# InteractiveRAG — Reproducible Cloud Build, Terraform & Governance Automation
#
# Typical end-to-end deployment workflow:
#   1. `make bootstrap`    -> Enables seed GCP APIs, creates the versioned GCS Terraform
#                             state bucket, configures the least-privilege deployer SA,
#                             and provisions Artifact Registry.
#   2. `make build-push`   -> Builds & pushes `linux/amd64` backend and frontend images
#                             via Google Cloud Build.
#   3. `make policy-check` -> Verifies live GCP Organization Policy (`gcp.resourceLocations`),
#                             static single-region / fail-closed / deletion-protection rules,
#                             and `terraform validate` (also runs automatically before `plan`/`deploy`).
#   4. `make deploy`       -> Applies the full Terraform stack in `infra/`.
#   5. `make outputs`      -> Prints the deployed HTTPS Load Balancer URL and resource IDs.
# ==============================================================================

.DEFAULT_GOAL := help

# ==============================================================================
# 1. Project, Region & State Backend Resolution
# ==============================================================================

INFRA_DIR      := infra
TF_DIR         := $(INFRA_DIR)/terraform
CLOUDBUILD_DIR := $(INFRA_DIR)/cloudbuild
DOCKER_DIR     := $(INFRA_DIR)/docker
SCRIPTS_DIR    := $(INFRA_DIR)/scripts
TFVARS         := $(TF_DIR)/terraform.tfvars

# Auto-detect `project_id`, `region`, `app_name`, and `environment` from `infra/terraform/terraform.tfvars`
# if present so `make plan` / `make deploy` work without repeating CLI flags on every command.
TFVARS_PROJECT := $(shell [ -f $(TFVARS) ] && sed -n 's/^[[:space:]]*project_id[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' $(TFVARS) | head -n 1)
TFVARS_REGION  := $(shell [ -f $(TFVARS) ] && sed -n 's/^[[:space:]]*region[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' $(TFVARS) | head -n 1)
TFVARS_APP     := $(shell [ -f $(TFVARS) ] && sed -n 's/^[[:space:]]*app_name[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' $(TFVARS) | head -n 1)
TFVARS_ENV     := $(shell [ -f $(TFVARS) ] && sed -n 's/^[[:space:]]*environment[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' $(TFVARS) | head -n 1)

PROJECT_ID     ?= $(if $(TFVARS_PROJECT),$(TFVARS_PROJECT),interactive-rag)
REGION         ?= $(if $(TFVARS_REGION),$(TFVARS_REGION),us-central1)
APP_NAME       ?= $(if $(TFVARS_APP),$(TFVARS_APP),interactive-rag)
ENV            ?= $(if $(TFVARS_ENV),$(TFVARS_ENV),prod)
IMAGE_TAG      ?= $(shell git rev-parse --short HEAD 2>/dev/null || echo latest)
PROJECT_NUMBER ?= $(shell gcloud projects describe "$(PROJECT_ID)" --format="value(projectNumber)" 2>/dev/null)

# Globally unique GCS bucket for remote Terraform state (`infra/terraform/versions.tf`) and Cloud Build logs.
TF_STATE_BUCKET ?= $(if $(PROJECT_NUMBER),$(APP_NAME)-tfstate-$(PROJECT_NUMBER),$(PROJECT_ID)-tfstate)
STATE_PREFIX    := $(APP_NAME)/$(ENV)/state
REPO_NAME       := $(APP_NAME)-repo
REGISTRY_URL    := $(REGION)-docker.pkg.dev/$(PROJECT_ID)/$(REPO_NAME)

# ==============================================================================
# 2. Terraform Execution Mode & Least-Privilege Deployer Service Account
# ==============================================================================

# Dedicated deployer service account used when running image builds or remote Terraform jobs
# in Google Cloud Build (avoids relying on the legacy overly permissive Cloud Build SA).
TF_DEPLOYER_ID  := $(APP_NAME)-deployer
TF_DEPLOYER_SA  := $(TF_DEPLOYER_ID)@$(PROJECT_ID).iam.gserviceaccount.com

# Hybrid execution mode (`TF_MODE`):
# - `local`:      Uses the local `terraform` binary if installed (passing short-lived
#                 `GOOGLE_OAUTH_ACCESS_TOKEN` credentials from `gcloud auth print-access-token`).
# - `cloudbuild`: Automatically falls back to running Terraform inside Google Cloud Build
#                 (`infra/cloudbuild/run-terraform.yaml`) if `terraform` is not installed locally.
HAS_LOCAL_TF := $(shell command -v terraform >/dev/null 2>&1 && terraform -version >/dev/null 2>&1 && echo yes || echo no)
TF_MODE      ?= $(if $(filter yes,$(HAS_LOCAL_TF)),local,cloudbuild)

# Scoped IAM roles granted to `TF_DEPLOYER_SA` so it can provision the infrastructure defined in `infra/terraform/`.
# Note: `roles/orgpolicy.policyAdmin` is intentionally NOT granted (Separation of Duties: the deployer
# can only read effective Organization Policies, never weaken or modify them).
TF_DEPLOYER_ROLES := \
	roles/run.admin \
	roles/storage.admin \
	roles/artifactregistry.admin \
	roles/compute.networkAdmin \
	roles/compute.loadBalancerAdmin \
	roles/compute.securityAdmin \
	roles/dns.admin \
	roles/iam.serviceAccountAdmin \
	roles/iam.serviceAccountUser \
	roles/resourcemanager.projectIamAdmin \
	roles/serviceusage.serviceUsageAdmin \
	roles/serviceusage.apiKeysAdmin \
	roles/identityplatform.admin \
	roles/modelarmor.admin \
	roles/dlp.admin \
	roles/networkconnectivity.regionalEndpointAdmin \
	roles/secretmanager.secretAccessor \
	roles/secretmanager.viewer \
	roles/browser \
	roles/logging.logWriter \
	roles/logging.configWriter \
	roles/monitoring.admin

# Prefer the repository virtual environment (`.venv/bin/python`) when running compliance scripts.
PYTHON       ?= $(shell [ -x .venv/bin/python ] && echo .venv/bin/python || command -v python3)

.PHONY: help check-project check-region-policy check-static-policy policy-check bootstrap ensure-deployer-sa tf-init build-push build-push-local plan deploy outputs destroy fmt validate

# ==============================================================================
# 3. Help & Pre-Flight Validation Gates (`policy-check`)
# ==============================================================================

help: ## Show available reproducible build & deployment commands
	@echo "InteractiveRAG — GCP Cloud Run & Terraform Automation"
	@echo ""
	@echo "Resolved configuration:"
	@echo "  PROJECT_ID      = $(PROJECT_ID) (number: $(if $(PROJECT_NUMBER),$(PROJECT_NUMBER),UNRESOLVED - run 'gcloud auth login'))"
	@echo "  REGION          = $(REGION)"
	@echo "  APP_NAME        = $(APP_NAME)"
	@echo "  ENV             = $(ENV)"
	@echo "  IMAGE_TAG       = $(IMAGE_TAG)"
	@echo "  TF_STATE_BUCKET = $(TF_STATE_BUCKET)"
	@echo "  TF_MODE         = $(TF_MODE)"
	@echo ""
	@echo "Targets:"
	@grep -E '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'

# Verifies that `PROJECT_ID` is configured and that the active `gcloud` CLI session can
# resolve `PROJECT_NUMBER` (catches expired `gcloud auth login` sessions early).
check-project:
	@if [ -z "$(PROJECT_ID)" ] || [ "$(PROJECT_ID)" = "your-gcp-project-id" ]; then \
		echo "Error: PROJECT_ID is not set. Populate $(TFVARS) or pass PROJECT_ID=<gcp-project-id>."; \
		exit 1; \
	fi
	@if [ -z "$(PROJECT_NUMBER)" ]; then \
		echo "Error: Could not resolve PROJECT_NUMBER for '$(PROJECT_ID)' via gcloud CLI."; \
		echo "       Your gcloud CLI session may have expired (note: 'gcloud' CLI uses 'gcloud auth login', not ADC)."; \
		echo "       Run 'gcloud auth login' (or complete any reauth prompt below) and re-run make."; \
		gcloud projects describe "$(PROJECT_ID)" --format="value(projectNumber)" >/dev/null || true; \
		exit 1; \
	fi

# Gate 1 (Live GCP Organization Policy Check):
# Queries `gcloud resource-manager org-policies describe gcp.resourceLocations --effective`
# to confirm an active location restriction policy exists on `PROJECT_ID` and permits `REGION`.
check-region-policy: ## Verify REGION adheres to live GCP gcp.resourceLocations Organization Policy
	@echo "==> Verifying REGION ($(REGION)) against live GCP Organization Policy on $(PROJECT_ID)..."
	@$(PYTHON) $(SCRIPTS_DIR)/check_region_policy.py --project="$(PROJECT_ID)" --region="$(REGION)"

# Gate 2 (Offline Static Code & HCL Policy Check):
# Scans `src/core/config.py`, `infra/terraform/*.tf`, and optional `infra/terraform/terraform.tfvars` to ensure
# single-region pinning (never `"global"`), fail-closed Model Armor, and `prod` deletion protection.
check-static-policy: ## Verify static single-region defaults, fail-closed Model Armor, and prod deletion protection
	@echo "==> Verifying static compliance & security policies..."
	@$(PYTHON) $(SCRIPTS_DIR)/check_static_policies.py

# Combined Pre-Deploy Gate (wired automatically as a prerequisite of `plan` and `deploy`):
# Runs project auth check + live Organization Policy check + static policy check + `terraform validate`.
policy-check: check-project check-region-policy check-static-policy ## Run pre-deploy Org Policy, static policy, and Terraform validation checks
	@$(MAKE) validate
	@echo "==> All compliance & governance pre-deploy policy checks passed."

# ==============================================================================
# 4. Project Bootstrap & Remote State Initialization
# ==============================================================================

# Creates `TF_DEPLOYER_SA` if missing and binds the least-privilege `TF_DEPLOYER_ROLES` above.
ensure-deployer-sa: check-project ## Create least-privilege Cloud Build deployer service account if missing
	@echo "==> Ensuring Cloud Build deployer service account ($(TF_DEPLOYER_SA))..."
	@if ! gcloud iam service-accounts describe "$(TF_DEPLOYER_SA)" --project="$(PROJECT_ID)" >/dev/null 2>&1; then \
		gcloud iam service-accounts create "$(TF_DEPLOYER_ID)" \
			--project="$(PROJECT_ID)" \
			--display-name="InteractiveRAG Cloud Build & Terraform Deployer"; \
		sleep 5; \
	fi
	@for role in $(TF_DEPLOYER_ROLES); do \
		gcloud projects add-iam-policy-binding "$(PROJECT_ID)" \
			--member="serviceAccount:$(TF_DEPLOYER_SA)" \
			--role="$$role" \
			--condition=None \
			--quiet >/dev/null; \
	done
	@echo "    Deployer service account $(TF_DEPLOYER_SA) configured."

# One-time (and idempotent) project bootstrap before building images or running `make deploy`:
#   1. Enables seed Google Cloud APIs required by Terraform and Cloud Build.
#   2. Creates the regional, versioned GCS bucket (`gs://$(TF_STATE_BUCKET)`) with uniform access
#      and public access prevention for storing remote Terraform state.
#   3. Configures `TF_DEPLOYER_SA` via `ensure-deployer-sa`.
#   4. Runs a targeted Terraform apply (`-target=google_artifact_registry_repository.docker_repo`)
#      so Artifact Registry exists before `make build-push` pushes Docker images.
bootstrap: check-project ## Enable seed APIs, create GCS Terraform state bucket, and create Artifact Registry
	@echo "==> Enabling required GCP APIs on project $(PROJECT_ID)..."
	gcloud services enable \
		storage.googleapis.com \
		artifactregistry.googleapis.com \
		cloudbuild.googleapis.com \
		compute.googleapis.com \
		dns.googleapis.com \
		networkconnectivity.googleapis.com \
		iam.googleapis.com \
		cloudresourcemanager.googleapis.com \
		serviceusage.googleapis.com \
		secretmanager.googleapis.com \
		identitytoolkit.googleapis.com \
		apikeys.googleapis.com \
		run.googleapis.com \
		aiplatform.googleapis.com \
		modelarmor.googleapis.com \
		dlp.googleapis.com \
		logging.googleapis.com \
		monitoring.googleapis.com \
		cloudtrace.googleapis.com \
		--project="$(PROJECT_ID)"
	@echo "==> Ensuring Terraform remote state bucket gs://$(TF_STATE_BUCKET) exists..."
	@if ! gcloud storage buckets describe "gs://$(TF_STATE_BUCKET)" --project="$(PROJECT_ID)" >/dev/null 2>&1; then \
		gcloud storage buckets create "gs://$(TF_STATE_BUCKET)" \
			--project="$(PROJECT_ID)" \
			--location="$(REGION)" \
			--uniform-bucket-level-access \
			--public-access-prevention; \
		gcloud storage buckets update "gs://$(TF_STATE_BUCKET)" --versioning --project="$(PROJECT_ID)"; \
	else \
		echo "    Bucket gs://$(TF_STATE_BUCKET) already exists."; \
	fi
	@$(MAKE) ensure-deployer-sa
	@echo "==> Provisioning Artifact Registry repository ($(REPO_NAME)) via Terraform ($(TF_MODE))..."
	@if [ "$(TF_MODE)" = "local" ]; then \
		$(MAKE) tf-init; \
		GOOGLE_OAUTH_ACCESS_TOKEN="$$(gcloud auth print-access-token)" \
		terraform -chdir=$(TF_DIR) apply \
			-target=google_artifact_registry_repository.docker_repo \
			-var="project_id=$(PROJECT_ID)" \
			-var="region=$(REGION)" \
			-var="image_tag=$(IMAGE_TAG)" \
			-auto-approve; \
	else \
		gcloud builds submit $(TF_DIR) \
			--project="$(PROJECT_ID)" \
			--region="$(REGION)" \
			--service-account="projects/$(PROJECT_ID)/serviceAccounts/$(TF_DEPLOYER_SA)" \
			--config=$(CLOUDBUILD_DIR)/run-terraform.yaml \
			--substitutions="_TF_ACTION=apply,_TF_STATE_BUCKET=$(TF_STATE_BUCKET),_STATE_PREFIX=$(STATE_PREFIX),_PROJECT_ID=$(PROJECT_ID),_REGION=$(REGION),_IMAGE_TAG=$(IMAGE_TAG),_TF_TARGET=google_artifact_registry_repository.docker_repo"; \
	fi

# Initializes `infra/terraform/` with the remote GCS state backend (`gs://$(TF_STATE_BUCKET)/$(STATE_PREFIX)`).
tf-init: check-project ## Initialize local Terraform with remote GCS state backend
	GOOGLE_OAUTH_ACCESS_TOKEN="$$(gcloud auth print-access-token)" \
	terraform -chdir=$(TF_DIR) init -reconfigure \
		-backend-config="bucket=$(TF_STATE_BUCKET)" \
		-backend-config="prefix=$(STATE_PREFIX)"

# ==============================================================================
# 5. Container Image Builds (`build-push` & `build-push-local`)
# ==============================================================================

# Recommended cloud build path: submits the repository context to Google Cloud Build (`infra/cloudbuild/build-images.yaml`)
# in `REGION` using `TF_DEPLOYER_SA` to build & push `linux/amd64` `backend` and `frontend` images
# (tagged with both `$(IMAGE_TAG)` and `latest`) without requiring a local Docker daemon.
build-push: check-project ## Build & push linux/amd64 backend and frontend images via Google Cloud Build
	gcloud builds submit \
		--project="$(PROJECT_ID)" \
		--region="$(REGION)" \
		--service-account="projects/$(PROJECT_ID)/serviceAccounts/$(TF_DEPLOYER_SA)" \
		--ignore-file=.dockerignore \
		--config=$(CLOUDBUILD_DIR)/build-images.yaml \
		--substitutions="_REGION=$(REGION),_REPO_NAME=$(REPO_NAME),_IMAGE_TAG=$(IMAGE_TAG),_LOGS_BUCKET=$(TF_STATE_BUCKET)" \
		.

# Optional local Docker Buildx path: cross-compiles `linux/amd64` images locally (e.g., on Apple Silicon)
# and pushes directly to regional Artifact Registry.
build-push-local: check-project ## Build & push linux/amd64 images locally using Docker Buildx
	gcloud auth configure-docker "$(REGION)-docker.pkg.dev" --quiet
	docker buildx build --platform linux/amd64 \
		-f $(DOCKER_DIR)/Dockerfile.backend \
		-t "$(REGISTRY_URL)/backend:$(IMAGE_TAG)" \
		-t "$(REGISTRY_URL)/backend:latest" \
		--push .
	docker buildx build --platform linux/amd64 \
		-f $(DOCKER_DIR)/Dockerfile.frontend \
		-t "$(REGISTRY_URL)/frontend:$(IMAGE_TAG)" \
		-t "$(REGISTRY_URL)/frontend:latest" \
		--push .

# ==============================================================================
# 6. Terraform Lifecycle (`plan`, `deploy`, `outputs`, `fmt`, `validate`, `destroy`)
# ==============================================================================

# Runs `policy-check` first, then previews all pending infrastructure changes in `infra/terraform/`.
plan: policy-check ## Preview Terraform infrastructure changes
	@if [ "$(TF_MODE)" = "local" ]; then \
		GOOGLE_OAUTH_ACCESS_TOKEN="$$(gcloud auth print-access-token)" \
		terraform -chdir=$(TF_DIR) plan \
			-var="project_id=$(PROJECT_ID)" \
			-var="region=$(REGION)" \
			-var="image_tag=$(IMAGE_TAG)"; \
	else \
		gcloud builds submit $(TF_DIR) \
			--project="$(PROJECT_ID)" \
			--region="$(REGION)" \
			--service-account="projects/$(PROJECT_ID)/serviceAccounts/$(TF_DEPLOYER_SA)" \
			--config=$(CLOUDBUILD_DIR)/run-terraform.yaml \
			--substitutions="_TF_ACTION=plan,_TF_STATE_BUCKET=$(TF_STATE_BUCKET),_STATE_PREFIX=$(STATE_PREFIX),_PROJECT_ID=$(PROJECT_ID),_REGION=$(REGION),_IMAGE_TAG=$(IMAGE_TAG)"; \
	fi

# Runs `policy-check` first, then applies the full Terraform stack (VPC, Private Google Access,
# Regional PSC, Cloud DLP, Model Armor, Cloud Audit Logs, GCS, Identity Platform, Cloud Run,
# and the Global External HTTPS Load Balancer).
deploy: policy-check ## Apply full Terraform stack (GCS, IAM, Identity Platform, Cloud Run services)
	@if [ "$(TF_MODE)" = "local" ]; then \
		GOOGLE_OAUTH_ACCESS_TOKEN="$$(gcloud auth print-access-token)" \
		terraform -chdir=$(TF_DIR) apply \
			-var="project_id=$(PROJECT_ID)" \
			-var="region=$(REGION)" \
			-var="image_tag=$(IMAGE_TAG)" \
			-auto-approve; \
	else \
		gcloud builds submit $(TF_DIR) \
			--project="$(PROJECT_ID)" \
			--region="$(REGION)" \
			--service-account="projects/$(PROJECT_ID)/serviceAccounts/$(TF_DEPLOYER_SA)" \
			--config=$(CLOUDBUILD_DIR)/run-terraform.yaml \
			--substitutions="_TF_ACTION=apply,_TF_STATE_BUCKET=$(TF_STATE_BUCKET),_STATE_PREFIX=$(STATE_PREFIX),_PROJECT_ID=$(PROJECT_ID),_REGION=$(REGION),_IMAGE_TAG=$(IMAGE_TAG)"; \
	fi

# Prints all Terraform outputs defined in `infra/terraform/outputs.tf` (Load Balancer HTTPS URL, static IP,
# internal Cloud Run service URIs, GCS bucket name, Model Armor / DLP template IDs, and audit log IDs).
outputs: check-project ## Print deployed Cloud Run URLs and GCS bucket name
	@if [ "$(TF_MODE)" = "local" ]; then \
		GOOGLE_OAUTH_ACCESS_TOKEN="$$(gcloud auth print-access-token)" \
		terraform -chdir=$(TF_DIR) output; \
	else \
		gcloud builds submit $(TF_DIR) \
			--project="$(PROJECT_ID)" \
			--region="$(REGION)" \
			--service-account="projects/$(PROJECT_ID)/serviceAccounts/$(TF_DEPLOYER_SA)" \
			--config=$(CLOUDBUILD_DIR)/run-terraform.yaml \
			--substitutions="_TF_ACTION=output,_TF_STATE_BUCKET=$(TF_STATE_BUCKET),_STATE_PREFIX=$(STATE_PREFIX),_PROJECT_ID=$(PROJECT_ID),_REGION=$(REGION),_IMAGE_TAG=$(IMAGE_TAG)"; \
	fi

# Formats all `.tf` files in `infra/terraform/` to canonical HCL style.
fmt: ## Format all Terraform files in infra/terraform/
	terraform -chdir=$(TF_DIR) fmt -recursive

# Validates Terraform HCL syntax and internal references in `infra/terraform/` (initializing providers with
# `-backend=false` first if `.terraform/` has not been initialized yet).
validate: check-project ## Validate Terraform syntax
	@if [ "$(TF_MODE)" = "local" ]; then \
		if [ ! -d "$(TF_DIR)/.terraform" ]; then \
			terraform -chdir=$(TF_DIR) init -backend=false; \
		fi; \
		terraform -chdir=$(TF_DIR) validate; \
	else \
		gcloud builds submit $(TF_DIR) \
			--project="$(PROJECT_ID)" \
			--region="$(REGION)" \
			--service-account="projects/$(PROJECT_ID)/serviceAccounts/$(TF_DEPLOYER_SA)" \
			--config=$(CLOUDBUILD_DIR)/run-terraform.yaml \
			--substitutions="_TF_ACTION=validate,_TF_STATE_BUCKET=$(TF_STATE_BUCKET),_STATE_PREFIX=$(STATE_PREFIX),_PROJECT_ID=$(PROJECT_ID),_REGION=$(REGION),_IMAGE_TAG=$(IMAGE_TAG)"; \
	fi

# Tears down Terraform-managed infrastructure.
# Note: In `prod` (`environment = "prod"`), Cloud Run `deletion_protection = true` blocks accidental
# destruction until `environment` is changed or `deletion_protection` is explicitly disabled.
destroy: check-project ## Tear down Terraform-managed GCP resources
	@if [ "$(TF_MODE)" = "local" ]; then \
		GOOGLE_OAUTH_ACCESS_TOKEN="$$(gcloud auth print-access-token)" \
		terraform -chdir=$(TF_DIR) destroy \
			-var="project_id=$(PROJECT_ID)" \
			-var="region=$(REGION)" \
			-var="image_tag=$(IMAGE_TAG)"; \
	else \
		gcloud builds submit $(TF_DIR) \
			--project="$(PROJECT_ID)" \
			--region="$(REGION)" \
			--service-account="projects/$(PROJECT_ID)/serviceAccounts/$(TF_DEPLOYER_SA)" \
			--config=$(CLOUDBUILD_DIR)/run-terraform.yaml \
			--substitutions="_TF_ACTION=destroy,_TF_STATE_BUCKET=$(TF_STATE_BUCKET),_STATE_PREFIX=$(STATE_PREFIX),_PROJECT_ID=$(PROJECT_ID),_REGION=$(REGION),_IMAGE_TAG=$(IMAGE_TAG)"; \
	fi
