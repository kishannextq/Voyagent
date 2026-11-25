# ECS Fargate: web, api (public via ALB), orchestrator, model gateway, ingest worker (internal via
# Service Connect), plus one-off `migrate` and scheduled `maintenance` task definitions.
# Rolling deploys with the deployment circuit breaker and automatic rollback.

terraform {
  required_providers {
    aws = { source = "hashicorp/aws", version = ">= 6.0" }
  }
}

variable "name" { type = string }
variable "environment" { type = string }
variable "vpc_id" { type = string }
variable "vpc_cidr" { type = string }
variable "app_subnet_ids" { type = list(string) }
variable "alb_security_group_id" { type = string }
variable "app_security_group_id" {
  type        = string
  description = "Security group shared by all ECS services (created in the network module)"
}
variable "api_target_group_arn" { type = string }
variable "web_target_group_arn" { type = string }
variable "kms_key_arn" { type = string }
variable "image_tag" {
  type        = string
  description = "Immutable image tag (git SHA) deployed to every service"
}
variable "secret_arns" {
  type        = map(string)
  description = "Secrets Manager ARNs by env var name (ORCHESTRATOR_TOKEN, LITELLM_MASTER_KEY, provider keys...)"
}
variable "db_master_secret_arn" { type = string }
variable "db_endpoint" { type = string }
variable "redis_url_secret_arn" { type = string }
variable "frontend_origins" { type = string }
variable "cognito_issuer" { type = string }
variable "cognito_client_id" { type = string }
variable "forwarded_allow_ips" {
  type        = string
  description = "VPC CIDR: uvicorn trusts X-Forwarded-For only from the ALB's subnets"
}
variable "desired" {
  type = map(number)
  default = {
    web = 2, api = 2, orchestrator = 2, gateway = 2, ingest = 1
  }
}
variable "max" {
  type = map(number)
  default = {
    web = 6, api = 10, orchestrator = 20
  }
}
variable "log_retention_days" {
  type    = number
  default = 365
}

data "aws_region" "current" {}

locals {
  region        = data.aws_region.current.region
  repos         = toset(["api", "web", "model-gateway"])
  api_image     = "${aws_ecr_repository.this["api"].repository_url}:${var.image_tag}"
  web_image     = "${aws_ecr_repository.this["web"].repository_url}:${var.image_tag}"
  gateway_image = "${aws_ecr_repository.this["model-gateway"].repository_url}:${var.image_tag}"
  common_env = [
    { name = "APP_ENV", value = var.environment },
    { name = "MOCK_MODE", value = "false" },
    { name = "DB_HOST", value = var.db_endpoint },
    { name = "DB_NAME", value = "voyagent" },
    { name = "CHECKPOINTER_SETUP", value = "false" },
    { name = "METRICS_PORT", value = "9464" },
    { name = "RAG_EMBEDDER", value = "gateway" },
    { name = "RAG_REQUIRE_SCAN", value = "true" },
    { name = "LLM_GATEWAY_URL", value = "http://gateway:4000" },
  ]
  db_secrets = [
    { name = "DB_USER", valueFrom = "${var.db_master_secret_arn}:username::" },
    { name = "DB_PASSWORD", valueFrom = "${var.db_master_secret_arn}:password::" },
    { name = "REDIS_URL", valueFrom = var.redis_url_secret_arn },
  ]
  logs = { for s in ["web", "api", "orchestrator", "gateway", "ingest", "jobs"] : s => {
    logDriver = "awslogs"
    options = {
      awslogs-group         = "/ecs/${var.name}/${s}"
      awslogs-region        = local.region
      awslogs-stream-prefix = s
    }
  } }
}

# ---------------- images ----------------

resource "aws_ecr_repository" "this" {
  for_each             = local.repos
  name                 = "${var.name}/${each.key}"
  image_tag_mutability = "IMMUTABLE"
  image_scanning_configuration { scan_on_push = true }
  encryption_configuration {
    encryption_type = "KMS"
    kms_key         = var.kms_key_arn
  }
}

