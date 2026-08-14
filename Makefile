# Multi-arch (amd64, arm64) container for Open Horizon Linux edge nodes.
# Built from the Dockerfile in this repo; see `make build` / `make push-multiarch`.
export DOCKER_IMAGE_BASE ?= joewxboy/edge-ai
export DOCKER_IMAGE_NAME ?= edge-ai
export DOCKER_IMAGE_VERSION ?= 0.0.6
export DOCKER_VOLUME_NAME ?= edge-ai-storage
# DockerHub ID of the third party providing the image (usually yours if building and pushing)
export DOCKER_HUB_ID ?= joewxboy
# The Open Horizon organization ID namespace where you will be publishing the service definition file
export HZN_ORG_ID ?= examples
# Open Horizon settings for publishing metadata about the service
export DEPLOYMENT_POLICY_NAME ?= deployment-policy-edge-ai
export NODE_POLICY_NAME ?= node-policy-edge-ai
export SERVICE_NAME ?= service-edge-ai
export SERVICE_VERSION ?= 0.0.7
# Default ARCH to the architecture of this machine (assumes hzn CLI installed)
export ARCH ?= amd64
# Detect Operating System running Make
OS := $(shell uname -s)

default: init run browse

check:
	@echo "====================="
	@echo "ENVIRONMENT VARIABLES"
	@echo "====================="
	@echo "DOCKER_IMAGE_BASE    default: joewxboy/edge-ai          actual: ${DOCKER_IMAGE_BASE}"
	@echo "DOCKER_IMAGE_NAME    default: edge-ai                    actual: ${DOCKER_IMAGE_NAME}"
	@echo "DOCKER_IMAGE_VERSION default: 0.0.6                      actual: ${DOCKER_IMAGE_VERSION}"
	@echo "DOCKER_VOLUME_NAME   default: edge-ai-storage            actual: ${DOCKER_VOLUME_NAME}"
	@echo "DOCKER_HUB_ID        default: joewxboy                   actual: ${DOCKER_HUB_ID}"
	@echo "HZN_ORG_ID           default: examples                   actual: ${HZN_ORG_ID}"
	@echo "DEPLOYMENT_POLICY_NAME default: deployment-policy-edge-ai actual: ${DEPLOYMENT_POLICY_NAME}"
	@echo "NODE_POLICY_NAME     default: node-policy-edge-ai        actual: ${NODE_POLICY_NAME}"
	@echo "SERVICE_NAME         default: service-edge-ai            actual: ${SERVICE_NAME}"
	@echo "SERVICE_VERSION      default: 0.0.7                      actual: ${SERVICE_VERSION}"
	@echo "ARCH                 default: amd64                      actual: ${ARCH}"
	@echo ""
	@echo "=================="
	@echo "SERVICE DEFINITION"
	@echo "=================="
	@cat horizon/service.definition.json | envsubst
	@echo ""

stop:
	@docker rm -f $(DOCKER_IMAGE_NAME) >/dev/null 2>&1 || :

init:
	@docker volume create $(DOCKER_VOLUME_NAME)

run: stop
	@docker run -d \
		--name $(DOCKER_IMAGE_NAME) \
		--restart=unless-stopped \
		--network host \
		-v $(DOCKER_VOLUME_NAME):/var/lib/monitor \
		-v edge-ai-models:/var/lib/ollama \
		-v /var/log/workloads:/var/log/workloads:ro \
		$(DOCKER_IMAGE_BASE):$(DOCKER_IMAGE_VERSION)

dev: run attach

attach:
	@docker exec -it \
		`docker ps -aqf "name=$(DOCKER_IMAGE_NAME)"` \
		/bin/bash

test:
	@curl -sS http://127.0.0.1:8080/health

browse:
ifeq ($(OS),Darwin)
	@open http://127.0.0.1:8080
else
	@xdg-open http://127.0.0.1:8080
endif

clean: stop
	@docker rmi -f $(DOCKER_IMAGE_BASE):$(DOCKER_IMAGE_VERSION) >/dev/null 2>&1 || :
	@docker volume rm $(DOCKER_VOLUME_NAME)

distclean: agent-stop remove-deployment-policy remove-service-policy remove-service clean

