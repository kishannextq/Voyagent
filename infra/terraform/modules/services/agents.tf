# Agents as separate services (services/api/agent_bus.py, agent_worker.py).
#
# Per agent: an SQS queue (+ dead-letter queue, KMS-encrypted, TLS-only), a task role that can
# only consume that queue, an ECS service running `python agent_worker.py <agent>` with only that
# agent's secrets, and autoscaling on queue depth. The orchestrator may only send to the queues.
# Results return through Redis (already in the stack).

variable "agents" {
  description = "Agents that run as their own ECS services. Key: agent name (agents/<name>.md)."
  type = map(object({
    cpu             = number
    memory          = number
    min             = number       # >= 1 (target tracking divides by running tasks)
    max             = number
    timeout_seconds = number       # timeout_seconds from agents/<name>.md
    secrets         = list(string) # keys of var.secret_arns this agent needs, nothing else
    database        = bool         # document search (Postgres); others get no DB credentials
    concurrency     = optional(number, 8)
  }))
  default = {}
  validation {
    condition     = alltrue([for a in values(var.agents) : a.min >= 1 && a.max >= a.min])
    error_message = "Each agent needs 1 <= min <= max."
  }
}

variable "agent_queue_wait_seconds" {
  description = "How long a task may wait in its queue before the orchestrator gives up (AGENT_QUEUE_WAIT_SECONDS)."
  type        = number
  default     = 30
}

variable "agent_target_utilization" {
  description = "Autoscaling target: (queued + running tasks) / (running workers x concurrency)."
  type        = number
  default     = 0.7
}

locals {
  agent_queue_prefix = "${var.name}-"
  agent_ids          = { for k, _ in var.agents : k => replace(k, "_", "-") }
  remote_agents_env = length(var.agents) == 0 ? [] : [
    { name = "REMOTE_AGENTS", value = join(",", sort(keys(var.agents))) },
    { name = "AGENT_QUEUE_PREFIX", value = local.agent_queue_prefix },
    { name = "AGENT_QUEUE_WAIT_SECONDS", value = tostring(var.agent_queue_wait_seconds) },
  ]
}

# ---------------- queues ----------------

resource "aws_sqs_queue" "agent_dlq" {
  for_each                          = var.agents
  name                              = "${local.agent_queue_prefix}${local.agent_ids[each.key]}-dlq"
  message_retention_seconds         = 1209600 # 14 days, for investigation
  kms_master_key_id                 = var.kms_key_arn
  kms_data_key_reuse_period_seconds = 300
}

resource "aws_sqs_queue" "agent" {
  for_each = var.agents
  name     = "${local.agent_queue_prefix}${local.agent_ids[each.key]}"
  # Longer than one run of the agent, so a message isn't redelivered while it's being worked on.
  visibility_timeout_seconds = ceil(each.value.timeout_seconds + var.agent_queue_wait_seconds + 30)
  # A task is useless after its deadline (timeout + queue wait); keep it a little longer at most.
  message_retention_seconds         = 900
  receive_wait_time_seconds         = 20
  max_message_size                  = 262144
  kms_master_key_id                 = var.kms_key_arn
  kms_data_key_reuse_period_seconds = 300
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.agent_dlq[each.key].arn
    maxReceiveCount     = 3
  })
}

data "aws_iam_policy_document" "agent_queue_tls" {
  for_each = var.agents
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["sqs:*"]
    resources = [aws_sqs_queue.agent[each.key].arn, aws_sqs_queue.agent_dlq[each.key].arn]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_sqs_queue_policy" "agent" {
  for_each  = var.agents
  queue_url = aws_sqs_queue.agent[each.key].id
  policy    = data.aws_iam_policy_document.agent_queue_tls[each.key].json
}

# ---------------- IAM ----------------