resource "aws_ecr_lifecycle_policy" "this" {
  for_each   = aws_ecr_repository.this
  repository = each.value.name
  policy = jsonencode({ rules = [{
    rulePriority = 1, description = "Keep the last 50 images",
    selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 50 },
    action       = { type = "expire" }
  }] })
}

# ---------------- cluster ----------------

resource "aws_ecs_cluster" "this" {
  name = var.name
  setting {
    name  = "containerInsights"
    value = "enhanced"
  }
}

resource "aws_service_discovery_http_namespace" "this" {
  name = "${var.name}.internal"
}

resource "aws_cloudwatch_log_group" "this" {
  for_each          = local.logs
  name              = each.value.options.awslogs-group
  retention_in_days = var.log_retention_days
  kms_key_id        = var.kms_key_arn
}

# ---------------- network ----------------

resource "aws_vpc_security_group_ingress_rule" "from_alb" {
  for_each                     = { web = 3000, api = 8000 }
  security_group_id            = var.app_security_group_id
  referenced_security_group_id = var.alb_security_group_id
  ip_protocol                  = "tcp"
  from_port                    = each.value
  to_port                      = each.value
  description                  = "ALB to ${each.key}"
}

resource "aws_vpc_security_group_ingress_rule" "internal" {
  for_each                     = { orchestrator = 8001, gateway = 4000 }
  security_group_id            = var.app_security_group_id
  referenced_security_group_id = var.app_security_group_id
  ip_protocol                  = "tcp"
  from_port                    = each.value
  to_port                      = each.value
  description                  = "Service Connect to ${each.key}"
}

resource "aws_vpc_security_group_ingress_rule" "metrics" {
  security_group_id = var.app_security_group_id
  cidr_ipv4         = var.vpc_cidr
  ip_protocol       = "tcp"
  from_port         = 9464
  to_port           = 9464
  description       = "Prometheus scrape from inside the VPC"
}

resource "aws_vpc_security_group_egress_rule" "all" {
  security_group_id = var.app_security_group_id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
  description       = "Outbound: data stores, AWS endpoints, model/tool providers via NAT"
}

# ---------------- IAM ----------------

data "aws_iam_policy_document" "ecs_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "execution" {
  name_prefix        = "${var.name}-exec-"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

resource "aws_iam_role_policy_attachment" "execution" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "execution_secrets" {
  statement {
    actions   = ["secretsmanager:GetSecretValue"]
    resources = concat(values(var.secret_arns), [var.db_master_secret_arn, var.redis_url_secret_arn])
  }
  statement {
    actions   = ["kms:Decrypt"]
    resources = [var.kms_key_arn]
  }
}

resource "aws_iam_role_policy" "execution_secrets" {
  role   = aws_iam_role.execution.id
  policy = data.aws_iam_policy_document.execution_secrets.json
}

