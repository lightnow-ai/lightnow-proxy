.PHONY: info install lint test package push run

BUILD_NUMBER ?= local
GIT_BRANCH ?= $(shell git rev-parse --abbrev-ref HEAD 2>/dev/null || echo local)
DOCKER_REPO_URI ?= lightnow-ai/lightnow-proxy
DOCKER_TAG ?= local
DOCKER_BUILD_FLAGS ?=

info:
	@echo "project=lightnow-proxy"
	@echo "branch=$(GIT_BRANCH)"
	@echo "build=$(BUILD_NUMBER)"
	@echo "tag=$(DOCKER_TAG)"

install:
	uv pip install -e .[dev]

lint:
	uv run --extra dev ruff check src tests examples

test:
	uv run --extra dev pytest -q

package:
	docker build $(DOCKER_BUILD_FLAGS) -t $(DOCKER_REPO_URI):$(DOCKER_TAG) .

push:
	docker push $(DOCKER_REPO_URI):$(DOCKER_TAG)

run:
	LIGHTNOW_PROXY_CONFIG=config.example.yaml uv run lightnow-proxy
