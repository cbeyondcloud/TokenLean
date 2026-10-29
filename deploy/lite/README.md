# TokenLean LITE

LITE is TokenLean as a **gateway only**. It uses the same code and the same OSS image as the full stack, with the [LITE profile](config/agentic.yaml) applied on top of the shipped config:

- **On:** authentication with per-user proxy keys, choosing the provider from the model name (OpenAI, Anthropic, Gemini, Mistral, Bedrock, Azure, any OpenAI-compatible endpoint), failover, rate limits and spend caps, a `usage_events` row and Prometheus metrics for every call, cost headers, provider prompt caching, and exact-match response caching. The trust and safety checks (PII, prompt injection, context trust, tool eligibility) also run, in `flag` mode, which records findings and never edits or blocks.
- **Teams (one tenant, teams under it):** proxy keys bound to a team and a user, issued, revoked and rotated over HTTP (`/admin/keys`) and kept in Postgres; each team's monthly spend cap enforced (a `429` before any provider call); team limits and routing rules set over HTTP (`/admin/routing`); the `X-Agentic-*` usage tags (run, step, agent, ...) recorded on each usage row. See [Teams and keys](#teams-and-keys).
- **Off:** every optimisation that changes the prompt, the model or the answer: compression, routing/cascade, the semantic cache, the step cache, memory, output and reasoning control, tool pruning, and so on. Also off: G21's request-editing knobs (system-message reordering, cache_control markers; its provider prompt-cache key stays) and the reasoning-model output-budget raise.
- **Refused:** a model the config does not know gets a `400` (`model_not_configured`) instead of being answered by the default model.

What is proven, by `tests/unit/test_agentic_profile.py`: for an OpenAI, an Anthropic, a Gemini and a reasoning model, on a ~35-message request with a repeated turn, a mid-conversation system message, a large JSON tool result and 40 tools, the provider receives the same messages, model and parameters; the only keys added are the credential, timeouts/retries, the provider prompt-cache key and (streams) the usage request; streamed chunks are relayed unchanged. Switching back on any group that acts on such a request is caught by the same comparison; the groups that cannot act on it in a test (they need a sidecar, a registry, a caller opt-in, or never edit) are pinned off by the config checks, each with its reason.

By design, LITE can still answer with a different model in one case: when you configure `resilience.fallbacks` and the requested model fails, the fallback answers (shown in `x-tokenlean-routed-model`). Without fallbacks configured it does not happen.

Turn optimisations on later one at a time, with your own overlay, after an A/B run on your own evaluation harness.

LITE is meant for agent pipelines (such as the agentic SDLC framework) and for teams that want one metered, key-managed endpoint for every LLM provider.

## Layout

```
deploy/lite/
├── README.md
├── config/
│   ├── agentic.yaml            # the LITE profile (byte copy of config/profiles/agentic.yaml)
│   └── langfuse.yaml           # overlay added by --langfuse: send traces to Langfuse
├── grafana/                    # the LITE dashboard set (--grafana), reusable by any Grafana
│   ├── provisioning/           # datasources + dashboard providers (values from the environment)
│   └── prometheus.yml          # scrapes the proxy's /metrics with the scrape token
├── local/                      # laptop / CI
│   ├── docker-compose.lite.yml # proxy + Postgres + Redis; profiles: qdrant, langfuse, mock, grafana
│   ├── .env.lite.template      # copied to .env.lite (git-ignored) by up
│   ├── up.sh    / up.ps1       # create .env.lite + secrets + key file, build, start, wait for /health
│   ├── down.sh  / down.ps1     # stop (volumes kept); --purge / -Purge deletes the volumes
│   ├── smoke.sh / smoke.ps1    # chat + stream + messages, headers, usage rows, a team-bound key
│   └── mock/                   # local OpenAI-compatible stand-in for keyless runs (--mock)
└── cloud/                      # any Kubernetes: the LITE Helm chart and its scripts
    ├── helm/tokenlean-lite/    # the chart: Chart.yaml, values.yaml (documented), templates/, files/
    ├── values/                 # dev.yaml, staging.yaml, prod.yaml, managed.yaml
    ├── install.sh   / .ps1     # helm upgrade --install with values/<env>.yaml, wait for the rollout
    ├── upgrade.sh   / .ps1     # the same, for an installed release only
    ├── uninstall.sh / .ps1     # helm uninstall (Secrets and data kept); --purge / -Purge
    ├── smoke.sh     / .ps1     # the local smoke's checks, against the ingress or a port-forward
    ├── kind/                   # e2e.sh: the whole cycle on a local kind cluster, keyless
    └── tokenlean-lite.env.template  # the proxy container's environment, for other platforms
```