# Task roles: least privilege per service. Only the gateway may call Bedrock.
resource "aws_iam_role" "task" {
  for_each           = toset(["web", "api", "orchestrator", "gateway", "ingest", "jobs"])
  name_prefix        = "${var.name}-${each.key}-"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

data "aws_caller_identity" "current" {}

data "aws_iam_policy_document" "gateway_bedrock" {
  statement {
    actions = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream", "bedrock:ApplyGuardrail"]
    resources = [
      "arn:aws:bedrock:*::foundation-model/*",
      "arn:aws:bedrock:*:${data.aws_caller_identity.current.account_id}:inference-profile/*",
      "arn:aws:bedrock:${local.region}:${data.aws_caller_identity.current.account_id}:guardrail/*",
    ]
  }
}

resource "aws_iam_role_policy" "gateway_bedrock" {
  role   = aws_iam_role.task["gateway"].id
  policy = data.aws_iam_policy_document.gateway_bedrock.json
}

# ---------------- task definitions ----------------

locals {
  task_defs = {
    web = {
      cpu = 256, memory = 512, image = local.web_image, port = 3000, command = null
      env = [], secrets = []
    }
    api = {
      cpu = 512, memory = 1024, image = local.api_image, port = 8000, command = null
      env = concat(local.common_env, [
        { name = "ORCHESTRATOR_URL", value = "http://orchestrator:8001" },
        { name = "FRONTEND_ORIGINS", value = var.frontend_origins },
        { name = "AUTH_MODE", value = "cognito" },
        { name = "COGNITO_ISSUER", value = var.cognito_issuer },
        { name = "COGNITO_CLIENT_IDS", value = var.cognito_client_id },
        { name = "FORWARDED_ALLOW_IPS", value = var.forwarded_allow_ips },
      ])
      secrets = concat(local.db_secrets, [{ name = "ORCHESTRATOR_TOKEN", valueFrom = var.secret_arns["ORCHESTRATOR_TOKEN"] }])
    }
    orchestrator = {
      cpu     = 1024, memory = 2048, image = local.api_image, port = 8001
      command = ["sh", "-c", "exec uvicorn orchestrator_app:app --host 0.0.0.0 --port 8001"]
      env     = local.common_env
      secrets = concat(local.db_secrets, [for k in ["ORCHESTRATOR_TOKEN", "LLM_GATEWAY_KEY", "TAVILY_API_KEY", "AVIATION_STACK_API_KEY", "OPENWEATHER_API_KEY"] : { name = k, valueFrom = var.secret_arns[k] }])
    }
    gateway = {
      cpu = 512, memory = 1024, image = local.gateway_image, port = 4000, command = null
      env = [{ name = "REDIS_PORT", value = "6379" }]
      secrets = [
        { name = "LITELLM_MASTER_KEY", valueFrom = var.secret_arns["LITELLM_MASTER_KEY"] },
        { name = "GROQ_API_KEY", valueFrom = var.secret_arns["GROQ_API_KEY"] },
      ]
    }
    ingest = {
      cpu     = 512, memory = 1024, image = local.api_image, port = null
      command = ["python", "ingest_worker.py"]
      env     = local.common_env
      secrets = concat(local.db_secrets, [{ name = "LLM_GATEWAY_KEY", valueFrom = var.secret_arns["LLM_GATEWAY_KEY"] }])
    }
  }
}

resource "aws_ecs_task_definition" "svc" {
  for_each                 = local.task_defs
  family                   = "${var.name}-${each.key}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = each.value.cpu
  memory                   = each.value.memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task[each.key].arn
  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }
  container_definitions = jsonencode([merge(
    {
      name                   = each.key
      image                  = each.value.image
      essential              = true
      readonlyRootFilesystem = false
      environment            = each.value.env
      secrets                = each.value.secrets
      logConfiguration       = local.logs[each.key]
      linuxParameters        = { initProcessEnabled = true }
    },
    each.value.port == null ? {} : { portMappings = [{ name = each.key, containerPort = each.value.port, protocol = "tcp" }] },
    each.value.command == null ? {} : { command = each.value.command },
  )])
}

# One-off jobs share the api image: `python migrate.py` before each deploy, and the daily purge.
resource "aws_ecs_task_definition" "job" {
  for_each                 = { migrate = ["python", "migrate.py"], maintenance = ["python", "maintenance.py", "purge"] }
  family                   = "${var.name}-${each.key}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task["jobs"].arn
  container_definitions = jsonencode([{
    name             = each.key
    image            = local.api_image
    essential        = true
    command          = each.value
    environment      = local.common_env
    secrets          = local.db_secrets
    logConfiguration = local.logs["jobs"]
  }])
}

# ---------------- services ----------------

locals {
  public_services = { web = var.web_target_group_arn, api = var.api_target_group_arn }
  service_connect = toset(["orchestrator", "gateway"]) # discoverable as http://<name>:<port>
}

