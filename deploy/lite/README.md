# TokenLean LITE

LITE is TokenLean as a **gateway only**. It uses the same code and the same OSS image as the full stack, with the [LITE profile](config/agentic.yaml) applied on top of the shipped config:

- **On:** authentication with per-user proxy keys, choosing the provider from the model name (OpenAI, Anthropic, Gemini, Mistral, Bedrock, Azure, any OpenAI-compatible endpoint), failover, rate limits and spend caps, a `usage_events` row and Prometheus metrics for every call, cost headers, provider prompt caching, and exact-match response caching. The trust and safety checks (PII, prompt injection, context trust, tool eligibility) also run, in `flag` mode, which records findings and never edits or blocks.
- **Off:** every optimisation that changes the prompt, the model or the answer: compression, routing/cascade, the semantic cache, the step cache, memory, output and reasoning control, tool pruning, and so on.

The provider receives exactly the request the client sent (a test pins this: `tests/unit/test_agentic_profile.py`). Turn optimisations on later one at a time, with your own overlay, after an A/B run on your own evaluation harness.

LITE is meant for agent pipelines (such as the agentic SDLC framework) and for teams that want one metered, key-managed endpoint for every LLM provider.

## Layout

```
deploy/lite/
├── README.md
├── config/
│   ├── agentic.yaml            # the LITE profile (byte copy of config/profiles/agentic.yaml)
│   └── langfuse.yaml           # overlay added by --langfuse: send traces to Langfuse
└── local/                      # laptop / CI
    ├── docker-compose.lite.yml # proxy + Postgres + Redis; profiles: qdrant, langfuse, mock
    ├── .env.lite.template      # copied to .env.lite (git-ignored) by up
    ├── up.sh    / up.ps1       # create .env.lite + secrets + key file, build, start, wait for /health
    ├── down.sh  / down.ps1     # stop (volumes kept); --purge / -Purge deletes the volumes
    ├── smoke.sh / smoke.ps1    # chat + streamed chat + messages, x-tokenlean-* headers, usage rows
    └── mock/                   # local OpenAI-compatible stand-in for keyless runs (--mock)
```

The cloud part (Helm chart and install scripts for any Kubernetes) is planned for later and is not here yet.

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
| `--no-build` | `-NoBuild` | reuse the existing proxy image |

`down --purge` / `down.ps1 -Purge` also deletes the Postgres, Redis and Qdrant volumes. `.env.lite` is kept, so the next `up` reuses the same keys.

## Using it

- Base URL: `http://127.0.0.1:4100` (`LITE_PROXY_PORT`, bound to `LITE_BIND`).
- Key: `TOKENLEAN_API_KEY` from `local/.env.lite`, generated on first `up`. It belongs to tenant `LITE_TENANT_ID`. `TOKENLEAN_ADMIN_KEY` may call the `/admin/*` endpoints.
- OpenAI SDKs: `base_url="http://127.0.0.1:4100/v1"`, `api_key=<TOKENLEAN_API_KEY>`.
- Anthropic SDKs / Claude Code: `ANTHROPIC_BASE_URL=http://127.0.0.1:4100`, with the key as `x-api-key`.
- Gemini: `POST /v1beta/models/{model}:generateContent` with `x-goog-api-key`.
- Every non-streamed response carries `x-tokenlean-request-id`, `x-tokenlean-routed-model`, `x-tokenlean-cost-usd` (what the call cost) and the other `x-tokenlean-*` headers. Every completed stream ends with `: x-tokenlean-cost {"request_id":…,"cost_usd":…}`, an SSE comment that SDKs ignore.
- Usage rows: `POST /admin/usage-export` (your own tenant with a user key; any tenant with the admin key).

## Customising

- **Turn an optimisation on:** write an overlay (only the keys you change), save it under `deploy/lite/config/`, and list it in `.env.lite` as `LITE_EXTRA_OVERLAYS=/app/lite/config/my-overlay.yaml`. Overlays are deep-merged in order (`CONFIG_OVERLAYS`, see `docs/config-reference.md`). A missing overlay stops the proxy from starting; it is never skipped silently.
- **Ports / exposure:** `LITE_PROXY_PORT`, `LITE_LANGFUSE_PORT`, `LITE_BIND` (keep `127.0.0.1` unless the machine is on a private network). Postgres, Redis and Qdrant are never published.
- **Next to the full stack:** the compose project is `tokenlean-lite`, and no container names or ports overlap with `docker-compose.yml` (`token-opt-*`, port 4000).

## Troubleshooting

- `up` waits up to `LITE_HEALTH_TIMEOUT` seconds (240) for `/health` to report `ok`, then prints `ps` and the proxy's last log lines.
- `.env.lite not found`: run `up` first; `smoke` and `down` read it.
- A `401` from smoke after editing keys: run `up` again. It rewrites `.state/local-keys.json` from `.env.lite`.
- Windows: if Docker Desktop is not running, `docker compose version` fails and the scripts say so.
- Linux: the proxy container runs as uid 1000 and reads `config/` and `deploy/lite/` through bind mounts, so those files must be world-readable. A checkout made under a restrictive `umask` (e.g. 077) needs `chmod -R a+rX config deploy/lite`. `up` already makes its own `.state/` readable.
