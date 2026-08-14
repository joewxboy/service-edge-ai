# Deploying the monitor with Open Horizon

## Prerequisites

- An Open Horizon management hub you can publish to, and credentials exported
  (`HZN_ORG_ID`, `HZN_EXCHANGE_USER_AUTH`).
- An edge node with the anax agent installed and able to reach the hub.
- **At least 4 GB of RAM on the edge node.** Local inference with a 3B
  quantized model needs roughly 2 GB on top of the runtime.
- A container registry you can push to, if you are building your own image.

## 1. Build and push the image

The image is multi-arch (`linux/amd64`, `linux/arm64`) and bundles both the
Ollama runtime and the monitor service.

```shell
# One-time: create a buildx builder capable of cross-platform builds
docker buildx create --use --name edge-ai-builder

export DOCKER_IMAGE_BASE=<your-registry>/edge-ai
export DOCKER_IMAGE_VERSION=0.0.1

make push-multiarch
```

To build only for the local architecture while developing:

```shell
make build
```

## 2. Publish the service and policies

```shell
export HZN_ORG_ID=<your-org>
export SERVICE_NAME=service-edge-ai
export SERVICE_VERSION=0.0.1
export ARCH=amd64          # or arm64

make publish
```

`make publish` runs, in order:

| Target | Effect |
|---|---|
| `publish-service` | Publishes `horizon/service.definition.json` to the exchange |
| `publish-service-policy` | Publishes `horizon/service.policy.json` |
| `publish-deployment-policy` | Publishes `horizon/deployment.policy.json` |
| `agent-run` | Registers the node with `horizon/node.policy.json` |

Check compatibility before registering:

```shell
make deploy-check
```

> ⚠️ **Keep the image version and the service version in step.** They are
> independent: `SERVICE_VERSION` names the exchange entry, `DOCKER_IMAGE_VERSION`
> names the image it points at. Publishing a new service version against an old
> image tag deploys old code with a new version number, and the symptom is
> subtle — a feature you just added is simply absent at runtime. Verify what
> actually shipped:
>
> ```shell
> hzn exchange service list -l myorg/service-edge-ai_<version>_amd64 \
>   | jq -r '.[].deployment' | jq -r '.services[].image'
> ```

## 3. Node requirements

The deployment policy requires the node to advertise `monitoring == true` and
have at least 4096 MB of memory. `horizon/node.policy.json` sets the property:

```json
{ "properties": [{ "name": "monitoring", "value": true }] }
```

A node that does not advertise this property will never form an agreement for
the monitor — which is intentional, so the monitor is not deployed to nodes that
have not opted in.

## 4. Prepare the context directory (optional but recommended)

Node-wide guidance — what hostnames really resolve to, which failures self-heal,
which component to restart — makes analyses specific instead of generic. See
[context-and-steering.md](context-and-steering.md) for a measured before/after.

```shell
sudo mkdir -p /etc/edge-ai-monitor/context
sudo tee /etc/edge-ai-monitor/context/10-node.md <<'EOF'
# This node

Rack 4, factory floor B. Intermittent WiFi — network timeouts are usually
transient and self-heal within 2 minutes.
EOF
```

The shipped service definition already mounts this directory read-only, so no
further change is needed. Confirm it loaded:

```shell
curl -sS http://127.0.0.1:8080/health | jq '{node_context_documents, node_context_bytes}'
```

`node_context_documents: 0` with files present means the bind is missing;
the field being **absent entirely** means the running image predates the
context feature.

## 5. Prepare the shared log directory

Create the directory the monitor reads from, before the first workload starts:

```shell
sudo mkdir -p /var/log/workloads
sudo chmod 755 /var/log/workloads
```

Each monitored workload writes into its own subdirectory. See
[monitoring-configuration.md](monitoring-configuration.md) for the mount
details each workload needs.

## 6. Verify

```shell
# Agreement formed?
hzn agreement list

# Service running and healthy?
curl -sS http://127.0.0.1:8080/health | jq

# Service logs
make log
```

A healthy response looks like:

```json
{
  "healthy": true,
  "startup_errors": [],
  "workloads_discovered": 3,
  "workloads_monitored": 1,
  "log_paths_watched": 2,
  "analysis_queue_depth": 0,
  "analyses_completed": 4,
  "proposals_written": 4
}
```

## 7. Read the proposals

Proposals are written inside the container to
`/var/lib/monitor/proposals/<workload>/<timestamp>.json`, backed by the
`edge-ai-monitor-state` volume:

```shell
docker run --rm -v edge-ai-monitor-state:/state busybox \
  find /state/proposals -name '*.json'
```

## Local testing without a hub

`docker-compose.yml` brings up the monitor alongside a sample workload that
emits errors on a timer:

```shell
docker compose up --build
curl -sS http://127.0.0.1:8080/health | jq
```

## Configuration at deploy time

Every setting is overridable through service definition `userInput` or plain
environment variables. See the table in the
[README](../README.md#configuration).

## Rollback

The monitor holds no state that other workloads depend on, so removing it is
safe at any time:

```shell
make agent-stop                     # unregister the node
make remove-deployment-policy
make remove-service-policy
make remove-service
```

Monitored workloads keep running normally with the monitor absent.