See [Cloud](#cloud-any-kubernetes-helm) for the chart.

## Prerequisites

- A container engine with Compose v2. Any of:
  - **Docker Desktop** (Windows/macOS). It needs a paid subscription for organisations above Docker's free-use limits.
  - **Docker Engine** (Linux).
  - **Podman** with `podman compose` (Windows/macOS/Linux) and the Docker Compose v2 provider installed. The stack uses healthcheck-gated `depends_on`, which the older `podman-compose` handles only partly.

  The scripts call `<engine> compose`. Choose the engine with `CONTAINER_ENGINE=docker` (the default) or `CONTAINER_ENGINE=podman`.
- `curl` (bash scripts; macOS and Linux have it) or PowerShell 5.1+ (Windows) / PowerShell 7 (any OS).
- About 4 GB free disk for the first image build. The build takes about 5 minutes on amd64 and longer on Apple Silicon. The base images are multi-arch, so it builds natively on arm64. Set `TOKENLEAN_IMAGE` to use an image your registry already has instead.

## Quick start

**macOS / Linux (bash or zsh):**

```bash
cd deploy/lite/local
./up.sh --mock       # keyless: every model goes to the local stand-in
./smoke.sh
./down.sh
```

**Windows (PowerShell):**

```powershell
cd deploy\lite\local
powershell -ExecutionPolicy Bypass -File .\up.ps1 -Mock
powershell -ExecutionPolicy Bypass -File .\smoke.ps1
powershell -ExecutionPolicy Bypass -File .\down.ps1
```

(`-ExecutionPolicy Bypass` is only needed where local scripts are blocked. `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` allows them permanently for your user.)

**With real providers:** run `up` once, put your keys in `local/.env.lite` (`LLM_KEY_OPENAI=…`, `LLM_KEY_ANTHROPIC=…`, any you use), then run `up` again without `--mock`. `smoke` then makes three small real calls (a fraction of a cent). It uses `SMOKE_MODEL`, or a model for the first key it finds.

### `up` options

| bash | PowerShell | Effect |
|---|---|---|
| `--mock` | `-Mock` | start the stand-in LLM and route every model to it (no keys, no spend) |
| `--qdrant` | `-Qdrant` | also run Qdrant (only needed once you enable an optimisation that uses it) |
| `--langfuse` | `-Langfuse` | also run Langfuse 2 and send traces to it (UI: `http://127.0.0.1:3110`, login in `.env.lite`) |
| `--grafana` | `-Grafana` | also run Grafana with the LITE dashboards and the Prometheus they read (UI: `http://127.0.0.1:3120`, user `admin`, `GRAFANA_ADMIN_PASSWORD` in `.env.lite`) |
| `--no-build` | `-NoBuild` | reuse the existing proxy image |

`down --purge` / `down.ps1 -Purge` also deletes the Postgres, Redis, Qdrant, Prometheus and Grafana volumes, and with Postgres the keys issued through `/admin/keys`. `.env.lite` is kept, so the next `up` reuses the same keys and copies them into the fresh key store.

## Using it

- Base URL: `http://127.0.0.1:4100` (`LITE_PROXY_PORT`, bound to `LITE_BIND`).
- Key: `TOKENLEAN_API_KEY` from `local/.env.lite`, generated on first `up`. It belongs to tenant `LITE_TENANT_ID`. `TOKENLEAN_ADMIN_KEY` may call the `/admin/*` endpoints.
- OpenAI SDKs: `base_url="http://127.0.0.1:4100/v1"`, `api_key=<TOKENLEAN_API_KEY>`.
- Anthropic SDKs / Claude Code: `ANTHROPIC_BASE_URL=http://127.0.0.1:4100`, with the key as `x-api-key`.
- Gemini: `POST /v1beta/models/{model}:generateContent` with `x-goog-api-key`.
- Every served non-streamed completion carries `x-tokenlean-request-id`, `x-tokenlean-routed-model`, `x-tokenlean-cost-usd` (what the call cost) and the other `x-tokenlean-*` headers (errors do not). A stream sends `x-tokenlean-request-id` with its headers, and every completed stream ends with `: x-tokenlean-cost {"request_id":…,"cost_usd":…}`, an SSE comment that SDKs ignore.
- Usage rows: `POST /admin/usage-export` (your own tenant with a user key, your own team with a team-bound key; any tenant with the admin key). Each row has the call's `team` and its usage `tags`.

## Teams and keys

LITE runs one tenant (`LITE_TENANT_ID`) with teams under it. Proxy keys live in Postgres (`key_store.backend: postgres` in the profile): `up` copies the two keys of `.env.lite` into the key store the first time (an empty table), and every other key is issued through the admin API with `TOKENLEAN_ADMIN_KEY`. Full reference: `docs/config-reference.md`, sections "teams", "/admin/keys" and "/admin/routing".

```bash
ADMIN="Authorization: Bearer $TOKENLEAN_ADMIN_KEY"   # or put the header in a file: curl -H @file
# a key for alice in team payments (the raw key is shown once)
curl -s -X POST -H "$ADMIN" -H 'Content-Type: application/json' \
  -d '{"tenant_id":"LITE-LOC-01","team":"payments","user":"alice@example.com"}' \
  http://127.0.0.1:4100/admin/keys
curl -s -H "$ADMIN" 'http://127.0.0.1:4100/admin/keys?team=payments'      # list (no raw keys)
curl -s -X DELETE -H "$ADMIN" http://127.0.0.1:4100/admin/keys/<key_id>   # revoke
# team payments may spend 50 USD a month (replaces the whole routing overlay; see GET first)
curl -s -X PUT -H "$ADMIN" -H 'Content-Type: application/json' \
  -d '{"fragment":{"tenants":{"LITE-LOC-01":{"teams":{"payments":{"monthly_spend_cap_usd":50}}}}}}' \
  http://127.0.0.1:4100/admin/routing
```

`scripts/issue-key.sh --api http://127.0.0.1:4100` does the same from the command line. A team-bound key's requests are metered under its team (`usage_events.team`; `default` for a key with no team), counted against its team's cap and limits (`rate_limit.per_team`), and its team cannot be changed by a request header. A team over its cap gets `429 team_spend_cap_exceeded` until the month ends or the cap is raised. Keys and routing changes are audited.

## Dashboards

`up --grafana` starts Grafana with the LITE set of TokenLean's dashboards in the folder *TokenLean LITE*: Live Calls, Requests Explorer, Billing, Latency Breakup, SLA, Trust & Safety, Trends and Quarterly, plus Per Call when Langfuse runs too (`--langfuse`). Tenant Overview is left out (one tenant). Every dashboard that reads `usage_events` has a **Team** picker that filters all of those panels (rows from before teams existed count as `default`); the Prometheus panels are not filtered by team. The dashboards are read-only copies of `dashboard/dashboards/`; copy one to change it.

To show the set in a Grafana you already run, mount `grafana/provisioning/datasources/lite.yaml`, `grafana/provisioning/dashboards/lite.yaml` and the eight JSON files (see the `grafana` service in `local/docker-compose.lite.yml`), and set `LITE_DB_HOST`, `LITE_DB_NAME`, `LITE_DB_USER`, `LITE_DB_PASSWORD`, `LITE_DB_SSLMODE` and `LITE_PROMETHEUS_URL` in its environment.

## Cloud: any Kubernetes (Helm)

`cloud/helm/tokenlean-lite` runs the LITE proxy on any Kubernetes 1.29+. It renders the same config as the local stack: the shipped `config.yaml.template` with the LITE profile applied first, carried in a ConfigMap byte for byte. Postgres is always yours (a managed service or one you run). Redis is yours too, except on a dev cluster, where the chart can run one. No cloud vendor's resource is required.

What it deploys: the proxy Deployment (startup, readiness and liveness probes on `/health`; non-root, read-only root filesystem, no capabilities; it serves only once billing, the audit log and the key store are wired), a Service, a PodDisruptionBudget, an optional HorizontalPodAutoscaler, an optional Ingress with TLS (cert-manager annotations, or your own certificate), Prometheus scrape annotations (or a ServiceMonitor), and a migration hook: before each install and upgrade, a Job runs the proxy's own schema steps once (`python -m schema_setup`), so replicas never race to create tables.

### Values files

| File | Replicas | Postgres | Redis | Ingress |
|---|---|---|---|---|
| `values/dev.yaml` | 1 | your dev Postgres, `sslmode=disable` | bundled (dev only, no TLS) | dev host, cert-manager |
| `values/staging.yaml` | HPA 2-4 | managed, `sslmode=require` | managed, TLS | staging host, cert-manager |
| `values/prod.yaml` | HPA 3-10, spread over zones and nodes | managed, `sslmode=verify-full` | managed, TLS | prod host, cert-manager |
| `values/managed.yaml` | 2 | managed, `sslmode=require` | managed, TLS | your certificate |

Each file is layered over the chart's `values.yaml`, which documents every setting. Put your own hosts, Secret names and sizes in a values file of your own and pass it with `--values` / `-Values`; it wins over the environment's file.

### Secrets

Values files name Secrets; they never hold secret values. Create these in the release's namespace from your secret store (External Secrets, Sealed Secrets, the CSI driver) before installing:

| Secret (default name) | Key | What |
|---|---|---|
| `tokenlean-lite-postgres` | `password` | the Postgres user's password (reaches the proxy as `PGPASSWORD`, never inside `DATABASE_URL`) |
| `tokenlean-lite-redis` | `password` | the Redis password (and an optional CA key, `redis.caCertSecretKey`) |
| `tokenlean-lite-bootstrap-keys` | `local-keys.json` | the bootstrap proxy keys, hashes only (`scripts/generate_proxy_key.py`); copied into an empty `proxy_keys` table once |
| `tokenlean-lite-provider-keys` | `LLM_KEY_<PROVIDER>` | one key per provider you use; every key becomes an environment variable |
| `tokenlean-lite-metrics` | `scrape-token` | the bearer token `/metrics` answers to |

On a dev cluster, `install.sh dev --create-secrets` creates the missing ones except Postgres's: random passwords, fresh bootstrap keys (the raw keys go to an owner-only, git-ignored file under `cloud/.state/` for smoke), and the `LLM_KEY_*` variables set in your shell. Values reach `kubectl` in files, never on a command line.

### Install, smoke, upgrade, uninstall

**macOS / Linux (bash or zsh):**

```bash
cd deploy/lite/cloud
./install.sh staging --image registry.example.com/tokenlean-proxy:1.0.0 --values my-staging.yaml
TOKENLEAN_API_KEY=... TOKENLEAN_ADMIN_KEY=... LITE_TENANT_ID=... SMOKE_MODEL=gpt-4o-mini \
  ./smoke.sh --url https://tokenlean-lite.staging.example.internal
./upgrade.sh staging --image registry.example.com/tokenlean-proxy:1.0.1 --values my-staging.yaml
./uninstall.sh
```

**Windows (PowerShell):**

```powershell
cd deploy\lite\cloud
.\install.ps1 staging -Image registry.example.com/tokenlean-proxy:1.0.0 -Values my-staging.yaml
.\smoke.ps1 -Url https://tokenlean-lite.staging.example.internal   # keys and model from $env:
.\upgrade.ps1 staging -Image registry.example.com/tokenlean-proxy:1.0.1 -Values my-staging.yaml
.\uninstall.ps1
```

**A dev cluster, keyless:** `./install.sh dev --image <image> --create-secrets --mock`, then `./smoke.sh` (it port-forwards the Service and finds the keys and the mock model by itself).

- The scripts need `helm` 3.8+ (or 4) and `kubectl`, or `HELM` / `KUBECTL` naming them. Every script takes `--namespace`, `--release` and `--context` (`-Namespace`, `-Release`, `-Context`), and prints the context it acts on.
- `install` and `upgrade` wait for the rollout (`--timeout`, default 10m) and print the pods, events and logs when it fails. A failed upgrade leaves the previous revision serving.
- `smoke` runs `local/smoke.sh` (or `smoke.ps1`): the same checks as the local stack, at the address it is given (`LITE_BASE_URL`, `LITE_ENV_FILE`). Without `--url` it port-forwards the proxy Service to `127.0.0.1:4180`. Through an ingress with a private CA, set `CURL_CA_BUNDLE` (bash) or trust the CA in the system store (PowerShell).
- `uninstall` keeps the namespace, the Secrets and your Postgres data. `--purge` also deletes the Secrets `--create-secrets` made and the bundled Redis's volume.

### Config and overlays

The ConfigMap carries `files/config.yaml.template` and `files/agentic.yaml`, byte copies of the repository's `config/config.yaml.template` and of `deploy/lite/config/agentic.yaml` (helm reads nothing outside a chart). After changing either source, run `python scripts/sync_lite_chart.py`; a test fails while a copy is stale. `config.overlays` lists overlays the chart carries (`deploy/lite/config/langfuse.yaml`); `config.extraOverlays` adds your own, by name and content, applied last in order. Keep secrets out of overlays: use `${VAR}` placeholders and `proxy.extraEnvFrom`.

### Ingress, metrics, migrations

- **Ingress:** set `ingress.host`, `ingress.tlsSecretName` and `ingress.certManager.clusterIssuer` (or `issuer`); leave the issuer empty to bring your own certificate in that Secret. Streamed answers need an unbuffered proxy path and long read timeouts: set your controller's annotations in `ingress.annotations` (examples in `values.yaml`).
- **Metrics:** the pods carry `prometheus.io/scrape|path|port`. `/metrics` answers only to the scrape token, so the scrape job must present it (`authorization.credentials_file`), or set `metrics.serviceMonitor.enabled=true` with the Prometheus Operator: its ServiceMonitor reads the token from the metrics Secret.
- **Migrations:** the hook runs as `postgres.user` by default. To run the proxy as a restricted role (`python -m audit.enforcement`), give the hook the tables' owner: `migrations.user` and `migrations.existingSecret`. `migrations.enabled=false` leaves the schema steps to the proxy's startup, as the local stack does.

### In an umbrella chart

The chart is self-contained (no dependencies, every object named after the release) so another chart can nest it. Its values then sit under the parent's `tokenlean-lite:` key, for example `tokenlean-lite.postgres.host`.

### On a local kind cluster

`cloud/kind/e2e.sh --image <image>` builds nothing; give it a proxy image you built (`docker build -t tokenlean-lite-proxy:1.0.0-dev src/proxy`). It creates a kind cluster, loads the image, starts a throwaway Postgres (`kind/dev-postgres.yaml`), then runs `install.sh dev --mock --create-secrets`, `smoke.sh`, `upgrade.sh`, `smoke.sh` and `uninstall.sh --purge`, and deletes the cluster. It needs Docker only: kind, helm and kubectl run in pinned containers. `TOKENLEAN_LITE_KIND_E2E=1 pytest tests/unit/test_lite_helm_chart.py -k kind` runs it from the suite (with `TOKENLEAN_IMAGE` set).

### Other container platforms

`cloud/tokenlean-lite.env.template` is the proxy container's environment for a managed Postgres (TLS, `sslmode=require`) and Redis (`rediss://`), for any platform that is not Kubernetes. With Postgres keys, every replica sees a key issued or revoked through `/admin/keys` within `key_store.refresh_seconds` (5 s).

## Customising

- **Turn an optimisation on:** write an overlay (only the keys you change), save it under `deploy/lite/config/`, and list it in `.env.lite` as `LITE_EXTRA_OVERLAYS=/app/lite/config/my-overlay.yaml`. Overlays are deep-merged in order (`CONFIG_OVERLAYS`, see `docs/config-reference.md`). A missing overlay stops the proxy from starting; it is never skipped silently.
- **Ports / exposure:** `LITE_PROXY_PORT`, `LITE_LANGFUSE_PORT`, `LITE_BIND` (keep `127.0.0.1` unless the machine is on a private network). Postgres, Redis and Qdrant are never published.
- **Next to the full stack:** the compose project is `tokenlean-lite`, and no container names or ports overlap with `docker-compose.yml` (`token-opt-*`, port 4000).
- **A second LITE stack** (a review checkout, a CI job): set `LITE_PROJECT=<name>` in the environment (not in `.env.lite`) for `up`, `smoke` and `down`, with its own `LITE_PROXY_PORT` / `LITE_GRAFANA_PORT` / `LITE_LANGFUSE_PORT`.

## Troubleshooting

- `up` waits up to `LITE_HEALTH_TIMEOUT` seconds (240) for `/health` to report `ok`, then prints `ps` and the proxy's last log lines.
- `.env.lite not found`: run `up` first; `smoke` and `down` read it.
- A `401` from smoke after editing keys in `.env.lite`: the key store copies the keys of `.env.lite` only into an empty table, so an edited key is not there (`up` warns about this). Issue a new key through `/admin/keys`, or start over with `down --purge` and `up`.
- Windows Git Bash: the scripts stop Git Bash from rewriting the in-container overlay paths (`MSYS2_ENV_CONV_EXCL`); PowerShell needs nothing.
- Windows: if Docker Desktop is not running, `docker compose version` fails and the scripts say so.
- Linux: the proxy container runs as uid 1000 and reads `config/` and `deploy/lite/` through bind mounts, so those files must be world-readable. A checkout made under a restrictive `umask` (e.g. 077) needs `chmod -R a+rX config deploy/lite`. `up` already makes its own `.state/` readable.
