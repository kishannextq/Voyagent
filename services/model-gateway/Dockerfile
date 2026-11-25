# Voyagent model gateway: LiteLLM proxy, pinned by digest (v1.108.5 LTS; see docs/MICROSERVICES.md
# for why this version) with our routing config baked in. Credentials come from the environment.
FROM ghcr.io/berriai/litellm@sha256:008a5916c0a0ca6f4f6ad64969f6a4ef6f03ce885a639f3d97fd53665754ce48
COPY config.yaml /app/config.yaml
EXPOSE 4000
CMD ["--config", "/app/config.yaml", "--port", "4000"]
