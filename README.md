# Voyagent

Multi-agent AI travel planner: a LangGraph supervisor routes each request to specialist agents
(flights, hotels, weather, budget, itinerary), pauses for human approval, and streams progress
to the browser over Server-Sent Events.

This branch (`integration/platform`) is the monorepo where Voyagent is being evolved into a
multi-tenant, observable, AWS-hosted platform. See [docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md)
for what is built and what is planned.

## Layout

```
apps/web/          Next.js 15 + React 19 + Tailwind UI
services/api/      FastAPI + LangGraph API (agents, MCP tools, human-in-the-loop)
docs/              Architecture, plan, branch strategy, security, deployment, …
docker-compose.yml Local stack: web, api, postgres (pgvector), redis, pgAdmin
.github/workflows/ CI: typecheck, build, lint, tests, secret scan, image scan
```

## Quick start

```bash
cp .env.example .env        # MOCK_MODE=true: full flow with canned responses, no keys needed
docker compose up --build
```

- Web: http://localhost:3000
- API docs: http://localhost:8000/docs

Without Docker and other details: [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

## Docs

| Doc | Contents |
|---|---|
| [ARCHITECTURE](docs/ARCHITECTURE.md) | Current and target architecture, key decisions |
| [IMPLEMENTATION_PLAN](docs/IMPLEMENTATION_PLAN.md) | Phased roadmap and live checklist |
| [BRANCH_STRATEGY](docs/BRANCH_STRATEGY.md) | Branches, protection rules, integration flow |
| [MICROSERVICES](docs/MICROSERVICES.md) | Service boundaries and per-service standards |
| [API_CONTRACTS](docs/API_CONTRACTS.md) | Current REST/SSE contract and v1 target |
| [DATABASE_DESIGN](docs/DATABASE_DESIGN.md) | Stores, ownership, schemas |
| [SECURITY](docs/SECURITY.md) | Findings register and controls |
| [DEPLOYMENT](docs/DEPLOYMENT.md) | Local dev, environments, AWS target |
| [OBSERVABILITY](docs/OBSERVABILITY.md) | Telemetry pipeline, KPIs, alerts |
| [RAG_EVALUATION](docs/RAG_EVALUATION.md) | Retrieval benchmark: strategies, metrics, limits |

## License

See [LICENSE](LICENSE).
