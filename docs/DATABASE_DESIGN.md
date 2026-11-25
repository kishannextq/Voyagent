# Database Design

## Stores and ownership

| Store | Use | Owner(s) |
|---|---|---|
| PostgreSQL (Aurora in AWS, `pgvector/pgvector:pg17` locally) | relational app data, LangGraph checkpoints, vectors, gateway spend | one schema + one DB role per service; no cross-schema grants |
| Redis (ElastiCache) | rate limits, response/semantic cache, short-lived locks, SSE resume buffers | namespaced keys per service (`api:`, `gw:`, `orch:`) |
| S3 | uploaded documents, extracted text, eval artifacts | `ingest-worker` (write), `rag` (read) |

Services never read another service's schema; they call its API.

## Current state

Only LangGraph checkpoint tables exist (created by `PostgresSaver.setup()` when `DATABASE_URL`
is set). Run metadata (`modes`, `fallback`, `dummy_nodes`) lives in Redis under `api:run:{thread_id}`
with a TTL (`RUN_META_TTL_SECONDS`, default 7 days); it moves to `app.plan_runs` once usage accounting lands.

## Planned schemas

### `app` (owned by api)
```sql
tenants(id uuid pk, name text, plan text, created_at timestamptz)
users(id uuid pk, tenant_id uuid fk, cognito_sub text unique, email_hash text, role text, created_at)
plans(id uuid pk /* = LangGraph thread_id */, tenant_id uuid, user_id uuid, status text,
      title text, created_at, updated_at, deleted_at)
      -- index (tenant_id, user_id, updated_at desc)
plan_runs(id uuid pk, plan_id uuid fk, modes jsonb, fallback jsonb, dummy_nodes jsonb,
          input_tokens int, output_tokens int, cost_usd numeric(12,6), started_at, finished_at)
audit_log(id bigserial, tenant_id, actor_id, action text, target text, at timestamptz, trace_id text)
```
Row-level security on `tenant_id` as defence in depth (app also filters).

### `orchestrator`
LangGraph `checkpoints`, `checkpoint_writes`, `checkpoint_blobs` (library-managed) and
`memories(id, tenant_id, user_id, kind, content, embedding vector, expires_at)`.

### `rag`
```sql
documents(id, tenant_id, s3_key, sha256, version, mime, status, error, created_at)  -- unique(tenant_id, sha256)
chunks(id, document_id, tenant_id, parent_id, ordinal, text, tsv tsvector, metadata jsonb)
embeddings(chunk_id, model, dim, embedding vector)  -- HNSW index per model; gin(tsv) for hybrid
```

### `litellm`
Managed by LiteLLM (keys, teams, budgets, spend logs).

## Operations
- Migrations: Alembic per service, run as a one-off ECS task before deploy; never on app start in prod.
- Pooling: psycopg_pool in-process; RDS Proxy in AWS if connection counts require it.
- Backups: Aurora PITR (35 days prod); quarterly restore drill.
- Retention: conversations/memories have per-tenant TTLs; deletion requests hard-delete plan, checkpoints, memories, chunks.
- Encryption: KMS at rest (Aurora, ElastiCache, S3), TLS in transit.
