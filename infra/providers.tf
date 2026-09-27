###############################################################################
# providers.tf
#
# Terraform + AWS provider configuration.
#
# Local development authenticates via an AWS SSO profile (R16.2): run
# `aws sso login --profile <profile>` and pass `-var aws_profile=<profile>`
# (or set it in a tfvars file). In CI or a deployed context leave aws_profile
# null so the provider uses the default credential chain / role (R16.3).
###############################################################################

terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  # Backend is intentionally left unconfigured here so `terraform init
  # -backend=false` works for local validation. Configure a remote backend
  # (e.g. S3 + DynamoDB lock) before applying in a shared environment.
}

provider "aws" {
  region = var.aws_region

  # profile is only set for local SSO development; null falls back to the
  # default credential chain (env vars, instance/role, AgentCore exec role).
  profile = var.aws_profile

  default_tags {
    tags = merge(
      {
        Project     = var.project_name
        Environment = var.environment
        ManagedBy   = "terraform"
      },
      var.tags,
    )
  }
}
