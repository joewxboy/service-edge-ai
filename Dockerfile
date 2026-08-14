# Multi-arch (linux/amd64, linux/arm64) image for the Open Horizon LLM
# workload monitor. Build with buildx:
#   docker buildx build --platform linux/amd64,linux/arm64 -t <image> .

# ---------- stage 1: build the Python wheel ----------
FROM --platform=$BUILDPLATFORM python:3.11-slim AS builder

WORKDIR /build
COPY pyproject.toml README.md LICENSE.md ./
COPY src/ ./src/

RUN pip install --no-cache-dir build \
    && python -m build --wheel --outdir /dist

# ---------- stage 2: runtime ----------
FROM python:3.11-slim

# TARGETARCH is supplied by buildx: "amd64" or "arm64". Ollama publishes
# separate release tarballs per architecture.
ARG TARGETARCH
ARG OLLAMA_VERSION=v0.5.4

LABEL org.opencontainers.image.title="edge-ai-monitor" \
      org.opencontainers.image.description="LLM-based workload monitoring for Open Horizon edge nodes" \
      org.opencontainers.image.source="https://github.com/open-horizon-services/service-edge-ai" \
      org.opencontainers.image.licenses="Apache-2.0"

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl tar \
    && rm -rf /var/lib/apt/lists/*

# Install the Ollama runtime for the target architecture.
RUN set -eux; \
    case "${TARGETARCH}" in \
        amd64) OLLAMA_ARCH=amd64 ;; \
        arm64) OLLAMA_ARCH=arm64 ;; \
        *) echo "unsupported architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /tmp/ollama.tgz \
        "https://github.com/ollama/ollama/releases/download/${OLLAMA_VERSION}/ollama-linux-${OLLAMA_ARCH}.tgz"; \
    tar -C /usr/local -xzf /tmp/ollama.tgz; \
    rm -f /tmp/ollama.tgz; \
    /usr/local/bin/ollama --version || true

COPY --from=builder /dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm -f /tmp/*.whl

COPY config/config.yaml /etc/edge-ai-monitor/config.yaml
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# Proposals are persisted here; mount a volume to keep them across restarts.
RUN mkdir -p /var/lib/monitor/proposals /var/lib/ollama
ENV OLLAMA_MODELS=/var/lib/ollama
VOLUME ["/var/lib/monitor", "/var/lib/ollama"]

# anax binds to 127.0.0.1 only, so the monitor must share the host network
# namespace to reach it. The service definition sets "network": "host"; a
# bridge-networked container cannot reach the agent at any address.
ENV MONITOR_CONFIG=/etc/edge-ai-monitor/config.yaml \
    MONITOR_ANAX_URL=http://localhost:8510 \
    MONITOR_LLM_HOST=http://localhost:11434 \
    MONITOR_HEALTH_PORT=8080 \
    PYTHONUNBUFFERED=1

EXPOSE 8080

# The health endpoint reports 503 until the service loop is running.
HEALTHCHECK --interval=30s --timeout=5s --start-period=180s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8080/health || exit 1

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
