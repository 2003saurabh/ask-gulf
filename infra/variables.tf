###############################################################################
# variables.tf
#
# Input variables for the Ask Gulf infrastructure. Defaults intentionally match
# backend/config.py (AWS_REGION=us-east-1, DYNAMODB_TABLE=ask-gulf-leads,
# S3_BUCKET=ask-gulf-documents) so a plain `terraform apply` provisions exactly
# what the runtime expects (R17).
###############################################################################

variable "aws_region" {
  description = "AWS region to provision resources in. Matches Config.aws_region."
  type        = string
  default     = "us-east-1"
}

variable "aws_profile" {
  description = <<-EOT
    Local AWS SSO profile used by the AWS provider for local development
    (R16.2). Leave null in CI or deployed contexts where credentials come from
    an IAM role / the AgentCore execution role (R16.3). When null the provider
    falls back to the default credential chain.
  EOT
  type        = string
  default     = null
}

variable "project_name" {
  description = "Project name used for tagging and resource naming prefixes."
  type        = string
  default     = "ask-gulf"
}

variable "environment" {
  description = "Deployment environment label (e.g. dev, demo, prod) used in tags."
  type        = string
  default     = "demo"
}

variable "table_name" {
  description = "DynamoDB table name for leads and draft applications. Matches Config.dynamodb_table."
  type        = string
  default     = "ask-gulf-leads"
}

variable "bucket_name" {
  description = "S3 bucket name for uploaded documents and mock data. Matches Config.s3_bucket."
  type        = string
  default     = "ask-gulf-documents"
}

variable "dynamodb_billing_mode" {
  description = "DynamoDB billing mode. PAY_PER_REQUEST (on-demand) is fine for the demo."
  type        = string
  default     = "PAY_PER_REQUEST"

  validation {
    condition     = contains(["PAY_PER_REQUEST", "PROVISIONED"], var.dynamodb_billing_mode)
    error_message = "dynamodb_billing_mode must be either PAY_PER_REQUEST or PROVISIONED."
  }
}

variable "bedrock_model_ids" {
  description = <<-EOT
    Bedrock model identifiers the runtime is allowed to invoke (Sonnet router +
    Haiku agents). Used to scope the InvokeModel IAM policy. Defaults match
    Config.sonnet_model_id / Config.haiku_model_id.
  EOT
  type = list(string)
  default = [
    "anthropic.claude-3-5-sonnet-20241022-v2:0",
    "anthropic.claude-3-5-haiku-20241022-v1:0",
  ]
}

variable "agentcore_runtime_name" {
  description = <<-EOT
    Logical name for the AgentCore Runtime deployment. Must match the
    AWS::BedrockAgentCore::Runtime name pattern [a-zA-Z][a-zA-Z0-9_]{0,47}
    (letters/digits/underscores only — no hyphens).
  EOT
  type    = string
  default = "ask_gulf_runtime"
}

variable "deploy_agentcore_runtime" {
  description = <<-EOT
    Whether to create the AgentCore Runtime resource. Defaults to false because
    the runtime requires a built container image (produced by the Dockerfile in
    the AgentCore packaging task) that does not exist until the image is pushed.
    Set to true once agentcore_container_uri points at a real image.
  EOT
  type    = bool
  default = false
}

variable "agentcore_container_uri" {
  description = <<-EOT
    ECR container image URI for the single-container runtime (Orchestrator +
    five agents + chaining + session store). Required when
    deploy_agentcore_runtime is true.
  EOT
  type    = string
  default = ""
}

variable "tags" {
  description = "Additional tags applied to all resources."
  type        = map(string)
  default     = {}
}
