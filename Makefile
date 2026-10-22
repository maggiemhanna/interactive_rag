.DEFAULT_GOAL := help

TF_DIR     := infra
TFVARS     := $(TF_DIR)/terraform.tfvars

# Auto-detect PROJECT_ID and REGION from infra/terraform.tfvars if present, else fallback
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

TF_STATE_BUCKET ?= $(if $(PROJECT_NUMBER),$(APP_NAME)-tfstate-$(PROJECT_NUMBER),$(PROJECT_ID)-tfstate)
STATE_PREFIX    := $(APP_NAME)/$(ENV)/state
REPO_NAME       := $(APP_NAME)-repo
REGISTRY_URL    := $(REGION)-docker.pkg.dev/$(PROJECT_ID)/$(REPO_NAME)

TF_DEPLOYER_ID  := $(APP_NAME)-deployer
TF_DEPLOYER_SA  := $(TF_DEPLOYER_ID)@$(PROJECT_ID).iam.gserviceaccount.com

# Auto-detect whether local `terraform` binary is installed and executable; otherwise run Terraform via Cloud Build
HAS_LOCAL_TF := $(shell command -v terraform >/dev/null 2>&1 && terraform -version >/dev/null 2>&1 && echo yes || echo no)
TF_MODE      ?= $(if $(filter yes,$(HAS_LOCAL_TF)),local,cloudbuild)

TF_DEPLOYER_ROLES := \
	roles/run.admin \
	roles/storage.admin \
	roles/artifactregistry.admin \
	roles/iam.serviceAccountAdmin \
	roles/iam.serviceAccountUser \
	roles/resourcemanager.projectIamAdmin \
	roles/serviceusage.serviceUsageAdmin \
	roles/serviceusage.apiKeysAdmin \
	roles/identityplatform.admin \
	roles/secretmanager.secretAccessor \
	roles/secretmanager.viewer \
	roles/browser \
	roles/logging.logWriter

.PHONY: help check-project bootstrap ensure-deployer-sa tf-init build-push build-push-local plan deploy outputs destroy fmt validate

help: ## Show available reproducible build & deployment commands
	@echo "InteractiveRAG — GCP Cloud Run & Terraform Automation"
	@echo ""
	@echo "Resolved configuration:"
	@echo "  PROJECT_ID      = $(PROJECT_ID) (number: $(PROJECT_NUMBER))"
	@echo "  REGION          = $(REGION)"
	@echo "  APP_NAME        = $(APP_NAME)"
	@echo "  ENV             = $(ENV)"
	@echo "  IMAGE_TAG       = $(IMAGE_TAG)"
	@echo "  TF_STATE_BUCKET = $(TF_STATE_BUCKET)"
	@echo "  TF_MODE         = $(TF_MODE)"
	@echo ""
	@echo "Targets:"
	@grep -E '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

check-project:
	@if [ -z "$(PROJECT_ID)" ] || [ "$(PROJECT_ID)" = "your-gcp-project-id" ]; then \
		echo "Error: PROJECT_ID is not set. Populate infra/terraform.tfvars or pass PROJECT_ID=<gcp-project-id>."; \
		exit 1; \
	fi

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

bootstrap: check-project ## Enable seed APIs, create GCS Terraform state bucket, and create Artifact Registry
	@echo "==> Enabling required GCP APIs on project $(PROJECT_ID)..."
	gcloud services enable \
		storage.googleapis.com \
		artifactregistry.googleapis.com \
		cloudbuild.googleapis.com \
		iam.googleapis.com \
		cloudresourcemanager.googleapis.com \
		serviceusage.googleapis.com \
		secretmanager.googleapis.com \
		identitytoolkit.googleapis.com \
		apikeys.googleapis.com \
		run.googleapis.com \
		aiplatform.googleapis.com \
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
			-auto-approve; \
	else \
		gcloud builds submit $(TF_DIR) \
			--project="$(PROJECT_ID)" \
			--region="$(REGION)" \
			--service-account="projects/$(PROJECT_ID)/serviceAccounts/$(TF_DEPLOYER_SA)" \
			--config=$(TF_DIR)/cloudbuild-terraform.yaml \
			--substitutions="_TF_ACTION=apply,_TF_STATE_BUCKET=$(TF_STATE_BUCKET),_STATE_PREFIX=$(STATE_PREFIX),_PROJECT_ID=$(PROJECT_ID),_REGION=$(REGION),_IMAGE_TAG=$(IMAGE_TAG),_TF_TARGET=google_artifact_registry_repository.docker_repo"; \
	fi

tf-init: check-project ## Initialize local Terraform with remote GCS state backend
	GOOGLE_OAUTH_ACCESS_TOKEN="$$(gcloud auth print-access-token)" \
	terraform -chdir=$(TF_DIR) init -reconfigure \
		-backend-config="bucket=$(TF_STATE_BUCKET)" \
		-backend-config="prefix=$(STATE_PREFIX)"

