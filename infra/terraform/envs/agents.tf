# Agents that run as their own ECS services (modules/services/agents.tf). Each one added here
# gets its own SQS queue, task role, service and queue-depth autoscaling, and the orchestrator
# sends it tasks instead of running it in-process. Agents not listed run inside the orchestrator.
#
# secrets: only what that agent uses. timeout_seconds: from services/api/agents/<name>.md.
locals {
  agents = {}
}
