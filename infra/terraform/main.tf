data "aws_caller_identity" "current" {}

# ---- Auth: Cognito user pool with RBAC groups ----
resource "aws_cognito_user_pool" "this" {
  name                     = var.name
  mfa_configuration        = "ON"
  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]

  software_token_mfa_configuration {
    enabled = true
  }

  admin_create_user_config {
    allow_admin_create_user_only = true
  }

  password_policy {
    minimum_length = 14
  }
}

resource "aws_cognito_user_group" "roles" {
  for_each     = toset(["viewer", "approver", "admin"])
  name         = each.key
  user_pool_id = aws_cognito_user_pool.this.id
}

resource "aws_cognito_user_pool_client" "ui" {
  name            = "${var.name}-ui"
  user_pool_id    = aws_cognito_user_pool.this.id
  generate_secret = false

  explicit_auth_flows                  = concat(["ALLOW_USER_SRP_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"], var.enable_scripted_auth ? ["ALLOW_ADMIN_USER_PASSWORD_AUTH"] : [])
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_scopes                 = ["openid", "email"]
  supported_identity_providers         = ["COGNITO"]
  callback_urls                        = concat(var.ui_callback_urls, ["https://${aws_cloudfront_distribution.ui.domain_name}/"])
  logout_urls                          = concat(var.ui_callback_urls, ["https://${aws_cloudfront_distribution.ui.domain_name}/"])
  prevent_user_existence_errors        = "ENABLED"
  access_token_validity                = 1
  refresh_token_validity               = 8

  token_validity_units {
    access_token  = "hours"
    refresh_token = "hours"
  }
}

# ---- Runbook bucket (private) ----
resource "aws_s3_bucket" "runbooks" {
  bucket = "${var.name}-runbooks-${data.aws_caller_identity.current.account_id}"
}

resource "aws_s3_bucket_versioning" "runbooks" {
  bucket = aws_s3_bucket.runbooks.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_public_access_block" "runbooks" {
  bucket                  = aws_s3_bucket.runbooks.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# ---- GitHub PAT: empty shell, value is set by hand, never in state or code ----
resource "aws_secretsmanager_secret" "github_pat" {
  name                    = "${var.name}/github-pat"
  description             = "Fine-grained PAT (contents, pull_requests, actions on allowlisted repos only). Rotate every 30 days."
  recovery_window_in_days = 7
}

# ---- Image registry ----
resource "aws_ecr_repository" "this" {
  name                 = var.name
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }
}

# ---- Runtime role: least privilege ----
data "aws_iam_policy_document" "trust" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["bedrock-agentcore.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

data "aws_iam_policy_document" "runtime" {
  statement {
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.github_pat.arn]
  }

  statement {
    actions   = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream", "bedrock:ApplyGuardrail"]
    resources = ["*"]
  }

  statement {
    actions   = ["bedrock:Retrieve"]
    resources = ["arn:aws:bedrock:${var.aws_region}:${data.aws_caller_identity.current.account_id}:knowledge-base/${aws_bedrockagent_knowledge_base.runbooks.id}"]
  }

  statement {
    actions   = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"]
    resources = [aws_ecr_repository.this.arn]
  }

  statement {
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    actions   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["arn:aws:logs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:log-group:/aws/bedrock-agentcore/*"]
  }
}

resource "aws_iam_role" "runtime" {
  name               = "${var.name}-runtime"
  assume_role_policy = data.aws_iam_policy_document.trust.json
}

resource "aws_iam_role_policy" "runtime" {
  role   = aws_iam_role.runtime.id
  policy = data.aws_iam_policy_document.runtime.json
}

# ---- AgentCore runtime, with Cognito JWT as inbound auth ----
resource "aws_bedrockagentcore_agent_runtime" "this" {
  depends_on = [aws_secretsmanager_secret_version.github_pat, aws_iam_role_policy.state]

  agent_runtime_name = replace(var.name, "-", "_")
  role_arn           = aws_iam_role.runtime.arn

  agent_runtime_artifact {
    container_configuration {
      container_uri = "${aws_ecr_repository.this.repository_url}:${var.image_tag}"
    }
  }

  network_configuration {
    network_mode = "PUBLIC"
  }

  request_header_configuration {
    request_header_allowlist = ["Authorization"]
  }

  authorizer_configuration {
    custom_jwt_authorizer {
      discovery_url   = "https://cognito-idp.${var.aws_region}.amazonaws.com/${aws_cognito_user_pool.this.id}/.well-known/openid-configuration"
      allowed_clients = [aws_cognito_user_pool_client.ui.id]
    }
  }

  environment_variables = {
    AWS_REGION             = var.aws_region
    BEDROCK_MODEL_ID       = var.model_id
    GITHUB_REPO_ALLOWLIST  = var.repo_allowlist
    STATE_TABLE            = aws_dynamodb_table.state.name
    GITHUB_TOKEN_SECRET_ID = aws_secretsmanager_secret.github_pat.name
    KNOWLEDGE_BASE_ID      = aws_bedrockagent_knowledge_base.runbooks.id
    GUARDRAIL_ID           = aws_bedrock_guardrail.this.guardrail_id
    GUARDRAIL_VERSION      = aws_bedrock_guardrail_version.this.version
    COGNITO_USER_POOL_ID   = aws_cognito_user_pool.this.id
    COGNITO_DOMAIN         = "${aws_cognito_user_pool_domain.this.domain}.auth.${var.aws_region}.amazoncognito.com"
    COGNITO_CLIENT_ID      = aws_cognito_user_pool_client.ui.id
  }
}

output "ecr_repository_url" {
  value = aws_ecr_repository.this.repository_url
}

output "runbook_bucket" {
  value = aws_s3_bucket.runbooks.bucket
}

output "cognito_user_pool_id" {
  value = aws_cognito_user_pool.this.id
}

output "cognito_client_id" {
  value = aws_cognito_user_pool_client.ui.id
}

resource "aws_secretsmanager_secret_version" "github_pat" {
  secret_id                = aws_secretsmanager_secret.github_pat.id
  secret_string_wo         = var.github_token
  secret_string_wo_version = var.github_token_version
}
