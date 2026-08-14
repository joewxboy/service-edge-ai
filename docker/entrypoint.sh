#!/bin/sh
# Starts the Ollama runtime, then the monitor, and forwards shutdown signals
# so Open Horizon can stop the service cleanly.
set -eu

OLLAMA_HOST="${OLLAMA_HOST:-127.0.0.1:11434}"
export OLLAMA_HOST

ollama serve &
OLLAMA_PID=$!

shutdown() {
    echo "entrypoint: forwarding shutdown to service"
    [ -n "${MONITOR_PID:-}" ] && kill -TERM "$MONITOR_PID" 2>/dev/null || true
    [ -n "${MONITOR_PID:-}" ] && wait "$MONITOR_PID" 2>/dev/null || true
    kill -TERM "$OLLAMA_PID" 2>/dev/null || true
    exit 0
}
trap shutdown TERM INT

# Give the runtime a moment to bind before the monitor probes it. The monitor
# tolerates an absent runtime, so this is a courtesy, not a requirement.
i=0
while [ "$i" -lt 30 ]; do
    if curl -fsS "http://${OLLAMA_HOST}/api/tags" >/dev/null 2>&1; then
        echo "entrypoint: ollama is ready"
        break
    fi
    i=$((i + 1))
    sleep 1
done

edge-ai-monitor "$@" &
MONITOR_PID=$!
wait "$MONITOR_PID"