resource "aws_iam_role" "agent" {
  for_each           = var.agents
  name_prefix        = "${var.name}-${substr(local.agent_ids[each.key], 0, 20)}-"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

data "aws_iam_policy_document" "agent_consume" {
  for_each = var.agents
  statement {
    actions = [
      "sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:ChangeMessageVisibility",
      "sqs:GetQueueUrl", "sqs:GetQueueAttributes",
    ]
    resources = [aws_sqs_queue.agent[each.key].arn]
  }
  statement {
    actions   = ["kms:Decrypt"]
    resources = [var.kms_key_arn]
  }
}

resource "aws_iam_role_policy" "agent_consume" {
  for_each = var.agents
  role     = aws_iam_role.agent[each.key].id
  policy   = data.aws_iam_policy_document.agent_consume[each.key].json
}

data "aws_iam_policy_document" "orchestrator_send" {
  count = length(var.agents) == 0 ? 0 : 1
  statement {
    actions   = ["sqs:SendMessage", "sqs:GetQueueUrl"]
    resources = [for q in aws_sqs_queue.agent : q.arn]
  }
  statement {
    actions   = ["kms:GenerateDataKey", "kms:Decrypt"]
    resources = [var.kms_key_arn]
  }
}

resource "aws_iam_role_policy" "orchestrator_send" {
  count  = length(var.agents) == 0 ? 0 : 1
  role   = aws_iam_role.task["orchestrator"].id
  policy = data.aws_iam_policy_document.orchestrator_send[0].json
}

# ---------------- tasks and services ----------------

resource "aws_cloudwatch_log_group" "agent" {
  for_each          = var.agents
  name              = "/ecs/${var.name}/agent-${local.agent_ids[each.key]}"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.kms_key_arn
}

resource "aws_ecs_task_definition" "agent" {
  for_each                 = var.agents
  family                   = "${var.name}-agent-${local.agent_ids[each.key]}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = each.value.cpu
  memory                   = each.value.memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.agent[each.key].arn
  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }
  container_definitions = jsonencode([{
    name                   = "agent"
    image                  = local.api_image
    essential              = true
    command                = ["python", "agent_worker.py", each.key]
    readonlyRootFilesystem = false
    stopTimeout            = 120 # AGENT_DRAIN_SECONDS (100) + margin
    linuxParameters        = { initProcessEnabled = true }
    environment = concat(
      [
        { name = "APP_ENV", value = var.environment },
        { name = "MOCK_MODE", value = "false" },
        { name = "METRICS_PORT", value = "9464" },
        { name = "AWS_REGION", value = local.region },
        { name = "AGENT_QUEUE_PREFIX", value = local.agent_queue_prefix },
        { name = "AGENT_QUEUE_WAIT_SECONDS", value = tostring(var.agent_queue_wait_seconds) },
        { name = "AGENT_CONCURRENCY", value = tostring(each.value.concurrency) },
        { name = "LLM_GATEWAY_URL", value = "http://gateway:4000" },
        { name = "GUARD_URL", value = "http://guard:8002" },
        { name = "GUARD_FAIL_MODE", value = "closed" },
      ],
      each.value.database ? [
        { name = "DB_HOST", value = var.db_endpoint },
        { name = "DB_NAME", value = "voyagent" },
        { name = "RAG_EMBEDDER", value = "gateway" },
      ] : [],
    )
    secrets = concat(
      [{ name = "REDIS_URL", valueFrom = var.redis_url_secret_arn }],
      [for k in each.value.secrets : { name = k, valueFrom = var.secret_arns[k] }],
      each.value.database ? [
        { name = "DB_USER", valueFrom = "${var.db_master_secret_arn}:username::" },
        { name = "DB_PASSWORD", valueFrom = "${var.db_master_secret_arn}:password::" },
      ] : [],
    )
    healthCheck = {
      command     = ["CMD", "python", "agent_worker.py", "--health"]
      interval    = 30
      timeout     = 5
      retries     = 3
      startPeriod = 30
    }
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.agent[each.key].name
        awslogs-region        = local.region
        awslogs-stream-prefix = "agent"
      }
    }
  }])
}

resource "aws_ecs_service" "agent" {
  for_each               = var.agents
  name                   = "agent-${local.agent_ids[each.key]}"
  cluster                = aws_ecs_cluster.this.id
  task_definition        = aws_ecs_task_definition.agent[each.key].arn
  desired_count          = each.value.min
  launch_type            = "FARGATE"
  enable_execute_command = false
  propagate_tags         = "SERVICE"

  network_configuration {
    subnets          = var.app_subnet_ids
    security_groups  = [var.app_security_group_id]
    assign_public_ip = false
  }

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }
  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200

  # Client only: reaches gateway:4000 and guard:8002; nothing connects to an agent.
  service_connect_configuration {
    enabled   = true
    namespace = aws_service_discovery_http_namespace.this.arn
  }

  lifecycle {
    ignore_changes = [desired_count] # owned by autoscaling after creation
  }
}

