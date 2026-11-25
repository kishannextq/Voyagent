-- Local only (docker compose, fresh volume): separate database for the model gateway's own
-- tables (virtual keys, budgets, spend logs), managed by LiteLLM's migrations.
CREATE DATABASE litellm;