resource "aws_ecs_service" "svc" {
  for_each               = local.task_defs
  name                   = each.key
  cluster                = aws_ecs_cluster.this.id
  task_definition        = aws_ecs_task_definition.svc[each.key].arn
  desired_count          = var.desired[each.key]
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

  dynamic "load_balancer" {
    for_each = contains(keys(local.public_services), each.key) ? [local.public_services[each.key]] : []
    content {
      target_group_arn = load_balancer.value
      container_name   = each.key
      container_port   = each.value.port
    }
  }

  service_connect_configuration {
    enabled   = true
    namespace = aws_service_discovery_http_namespace.this.arn
    dynamic "service" {
      for_each = contains(local.service_connect, each.key) ? [each.key] : []
      content {
        port_name = service.value
        client_alias {
          port     = each.value.port
          dns_name = service.value
        }
      }
    }
  }

  lifecycle {
    ignore_changes = [desired_count] # owned by autoscaling after creation
  }
}

resource "aws_appautoscaling_target" "svc" {
  for_each           = var.max
  service_namespace  = "ecs"
  resource_id        = "service/${aws_ecs_cluster.this.name}/${aws_ecs_service.svc[each.key].name}"
  scalable_dimension = "ecs:service:DesiredCount"
  min_capacity       = var.desired[each.key]
  max_capacity       = each.value
}

resource "aws_appautoscaling_policy" "cpu" {
  for_each           = aws_appautoscaling_target.svc
  name               = "${each.key}-cpu"
  policy_type        = "TargetTrackingScaling"
  service_namespace  = each.value.service_namespace
  resource_id        = each.value.resource_id
  scalable_dimension = each.value.scalable_dimension
  target_tracking_scaling_policy_configuration {
    target_value       = 60
    scale_in_cooldown  = 300
    scale_out_cooldown = 60
    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageCPUUtilization"
    }
  }
}

# ---------------- scheduled maintenance ----------------

data "aws_iam_policy_document" "scheduler_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "scheduler" {
  name_prefix        = "${var.name}-sched-"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume.json
}

data "aws_iam_policy_document" "scheduler" {
  statement {
    actions   = ["ecs:RunTask"]
    resources = [aws_ecs_task_definition.job["maintenance"].arn_without_revision, "${aws_ecs_task_definition.job["maintenance"].arn_without_revision}:*"]
  }
  statement {
    actions   = ["iam:PassRole"]
    resources = [aws_iam_role.execution.arn, aws_iam_role.task["jobs"].arn]
  }
}

resource "aws_iam_role_policy" "scheduler" {
  role   = aws_iam_role.scheduler.id
  policy = data.aws_iam_policy_document.scheduler.json
}

resource "aws_scheduler_schedule" "maintenance" {
  name                = "${var.name}-maintenance"
  schedule_expression = "cron(30 3 * * ? *)"
  kms_key_arn         = var.kms_key_arn
  flexible_time_window { mode = "OFF" }
  target {
    arn      = aws_ecs_cluster.this.arn
    role_arn = aws_iam_role.scheduler.arn
    ecs_parameters {
      task_definition_arn = aws_ecs_task_definition.job["maintenance"].arn_without_revision
      launch_type         = "FARGATE"
      network_configuration {
        subnets          = var.app_subnet_ids
        security_groups  = [var.app_security_group_id]
        assign_public_ip = false
      }
    }
  }
}

output "cluster_arn" { value = aws_ecs_cluster.this.arn }
output "cluster_name" { value = aws_ecs_cluster.this.name }
output "app_security_group_id" { value = var.app_security_group_id }
output "ecr_repository_arns" { value = [for r in aws_ecr_repository.this : r.arn] }
output "ecr_repository_urls" { value = { for k, r in aws_ecr_repository.this : k => r.repository_url } }
output "role_arns" { value = concat([aws_iam_role.execution.arn], [for r in aws_iam_role.task : r.arn]) }
output "migrate_task_definition" { value = aws_ecs_task_definition.job["migrate"].family }