# ---------------- autoscaling on queue depth ----------------

resource "aws_appautoscaling_target" "agent" {
  for_each           = var.agents
  service_namespace  = "ecs"
  resource_id        = "service/${aws_ecs_cluster.this.name}/${aws_ecs_service.agent[each.key].name}"
  scalable_dimension = "ecs:service:DesiredCount"
  min_capacity       = each.value.min
  max_capacity       = each.value.max
}

# Utilisation = (waiting + in-flight messages) / (running tasks x concurrency). Messages stay
# in flight while an agent works on them, so this counts busy slots as well as backlog.
resource "aws_appautoscaling_policy" "agent_queue" {
  for_each           = var.agents
  name               = "agent-${local.agent_ids[each.key]}-queue"
  policy_type        = "TargetTrackingScaling"
  service_namespace  = aws_appautoscaling_target.agent[each.key].service_namespace
  resource_id        = aws_appautoscaling_target.agent[each.key].resource_id
  scalable_dimension = aws_appautoscaling_target.agent[each.key].scalable_dimension

  target_tracking_scaling_policy_configuration {
    target_value       = var.agent_target_utilization
    scale_out_cooldown = 30
    scale_in_cooldown  = 300

    customized_metric_specification {
      metrics {
        id          = "visible"
        return_data = false
        metric_stat {
          stat = "Sum"
          metric {
            namespace   = "AWS/SQS"
            metric_name = "ApproximateNumberOfMessagesVisible"
            dimensions {
              name  = "QueueName"
              value = aws_sqs_queue.agent[each.key].name
            }
          }
        }
      }
      metrics {
        id          = "inflight"
        return_data = false
        metric_stat {
          stat = "Sum"
          metric {
            namespace   = "AWS/SQS"
            metric_name = "ApproximateNumberOfMessagesNotVisible"
            dimensions {
              name  = "QueueName"
              value = aws_sqs_queue.agent[each.key].name
            }
          }
        }
      }
      metrics {
        id          = "tasks"
        return_data = false
        metric_stat {
          stat = "Average"
          metric {
            namespace   = "ECS/ContainerInsights"
            metric_name = "RunningTaskCount"
            dimensions {
              name  = "ClusterName"
              value = aws_ecs_cluster.this.name
            }
            dimensions {
              name  = "ServiceName"
              value = aws_ecs_service.agent[each.key].name
            }
          }
        }
      }
      metrics {
        id          = "utilization"
        label       = "busy fraction of agent slots"
        expression  = "(visible + inflight) / (tasks * ${each.value.concurrency})"
        return_data = true
      }
    }
  }
}

# ---------------- alarms (routing to on-call is not set up yet) ----------------

resource "aws_cloudwatch_metric_alarm" "agent_dlq" {
  for_each            = var.agents
  alarm_name          = "${var.name}-agent-${local.agent_ids[each.key]}-dlq"
  alarm_description   = "Agent tasks failed delivery 3 times (worker crash loop or poison message)."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = aws_sqs_queue.agent_dlq[each.key].name }
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
}

resource "aws_cloudwatch_metric_alarm" "agent_backlog_age" {
  for_each            = var.agents
  alarm_name          = "${var.name}-agent-${local.agent_ids[each.key]}-backlog-age"
  alarm_description   = "Agent tasks wait too long in the queue: scaling is not keeping up or the service is down."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateAgeOfOldestMessage"
  dimensions          = { QueueName = aws_sqs_queue.agent[each.key].name }
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 3
  threshold           = var.agent_queue_wait_seconds / 2
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
}

output "agent_queue_urls" { value = { for k, q in aws_sqs_queue.agent : k => q.url } }
output "agent_role_arns" { value = [for r in aws_iam_role.agent : r.arn] }
