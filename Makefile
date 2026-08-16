SHELL := /usr/bin/env bash
.PHONY: setup build test lint deploy port-forward clean status help

IMAGE_NAME ?= demo-app
IMAGE_TAG  ?= 0.2.0
VENV       ?= .venv
PY         := $(VENV)/bin/python

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

setup: ## Create venv, install deps, extract configs for docker-compose
	python3 -m venv $(VENV)
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -r app/requirements-dev.txt
	$(PY) scripts/extract-configs.py

build: ## Build the app Docker image
	docker build -t $(IMAGE_NAME):$(IMAGE_TAG) app/

test: ## Run the test suite
	$(PY) -m pytest -q

lint: ## Lint Python with ruff
	$(PY) -m ruff check app/ scripts/extract-configs.py

deploy: ## Deploy app + observability stack to the current cluster
	./scripts/deploy-all.sh

port-forward: ## Forward local ports to the cluster services
	./scripts/port-forward.sh

status: ## Show cluster + deployment status
	kubectl get ns demo monitoring
	kubectl get pods -n demo -o wide
	kubectl get pods -n monitoring -o wide
	kubectl get svc -n demo
	kubectl get svc -n monitoring

clean: ## Remove venv, raw configs, and local artifacts
	rm -rf $(VENV) .local .pytest_cache app/__pycache__ app/tests/__pycache__
