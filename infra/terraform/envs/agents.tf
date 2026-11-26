# Agents that run as their own ECS services (modules/services/agents.tf). Each one added here
# gets its own SQS queue, task role, service and queue-depth autoscaling, and the orchestrator
# sends it tasks instead of running it in-process. Agents not listed run inside the orchestrator.
#
# secrets: only what that agent uses. timeout_seconds: from services/api/agents/<name>.md.
locals {
  agents = {
    supervisor = {
      cpu             = 256, memory = 512, min = 1, max = var.production_sizing ? 10 : 3
      timeout_seconds = 30, database = false
      secrets         = ["LLM_GATEWAY_KEY"]
    }
    flight_agent = {
      cpu             = 512, memory = 1024, min = 1, max = var.production_sizing ? 10 : 3
      timeout_seconds = 60, database = false
      secrets         = ["AVIATION_STACK_API_KEY", "LLM_GATEWAY_KEY", "GUARD_TOKEN"]
    }
  }
}