build-push: check-project ## Build & push linux/amd64 backend and frontend images via Google Cloud Build
	gcloud builds submit \
		--project="$(PROJECT_ID)" \
		--region="$(REGION)" \
		--service-account="projects/$(PROJECT_ID)/serviceAccounts/$(TF_DEPLOYER_SA)" \
		--ignore-file=.dockerignore \
		--config=cloudbuild.yaml \
		--substitutions="_REGION=$(REGION),_REPO_NAME=$(REPO_NAME),_IMAGE_TAG=$(IMAGE_TAG),_LOGS_BUCKET=$(TF_STATE_BUCKET)" \
		.

build-push-local: check-project ## Build & push linux/amd64 images locally using Docker Buildx
	gcloud auth configure-docker "$(REGION)-docker.pkg.dev" --quiet
	docker buildx build --platform linux/amd64 \
		-f docker/Dockerfile.backend \
		-t "$(REGISTRY_URL)/backend:$(IMAGE_TAG)" \
		-t "$(REGISTRY_URL)/backend:latest" \
		--push .
	docker buildx build --platform linux/amd64 \
		-f docker/Dockerfile.frontend \
		-t "$(REGISTRY_URL)/frontend:$(IMAGE_TAG)" \
		-t "$(REGISTRY_URL)/frontend:latest" \
		--push .

plan: check-project ## Preview Terraform infrastructure changes
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
			--config=$(TF_DIR)/cloudbuild-terraform.yaml \
			--substitutions="_TF_ACTION=plan,_TF_STATE_BUCKET=$(TF_STATE_BUCKET),_STATE_PREFIX=$(STATE_PREFIX),_PROJECT_ID=$(PROJECT_ID),_REGION=$(REGION),_IMAGE_TAG=$(IMAGE_TAG)"; \
	fi

deploy: check-project ## Apply full Terraform stack (GCS, IAM, Identity Platform, Cloud Run services)
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
			--config=$(TF_DIR)/cloudbuild-terraform.yaml \
			--substitutions="_TF_ACTION=apply,_TF_STATE_BUCKET=$(TF_STATE_BUCKET),_STATE_PREFIX=$(STATE_PREFIX),_PROJECT_ID=$(PROJECT_ID),_REGION=$(REGION),_IMAGE_TAG=$(IMAGE_TAG)"; \
	fi

outputs: check-project ## Print deployed Cloud Run URLs and GCS bucket name
	@if [ "$(TF_MODE)" = "local" ]; then \
		GOOGLE_OAUTH_ACCESS_TOKEN="$$(gcloud auth print-access-token)" \
		terraform -chdir=$(TF_DIR) output; \
	else \
		gcloud builds submit $(TF_DIR) \
			--project="$(PROJECT_ID)" \
			--region="$(REGION)" \
			--service-account="projects/$(PROJECT_ID)/serviceAccounts/$(TF_DEPLOYER_SA)" \
			--config=$(TF_DIR)/cloudbuild-terraform.yaml \
			--substitutions="_TF_ACTION=output,_TF_STATE_BUCKET=$(TF_STATE_BUCKET),_STATE_PREFIX=$(STATE_PREFIX),_PROJECT_ID=$(PROJECT_ID),_REGION=$(REGION),_IMAGE_TAG=$(IMAGE_TAG)"; \
	fi

fmt: ## Format all Terraform files in infra/
	terraform -chdir=$(TF_DIR) fmt -recursive

validate: check-project ## Validate Terraform syntax
	@if [ "$(TF_MODE)" = "local" ]; then \
		terraform -chdir=$(TF_DIR) init -backend=false && terraform -chdir=$(TF_DIR) validate; \
	else \
		gcloud builds submit $(TF_DIR) \
			--project="$(PROJECT_ID)" \
			--region="$(REGION)" \
			--service-account="projects/$(PROJECT_ID)/serviceAccounts/$(TF_DEPLOYER_SA)" \
			--config=$(TF_DIR)/cloudbuild-terraform.yaml \
			--substitutions="_TF_ACTION=validate,_TF_STATE_BUCKET=$(TF_STATE_BUCKET),_STATE_PREFIX=$(STATE_PREFIX),_PROJECT_ID=$(PROJECT_ID),_REGION=$(REGION),_IMAGE_TAG=$(IMAGE_TAG)"; \
	fi

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
			--config=$(TF_DIR)/cloudbuild-terraform.yaml \
			--substitutions="_TF_ACTION=destroy,_TF_STATE_BUCKET=$(TF_STATE_BUCKET),_STATE_PREFIX=$(STATE_PREFIX),_PROJECT_ID=$(PROJECT_ID),_REGION=$(REGION),_IMAGE_TAG=$(IMAGE_TAG)"; \
	fi
