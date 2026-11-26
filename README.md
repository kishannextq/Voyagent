# Voyagent

Multi-agent AI travel planner. A LangGraph supervisor sends each request to specialist agents
(flights, hotels, weather, budget, itinerary), pauses for human approval, and streams progress
to the browser over Server-Sent Events.

This branch line is the monorepo that evolves Voyagent into a multi-tenant, observable,
AWS-hosted platform. Status: **built and tested in CI, not deployed, not production-ready**.
See [docs/PRODUCTION_READINESS.md](docs/PRODUCTION_READINESS.md) for the evidence and the
remaining blockers, and [docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md) for the
live checklist.

## Architecture

```
Browser ──HTTPS + SSE──► apps/web (Next.js 15, Cognito PKCE sign-in)
                            │
                            ▼
                     api (FastAPI, public)  ── auth, rate limits, input guardrails, uploads
                            │ internal token
                            ▼
                     orchestrator  ── LangGraph graph, checkpoints, approval interrupt, SSE
                       │        │
          in-process or│        │ SQS task ──► agent-<name> workers (one service per agent)
          remote agents│        │ ◄── Redis reply        scale on queue depth
                       ▼        ▼
   model gateway (LiteLLM) · MCP tools (AviationStack, OpenWeather, Tavily)
   guard (injection classifier) · ClamAV · Postgres + pgvector · Redis
```

- **Agents as services**: any agent listed in `REMOTE_AGENTS` runs in its own worker
  (`agent_worker.py`). The orchestrator puts a task on that agent's SQS queue and waits for the
  result on Redis. Each worker has bounded concurrency, drains on shutdown, and gets only the
  credentials its tools need. `human_approval` and `final_response` stay in the orchestrator.
- **Model access** goes only through the gateway (fallback, alias routing, per-user budgets).
  Provider keys are server-side only and never reach the browser.
- **Document search (RAG)**: tenant-isolated pgvector store with hybrid retrieval, fed by
  `ingest_worker.py`; uploads are scanned by ClamAV before ingestion.
- **Guardrails**: rule and model-based prompt-injection checks, tool allow-lists, per-agent and
  per-run budgets (time, model calls), retrieved text treated as data, never as instructions.

Details: [ARCHITECTURE](docs/ARCHITECTURE.md), [MICROSERVICES](docs/MICROSERVICES.md).

## Layout

```
apps/web/                  Next.js 15 + React 19 + Tailwind UI
services/api/              FastAPI api, orchestrator, agents, agent bus + worker, RAG, guardrails
services/model-gateway/    LiteLLM config and virtual-key bootstrap
services/guard/            Prompt-injection classifier service (ONNX)
infra/terraform/           AWS: VPC, ECS Fargate, RDS, ElastiCache, SQS, Cognito, WAF, …
infra/deploy/              Rolling deploy script (agent services before the orchestrator)
infra/observability/       OpenTelemetry collector, Prometheus, Loki, Tempo, Grafana
infra/elasticmq/           Local SQS queues (one per agent, each with a dead-letter queue)
infra/resilience/          Failure drills and the agent scale-out / durability check
tests/e2e, tests/load/     Playwright end-to-end tests, k6 load tests
tools/                     Mock LLM and mock OIDC provider for local and CI runs
docker-compose.yml         Local stack (profiles: gateway, safety, observability, tools)
docker-compose.agents.yml  Override: every agent as its own service on local SQS
.github/workflows/         CI: lint, types, tests, e2e, load, resilience, agents, infra scan, images
```

## Quick start

```bash
cp .env.example .env        # MOCK_MODE=true: full flow with canned responses, no keys needed
docker compose up --build
```

- Web: http://localhost:3000
- API docs: http://localhost:8000/docs

Optional parts of the stack:

```bash
# Each agent as its own service (ElasticMQ stands in for SQS)
docker compose -f docker-compose.yml -f docker-compose.agents.yml up --build
docker compose -f docker-compose.yml -f docker-compose.agents.yml up -d --scale agent-weather=3

docker compose --profile gateway up        # model gateway on :4000
docker compose --profile safety up         # classifier (:8002) + ClamAV; set GUARD_URL, MALWARE_SCAN_URL in .env
docker compose --profile observability up  # Grafana on :3001, Prometheus :9090
docker compose --profile tools up          # pgAdmin on :5050
```

All ports bind to `127.0.0.1`. Values in `.env.example` are local-only; real secrets live in
AWS Secrets Manager. Never commit `.env`.

## Tests

```bash
cd services/api && uv run ruff check . && uv run pytest -q
cd apps/web && npx tsc --noEmit && npm run build
```

CI runs the same plus Postgres integration, the model-gateway suite, RAG evaluation, Playwright
e2e, k6 load, resilience drills, the agents-as-services check (scale to 3 replicas, concurrent
plans, queue durability), Terraform validate + checkov, and Trivy image scans.

Without Docker and other details: [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

## Branches

Work is delivered as stacked branches, each building on the previous one; `frontend` and
`backend` are the original branches and are not modified.

| Branch | Adds |
|---|---|
| `integration/platform` | Monorepo base, docs, CI |
| `feature/*` (platform) | Backend, web app, auth, model gateway, agent workflow, RAG, guardrails, observability, AWS infrastructure, tests, guard + scanner |
| `feature/agent-runtime` | Agent bus (SQS + Redis), worker, Terraform, local stack, CI job |
| `feature/agent-{supervisor,flight,hotel,weather,budget,itinerary}` | Each agent enabled as its own service |
| `fix/frontend-nextjs15` | Next.js 15 upgrade of the legacy `frontend` app |

See [BRANCH_STRATEGY](docs/BRANCH_STRATEGY.md).

## Status and blockers

Not deployed. Before production:

1. An AWS account: Terraform has only been validated and scanned, never applied.
2. A run with real models and tools: all end-to-end runs so far use mock mode.
3. Rotate the `VERCEL_OIDC_TOKEN` exposed in the `backend` branch history.
4. Ship the Next.js 15 upgrade to the live site (`fix/frontend-nextjs15` into `frontend`).

## Docs

| Doc | Contents |
|---|---|
| [ARCHITECTURE](docs/ARCHITECTURE.md) | Current and target architecture, key decisions |
| [IMPLEMENTATION_PLAN](docs/IMPLEMENTATION_PLAN.md) | Phased roadmap and live checklist |
| [BRANCH_STRATEGY](docs/BRANCH_STRATEGY.md) | Branches, protection rules, integration flow |
| [MICROSERVICES](docs/MICROSERVICES.md) | Service boundaries, agents as separate services |
| [API_CONTRACTS](docs/API_CONTRACTS.md) | REST/SSE contract |
| [DATABASE_DESIGN](docs/DATABASE_DESIGN.md) | Stores, ownership, schemas |
| [SECURITY](docs/SECURITY.md) | Findings register and controls |
| [DEPLOYMENT](docs/DEPLOYMENT.md) | Local dev, environments, AWS target |
| [OBSERVABILITY](docs/OBSERVABILITY.md) | Telemetry pipeline, KPIs, alerts |
| [RAG_EVALUATION](docs/RAG_EVALUATION.md) | Retrieval benchmark: strategies, metrics, limits |
| [PRODUCTION_READINESS](docs/PRODUCTION_READINESS.md) | Go/no-go review: evidence, load and resilience results, blockers |
| [infra/terraform](infra/terraform/README.md) | Terraform modules and environments |

## License

See [LICENSE](LICENSE).
