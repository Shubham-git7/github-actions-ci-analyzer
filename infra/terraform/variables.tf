variable "aws_region" {
  type    = string
  default = "us-west-2"
}

variable "aws_profile" {
  type    = string
  default = "default"
}

variable "name" {
  type    = string
  default = "ci-analyzer"
}

variable "repo_allowlist" {
  type        = string
  description = "Comma-separated owner/repo list the agent may touch."
}

variable "image_tag" {
  type    = string
  default = "latest"
}

variable "model_id" {
  type    = string
  default = "us.anthropic.claude-sonnet-5-5"
}

variable "ui_callback_urls" {
  type        = list(string)
  default     = ["http://localhost:8080/"]
  description = "Allowed OAuth redirect URLs for the UI."
}

variable "github_token" {
  type        = string
  ephemeral   = true
  sensitive   = true
  description = "GitHub PAT, written to Secrets Manager write-only (never stored in state). Pass via TF_VAR_github_token."
}

variable "github_token_version" {
  type        = number
  default     = 1
  description = "Bump to rotate the stored token."
}

variable "enable_scripted_auth" {
  type        = bool
  default     = false
  description = "Allow ADMIN_USER_PASSWORD_AUTH (needs IAM credentials) so test users can be scripted. Keep false in production."
}
