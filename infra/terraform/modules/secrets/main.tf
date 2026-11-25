# KMS key for data at rest, and Secrets Manager entries for application secrets.
# Generated secrets (service token, gateway master key) are random. Provider API keys are
# created EMPTY: an operator puts the real values in with the console/CLI; Terraform never
# holds them (ignore_changes on the version).

terraform {
  required_providers {
    aws    = { source = "hashicorp/aws", version = ">= 6.0" }
    random = { source = "hashicorp/random", version = ">= 3.6" }
  }
}

variable "name" { type = string }
variable "provider_secret_names" {
  type    = list(string)
  default = ["GROQ_API_KEY", "TAVILY_API_KEY", "AVIATION_STACK_API_KEY", "OPENWEATHER_API_KEY", "LLM_GATEWAY_KEY"]
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

data "aws_iam_policy_document" "kms" {
  #checkov:skip=CKV_AWS_109:Key policy: account-root admin statement is the AWS default and required
  #checkov:skip=CKV_AWS_111:Key policy: account-root admin statement is the AWS default and required
  #checkov:skip=CKV_AWS_356:In a key policy "*" refers to this key only
  statement {
    sid       = "AccountAdmin"
    actions   = ["kms:*"]
    resources = ["*"] # in a key policy "*" means this key
    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"]
    }
  }
  statement {
    sid       = "CloudWatchLogs"
    actions   = ["kms:Encrypt*", "kms:Decrypt*", "kms:ReEncrypt*", "kms:GenerateDataKey*", "kms:Describe*"]
    resources = ["*"]
    principals {
      type        = "Service"
      identifiers = ["logs.${data.aws_region.current.region}.amazonaws.com"]
    }
    condition {
      test     = "ArnLike"
      variable = "kms:EncryptionContext:aws:logs:arn"
      values   = ["arn:aws:logs:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:log-group:*"]
    }
  }
}

resource "aws_kms_key" "this" {
  description             = "${var.name} data at rest (RDS, cache, secrets, logs)"
  enable_key_rotation     = true
  deletion_window_in_days = 30
  policy                  = data.aws_iam_policy_document.kms.json
}

resource "aws_kms_alias" "this" {
  name          = "alias/${var.name}"
  target_key_id = aws_kms_key.this.key_id
}

resource "random_password" "generated" {
  for_each = toset(["ORCHESTRATOR_TOKEN", "LITELLM_MASTER_KEY"])
  length   = 48
  special  = false
}

resource "aws_secretsmanager_secret" "generated" {
  #checkov:skip=CKV2_AWS_57:Rotation needs a coordinated restart of api+orchestrator / gateway keys; planned (docs/SECURITY.md)
  for_each    = random_password.generated
  name_prefix = "${var.name}/${each.key}-"
  kms_key_id  = aws_kms_key.this.arn
}

resource "aws_secretsmanager_secret_version" "generated" {
  for_each      = random_password.generated
  secret_id     = aws_secretsmanager_secret.generated[each.key].id
  secret_string = each.key == "LITELLM_MASTER_KEY" ? "sk-${each.value.result}" : each.value.result
}

resource "aws_secretsmanager_secret" "provider" {
  #checkov:skip=CKV2_AWS_57:Third-party API keys (Groq, Tavily...) can't be rotated automatically; rotated by operators
  for_each    = toset(var.provider_secret_names)
  name_prefix = "${var.name}/${each.key}-"
  kms_key_id  = aws_kms_key.this.arn
}

resource "aws_secretsmanager_secret_version" "provider_placeholder" {
  for_each      = aws_secretsmanager_secret.provider
  secret_id     = each.value.id
  secret_string = "SET-ME" # replaced out of band; the app treats a missing/placeholder key as "mock"
  lifecycle { ignore_changes = [secret_string] }
}

output "kms_key_arn" { value = aws_kms_key.this.arn }
output "secret_arns" {
  value = merge(
    { for k, s in aws_secretsmanager_secret.generated : k => s.arn },
    { for k, s in aws_secretsmanager_secret.provider : k => s.arn },
  )
}
