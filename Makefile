SHELL := /usr/bin/env bash
.PHONY: setup build images test lint lint-shell lint-yaml deploy port-forward \
        dashboard dashboard-dev dashboard-stop smoke clean status help

IMAGE_NAME ?= demo-app
IMAGE_TAG  ?= 0.2.0
DASH_IMAGE ?= demo-dashboard
VENV       ?= .venv
PY         := $(VENV)/bin/python
DOCKER_COMPOSE ?= docker compose -f compose.yaml -f docker-compose.local.yaml

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

setup: ## Create venv, install deps, extract configs for docker-compose
	python3 -m venv $(VENV)
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -r app/requirements-dev.txt
	$(PY) scripts/extract-configs.py

build: ## Build the app Docker image
	docker build -t $(IMAGE_NAME):$(IMAGE_TAG) app/

images: ## Build both images (multi-stage: app + dashboard)
	docker build -t $(IMAGE_NAME):$(IMAGE_TAG) app/
	docker build -t $(DASH_IMAGE):$(IMAGE_TAG) dashboard/

test: ## Run the test suite (app + dashboard)
	$(PY) -m pytest -q

lint: ## Lint Python with ruff
	$(PY) -m ruff check app/ dashboard/ scripts/extract-configs.py

lint-shell: ## Shellcheck the local (non-cluster) scripts
	shellcheck scripts/start-dashboard.sh scripts/smoke-test.sh

lint-yaml: ## Lint compose files and workflows with yamllint
	$(PY) -m yamllint compose.yaml docker-compose.*.yaml .github/workflows/

deploy: ## Deploy app + observability stack to the current cluster
	./scripts/deploy-all.sh

port-forward: ## Forward local ports to the cluster services
	./scripts/port-forward.sh

dashboard: ## Start the whole local stack + dashboard on http://localhost:9983 (no cluster)
	./scripts/start-dashboard.sh

dashboard-dev: ## Run the dashboard locally (gunicorn) against the compose stack
	./scripts/start-dashboard.sh --dev

dashboard-stop: ## Stop the local docker-compose stack
	$(DOCKER_COMPOSE) down

smoke: ## End-to-end check of the local stack + dashboard
	./scripts/smoke-test.sh

status: ## Show local stack + cluster status
	@$(DOCKER_COMPOSE) ps
	@echo
	@kubectl get ns demo monitoring 2>/dev/null || echo "(no cluster configured)"

clean: ## Remove venv, raw configs, and local artifacts
	rm -rf $(VENV) .local .pytest_cache app/__pycache__ app/tests/__pycache__ \
	       dashboard/__pycache__ dashboard/tests/__pycache__