build:
	@echo "=================="
	@echo "BUILDING IMAGE"
	@echo "=================="
	@docker build -t $(DOCKER_IMAGE_BASE):$(DOCKER_IMAGE_VERSION) .

# Multi-arch build requires the buildx plugin and a configured builder:
#   docker buildx create --use --name edge-ai-builder
buildx:
	@echo "=========================="
	@echo "BUILDING MULTI-ARCH IMAGE"
	@echo "=========================="
	@docker buildx build --platform linux/amd64,linux/arm64 \
		-t $(DOCKER_IMAGE_BASE):$(DOCKER_IMAGE_VERSION) .

push:
	@echo "=================="
	@echo "PUSHING IMAGE"
	@echo "=================="
	@docker push $(DOCKER_IMAGE_BASE):$(DOCKER_IMAGE_VERSION)

# Builds and pushes the multi-arch manifest in one step.
push-multiarch:
	@echo "==========================="
	@echo "PUSHING MULTI-ARCH MANIFEST"
	@echo "==========================="
	@docker buildx build --platform linux/amd64,linux/arm64 \
		-t $(DOCKER_IMAGE_BASE):$(DOCKER_IMAGE_VERSION) --push .

unittest:
	@python3 -m pytest tests/ -v

publish: publish-service publish-service-policy publish-deployment-policy agent-run browse

publish-service:
	@echo "=================="
	@echo "PUBLISHING SERVICE"
	@echo "=================="
	@hzn exchange service publish -O -P --json-file=horizon/service.definition.json
	@echo ""

remove-service:
	@echo "=================="
	@echo "REMOVING SERVICE"
	@echo "=================="
	@hzn exchange service remove -f $(HZN_ORG_ID)/$(SERVICE_NAME)_$(SERVICE_VERSION)_$(ARCH)
	@echo ""

publish-service-policy:
	@echo "========================="
	@echo "PUBLISHING SERVICE POLICY"
	@echo "========================="
	@hzn exchange service addpolicy -f horizon/service.policy.json $(HZN_ORG_ID)/$(SERVICE_NAME)_$(SERVICE_VERSION)_$(ARCH)
	@echo ""

remove-service-policy:
	@echo "======================="
	@echo "REMOVING SERVICE POLICY"
	@echo "======================="
	@hzn exchange service removepolicy -f $(HZN_ORG_ID)/$(SERVICE_NAME)_$(SERVICE_VERSION)_$(ARCH)
	@echo ""

publish-deployment-policy:
	@echo "============================"
	@echo "PUBLISHING DEPLOYMENT POLICY"
	@echo "============================"
	@hzn exchange deployment addpolicy -f horizon/deployment.policy.json $(HZN_ORG_ID)/policy-$(SERVICE_NAME)_$(SERVICE_VERSION)
	@echo ""

remove-deployment-policy:
	@echo "=========================="
	@echo "REMOVING DEPLOYMENT POLICY"
	@echo "=========================="
	@hzn exchange deployment removepolicy -f $(HZN_ORG_ID)/policy-$(SERVICE_NAME)_$(SERVICE_VERSION)
	@echo ""

agent-run:
	@echo "================"
	@echo "REGISTERING NODE"
	@echo "================"
	@hzn register --policy=horizon/node.policy.json
	@watch hzn agreement list

agent-stop:
	@echo "==================="
	@echo "UN-REGISTERING NODE"
	@echo "==================="
	@hzn unregister -f
	@echo ""

deploy-check:
	@hzn deploycheck all -t device -B horizon/deployment.policy.json --service=horizon/service.definition.json --service-pol=horizon/service.policy.json --node-pol=horizon/node.policy.json

log:
	@echo "========="
	@echo "EVENT LOG"
	@echo "========="
	@hzn eventlog list
	@echo ""
	@echo "==========="
	@echo "SERVICE LOG"
	@echo "==========="
	@hzn service log -f $(SERVICE_NAME)

.PHONY: default stop init run dev test unittest clean build buildx push push-multiarch attach browse publish publish-service publish-service-policy publish-deployment-policy publish-pattern agent-run distclean deploy-check check log remove-deployment-policy remove-service-policy remove-service
