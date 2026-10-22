.DEFAULT_GOAL := help

TF_DIR     := infra
TFVARS     := $(TF_DIR)/terraform.tfvars

# Auto-detect PROJECT_ID and REGION from infra/terraform.tfvars if present, else fallback
TFVARS_PROJECT := $(shell [ -f $(TFVARS) ] && sed -n 's/^[[:space:]]*project_id[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' $(TFVARS) | head -n 1)
TFVARS_REGION  := $(shell [ -f $(TFVARS) ] && sed -n 's/^[[:space:]]*region[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' $(TFVARS) | head -n 1)
TFVARS_APP     := $(shell [ -f $(TFVARS) ] && sed -n 's/^[[:space:]]*app_name[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' $(TFVARS) | head -n 1)
TFVARS_ENV     := $(shell [ -f $(TFVARS) ] && sed -n 's/^[[:space:]]*environment[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' $(TFVARS) | head -n 1)

PROJECT_ID ?= $(if $(TFVARS_PROJECT),$(TFVARS_PROJECT),interactive-rag)
REGION     ?= $(if $(TFVARS_REGION),$(TFVARS_REGION),us-central1)
APP_NAME   ?= $(if $(TFVARS_APP),$(TFVARS_APP),interactive-rag)
ENV        ?= $(if $(TFVARS_ENV),$(TFVARS_ENV),prod)
IMAGE_TAG  ?= $(shell git rev-parse --short HEAD 2>/dev/null || echo latest)

TF_STATE_BUCKET ?= $(if $(filter $(PROJECT_ID),$(APP_NAME)),$(PROJECT_ID)-tfstate,$(PROJECT_ID)-$(APP_NAME)-tfstate)
REPO_NAME       := $(APP_NAME)-repo
REGISTRY_URL    := $(REGION)-docker.pkg.dev/$(PROJECT_ID)/$(REPO_NAME)

.PHONY: help check-project bootstrap tf-init build-push build-push-local plan deploy outputs destroy fmt validate

help: ## Show available reproducible build & deployment commands
	@echo "InteractiveRAG — GCP Cloud Run & Terraform Automation"
	@echo ""
	@echo "Resolved configuration:"
	@echo "  PROJECT_ID      = $(PROJECT_ID)"
	@echo "  REGION          = $(REGION)"
	@echo "  APP_NAME        = $(APP_NAME)"
	@echo "  ENV             = $(ENV)"
	@echo "  IMAGE_TAG       = $(IMAGE_TAG)"
	@echo "  TF_STATE_BUCKET = $(TF_STATE_BUCKET)"
	@echo ""
	@echo "Targets:"
	@grep -E '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

check-project:
	@if [ -z "$(PROJECT_ID)" ] || [ "$(PROJECT_ID)" = "your-gcp-project-id" ]; then \
		echo "Error: PROJECT_ID is not set. Populate infra/terraform.tfvars or pass PROJECT_ID=<gcp-project-id>."; \
		exit 1; \
	fi

bootstrap: check-project ## Enable seed APIs, create GCS Terraform state bucket, and create Artifact Registry
	@echo "==> Enabling bootstrap GCP APIs on project $(PROJECT_ID)..."
	gcloud services enable \
		storage.googleapis.com \
		artifactregistry.googleapis.com \
		cloudbuild.googleapis.com \
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
	@$(MAKE) tf-init
	@echo "==> Provisioning Artifact Registry repository ($(REPO_NAME))..."
	terraform -chdir=$(TF_DIR) apply \
		-target=google_artifact_registry_repository.docker_repo \
		-var="project_id=$(PROJECT_ID)" \
		-var="region=$(REGION)" \
		-auto-approve

tf-init: check-project ## Initialize Terraform with remote GCS state backend
	terraform -chdir=$(TF_DIR) init -reconfigure \
		-backend-config="bucket=$(TF_STATE_BUCKET)" \
		-backend-config="prefix=$(APP_NAME)/$(ENV)/state"

build-push: check-project ## Build & push linux/amd64 backend and frontend images via Google Cloud Build
	gcloud builds submit \
		--project="$(PROJECT_ID)" \
		--config=cloudbuild.yaml \
		--substitutions="_REGION=$(REGION),_REPO_NAME=$(REPO_NAME),_IMAGE_TAG=$(IMAGE_TAG)" \
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
	terraform -chdir=$(TF_DIR) plan \
		-var="project_id=$(PROJECT_ID)" \
		-var="region=$(REGION)" \
		-var="image_tag=$(IMAGE_TAG)"

deploy: check-project ## Apply full Terraform stack (GCS, IAM, Identity Platform, Cloud Run services)
	terraform -chdir=$(TF_DIR) apply \
		-var="project_id=$(PROJECT_ID)" \
		-var="region=$(REGION)" \
		-var="image_tag=$(IMAGE_TAG)"

outputs: ## Print deployed Cloud Run URLs and GCS bucket name
	terraform -chdir=$(TF_DIR) output

fmt: ## Format all Terraform files in infra/
	terraform -chdir=$(TF_DIR) fmt -recursive

validate: ## Validate Terraform syntax offline (without remote backend)
	terraform -chdir=$(TF_DIR) init -backend=false
	terraform -chdir=$(TF_DIR) validate

destroy: check-project ## Tear down Terraform-managed GCP resources
	terraform -chdir=$(TF_DIR) destroy \
		-var="project_id=$(PROJECT_ID)" \
		-var="region=$(REGION)" \
		-var="image_tag=$(IMAGE_TAG)"
