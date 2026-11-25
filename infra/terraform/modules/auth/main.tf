# Cognito user pool for the web app: Hosted UI, authorization code + PKCE (public client, no
# secret), groups `admin`/`member`, and a custom tenant attribute. The API verifies access
# tokens against this pool's JWKS (services/api/auth.py).
# Access tokens only carry custom attributes through a pre-token-generation trigger (V2); until
# that Lambda is added each user is their own tenant (auth.py falls back to user:<sub>).

terraform {
  required_providers {
    aws = { source = "hashicorp/aws", version = ">= 6.0" }
  }
}

variable "name" { type = string }
variable "domain_prefix" { type = string }
variable "callback_urls" { type = list(string) }
variable "logout_urls" { type = list(string) }
variable "mfa" {
  type    = string
  default = "OPTIONAL"
}

resource "aws_cognito_user_pool" "this" {
  name                     = var.name
  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]
  mfa_configuration        = var.mfa
  deletion_protection      = "ACTIVE"

  software_token_mfa_configuration { enabled = true }

  password_policy {
    minimum_length                   = 12
    require_lowercase                = true
    require_uppercase                = true
    require_numbers                  = true
    require_symbols                  = false
    temporary_password_validity_days = 3
  }

  user_pool_add_ons { advanced_security_mode = "ENFORCED" }

  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }

  schema {
    name                = "tenant_id"
    attribute_data_type = "String"
    mutable             = false
    string_attribute_constraints {
      min_length = 1
      max_length = 200
    }
  }
}

resource "aws_cognito_user_pool_domain" "this" {
  domain       = var.domain_prefix
  user_pool_id = aws_cognito_user_pool.this.id
}

resource "aws_cognito_user_pool_client" "web" {
  name                                 = "${var.name}-web"
  user_pool_id                         = aws_cognito_user_pool.this.id
  generate_secret                      = false # public SPA client; PKCE protects the code exchange
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = ["openid", "email"]
  supported_identity_providers         = ["COGNITO"]
  callback_urls                        = var.callback_urls
  logout_urls                          = var.logout_urls
  prevent_user_existence_errors        = "ENABLED"
  enable_token_revocation              = true
  access_token_validity                = 60
  id_token_validity                    = 60
  refresh_token_validity               = 7
  token_validity_units {
    access_token  = "minutes"
    id_token      = "minutes"
    refresh_token = "days"
  }
  explicit_auth_flows = ["ALLOW_REFRESH_TOKEN_AUTH", "ALLOW_USER_SRP_AUTH"]
}

resource "aws_cognito_user_group" "role" {
  for_each     = toset(["admin", "member"])
  name         = each.key
  user_pool_id = aws_cognito_user_pool.this.id
  description  = each.key == "admin" ? "Tenant admin: manage documents, read tenant plans" : "Traveller"
}

data "aws_region" "current" {}

output "user_pool_id" { value = aws_cognito_user_pool.this.id }
output "client_id" { value = aws_cognito_user_pool_client.web.id }
output "issuer" { value = "https://cognito-idp.${data.aws_region.current.region}.amazonaws.com/${aws_cognito_user_pool.this.id}" }
output "hosted_ui_domain" { value = "https://${var.domain_prefix}.auth.${data.aws_region.current.region}.amazoncognito.com" }
