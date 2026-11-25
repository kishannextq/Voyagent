# Data stores in isolated subnets: RDS PostgreSQL 17 (pgvector) and ElastiCache (Valkey).
# Credentials never appear in Terraform state: RDS manages its master password in Secrets
# Manager; the cache auth token is generated into a Secrets Manager secret.

terraform {
  required_providers {
    aws    = { source = "hashicorp/aws", version = ">= 6.0" }
    random = { source = "hashicorp/random", version = ">= 3.6" }
  }
}

variable "name" { type = string }
variable "vpc_id" { type = string }
variable "data_subnet_ids" { type = list(string) }
variable "client_security_group_ids" {
  type        = list(string)
  description = "Security groups (ECS services) allowed to connect"
}
variable "db_instance_class" {
  type    = string
  default = "db.t4g.medium"
}
variable "db_allocated_storage" {
  type    = number
  default = 50
}
variable "multi_az" {
  type    = bool
  default = false
}
variable "backup_retention_days" {
  type    = number
  default = 7
}
variable "deletion_protection" {
  type    = bool
  default = true
}
variable "cache_node_type" {
  type    = string
  default = "cache.t4g.small"
}
variable "cache_replicas" {
  type    = number
  default = 0
}
variable "kms_key_arn" { type = string }

resource "aws_security_group" "db" {
  name_prefix = "${var.name}-db-"
  description = "PostgreSQL from application services only"
  vpc_id      = var.vpc_id
  lifecycle { create_before_destroy = true }
}

resource "aws_vpc_security_group_ingress_rule" "db" {
  for_each                     = toset(var.client_security_group_ids)
  security_group_id            = aws_security_group.db.id
  referenced_security_group_id = each.value
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
  description                  = "PostgreSQL from app"
}

resource "aws_db_subnet_group" "this" {
  name_prefix = "${var.name}-"
  subnet_ids  = var.data_subnet_ids
}

resource "aws_db_parameter_group" "pg17" {
  name_prefix = "${var.name}-pg17-"
  family      = "postgres17"
  parameter {
    name  = "rds.force_ssl"
    value = "1"
  }
  parameter {
    name  = "log_min_duration_statement"
    value = "1000"
  }
  lifecycle { create_before_destroy = true }
}

data "aws_iam_policy_document" "monitoring_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["monitoring.rds.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "monitoring" {
  name_prefix        = "${var.name}-rdsmon-"
  assume_role_policy = data.aws_iam_policy_document.monitoring_assume.json
}

resource "aws_iam_role_policy_attachment" "monitoring" {
  role       = aws_iam_role.monitoring.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonRDSEnhancedMonitoringRole"
}

resource "aws_db_instance" "this" {
  #checkov:skip=CKV_AWS_157:Multi-AZ is per environment (var.multi_az = true for production)
  identifier_prefix                   = "${var.name}-"
  engine                              = "postgres"
  engine_version                      = "17"
  instance_class                      = var.db_instance_class
  allocated_storage                   = var.db_allocated_storage
  max_allocated_storage               = var.db_allocated_storage * 4
  storage_type                        = "gp3"
  storage_encrypted                   = true
  kms_key_id                          = var.kms_key_arn
  db_name                             = "voyagent"
  username                            = "voyagent_admin"
  manage_master_user_password         = true # password lives only in Secrets Manager
  db_subnet_group_name                = aws_db_subnet_group.this.name
  vpc_security_group_ids              = [aws_security_group.db.id]
  parameter_group_name                = aws_db_parameter_group.pg17.name
  multi_az                            = var.multi_az
  publicly_accessible                 = false
  backup_retention_period             = var.backup_retention_days
  copy_tags_to_snapshot               = true
  deletion_protection                 = var.deletion_protection
  skip_final_snapshot                 = false
  final_snapshot_identifier           = "${var.name}-final"
  iam_database_authentication_enabled = true
  performance_insights_enabled        = true
  performance_insights_kms_key_id     = var.kms_key_arn
  auto_minor_version_upgrade          = true
  enabled_cloudwatch_logs_exports     = ["postgresql"]
  monitoring_interval                 = 60
  monitoring_role_arn                 = aws_iam_role.monitoring.arn
}

resource "aws_security_group" "cache" {
  name_prefix = "${var.name}-cache-"
  description = "Valkey/Redis from application services only"
  vpc_id      = var.vpc_id
  lifecycle { create_before_destroy = true }
}

resource "aws_vpc_security_group_ingress_rule" "cache" {
  for_each                     = toset(var.client_security_group_ids)
  security_group_id            = aws_security_group.cache.id
  referenced_security_group_id = each.value
  ip_protocol                  = "tcp"
  from_port                    = 6379
  to_port                      = 6379
  description                  = "Valkey from app"
}

resource "aws_elasticache_subnet_group" "this" {
  name       = "${var.name}-cache"
  subnet_ids = var.data_subnet_ids
}

resource "random_password" "cache" {
  length  = 48
  special = false
}

resource "aws_secretsmanager_secret" "cache" {
  #checkov:skip=CKV2_AWS_57:Cache auth token rotation needs a coordinated restart; planned
  name_prefix = "${var.name}/redis-url-"
  kms_key_id  = var.kms_key_arn
}

resource "aws_elasticache_replication_group" "this" {
  #checkov:skip=CKV2_AWS_50:Failover/Multi-AZ is per environment (cache_replicas > 0 for production)
  replication_group_id       = "${var.name}-cache"
  description                = "Voyagent run metadata, rate limits, queues, gateway cache"
  engine                     = "valkey"
  engine_version             = "8.0"
  node_type                  = var.cache_node_type
  num_cache_clusters         = 1 + var.cache_replicas
  automatic_failover_enabled = var.cache_replicas > 0
  multi_az_enabled           = var.cache_replicas > 0
  port                       = 6379
  subnet_group_name          = aws_elasticache_subnet_group.this.name
  security_group_ids         = [aws_security_group.cache.id]
  at_rest_encryption_enabled = true
  kms_key_id                 = var.kms_key_arn
  transit_encryption_enabled = true
  auth_token                 = random_password.cache.result
  snapshot_retention_limit   = 1
}

# rediss:// URL (TLS) for the services. Note: the random token is in Terraform state; the state
# bucket is encrypted and access-restricted (see envs/*/backend.hcl).
resource "aws_secretsmanager_secret_version" "cache" {
  secret_id     = aws_secretsmanager_secret.cache.id
  secret_string = "rediss://:${random_password.cache.result}@${aws_elasticache_replication_group.this.primary_endpoint_address}:6379/0"
}

output "db_endpoint" { value = aws_db_instance.this.address }
output "db_name" { value = aws_db_instance.this.db_name }
output "db_master_secret_arn" { value = aws_db_instance.this.master_user_secret[0].secret_arn }
output "redis_url_secret_arn" { value = aws_secretsmanager_secret.cache.arn }
