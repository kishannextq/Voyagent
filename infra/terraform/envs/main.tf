# One root module per environment, selected by -var-file and -backend-config:
#   terraform init -backend-config=staging.backend.hcl
#   terraform plan -var-file=staging.tfvars
# Nothing here has been applied. Creating this stack is billable and needs explicit approval.

terraform {
  required_version = ">= 1.10"
  required_providers {
    aws    = { source = "hashicorp/aws", version = "~> 6.72" }
    random = { source = "hashicorp/random", version = "~> 3.9" }
  }
  # S3 state with native locking (use_lockfile), encrypted; bucket/key/region per environment.
  backend "s3" {}
}

variable "environment" {
  type = string
  validation {
    condition     = contains(["staging", "production"], var.environment)
    error_message = "environment must be staging or production."
  }
}
variable "region" { type = string }
variable "azs" { type = list(string) }
variable "image_tag" { type = string }
variable "certificate_arn" { type = string }
variable "app_hostname" {
  type        = string
  description = "Public hostname of the web app, e.g. app.example.com (domain needs owner action)"
}
variable "cognito_domain_prefix" { type = string }
variable "github_repository" { type = string }
variable "production_sizing" {
  type    = bool
  default = false
}

provider "aws" {
  region = var.region
  default_tags {
    tags = { project = "voyagent", environment = var.environment, managed_by = "terraform" }
  }
}

locals {
  name = "voyagent-${var.environment}"
}

module "secrets" {
  source = "../modules/secrets"
  name   = local.name
}

module "network" {
  source      = "../modules/network"
  name        = local.name
  azs         = var.azs
  single_nat  = !var.production_sizing
  kms_key_arn = module.secrets.kms_key_arn
}

module "edge" {
  source            = "../modules/edge"
  name              = local.name
  vpc_id            = module.network.vpc_id
  public_subnet_ids = module.network.public_subnet_ids
  certificate_arn   = var.certificate_arn
  kms_key_arn       = module.secrets.kms_key_arn
}

module "data" {
  source                    = "../modules/data"
  name                      = local.name
  vpc_id                    = module.network.vpc_id
  data_subnet_ids           = module.network.data_subnet_ids
  client_security_group_ids = [module.network.app_security_group_id]
  kms_key_arn               = module.secrets.kms_key_arn
  multi_az                  = var.production_sizing
  backup_retention_days     = var.production_sizing ? 30 : 7
  db_instance_class         = var.production_sizing ? "db.r7g.large" : "db.t4g.medium"
  cache_replicas            = var.production_sizing ? 1 : 0
}

module "auth" {
  source        = "../modules/auth"
  name          = local.name
  domain_prefix = var.cognito_domain_prefix
  callback_urls = ["https://${var.app_hostname}/auth/callback"]
  logout_urls   = ["https://${var.app_hostname}/"]
}

module "services" {
  source                = "../modules/services"
  name                  = local.name
  environment           = var.environment
  vpc_id                = module.network.vpc_id
  vpc_cidr              = module.network.cidr
  app_subnet_ids        = module.network.app_subnet_ids
  app_security_group_id = module.network.app_security_group_id
  alb_security_group_id = module.edge.alb_security_group_id
  api_target_group_arn  = module.edge.api_target_group_arn
  web_target_group_arn  = module.edge.web_target_group_arn
  kms_key_arn           = module.secrets.kms_key_arn
  image_tag             = var.image_tag
  secret_arns           = module.secrets.secret_arns
  db_master_secret_arn  = module.data.db_master_secret_arn
  db_endpoint           = module.data.db_endpoint
  redis_url_secret_arn  = module.data.redis_url_secret_arn
  frontend_origins      = "https://${var.app_hostname}"
  cognito_issuer        = module.auth.issuer
  cognito_client_id     = module.auth.client_id
  forwarded_allow_ips   = module.network.cidr
  agents                = local.agents # envs/agents.tf
  desired = var.production_sizing ? {
    web = 2, api = 3, orchestrator = 3, gateway = 2, ingest = 2, guard = 2, clamav = 2
    } : {
    web = 1, api = 1, orchestrator = 1, gateway = 1, ingest = 1, guard = 1, clamav = 1
  }
}

module "ci" {
  source              = "../modules/ci_oidc"
  name                = local.name
  github_repository   = var.github_repository
  github_environment  = var.environment
  ecr_repository_arns = module.services.ecr_repository_arns
  ecs_cluster_arn     = module.services.cluster_arn
  task_role_arns      = module.services.role_arns
}

output "alb_dns_name" { value = module.edge.alb_dns_name }
output "cognito" {
  value = {
    issuer    = module.auth.issuer
    client_id = module.auth.client_id
    domain    = module.auth.hosted_ui_domain
  }
}
output "ecr" { value = module.services.ecr_repository_urls }
output "deploy_role_arn" { value = module.ci.deploy_role_arn }
output "cluster_name" { value = module.services.cluster_name }
output "agent_queue_urls" { value = module.services.agent_queue_urls }
