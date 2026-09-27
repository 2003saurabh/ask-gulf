###############################################################################
# agentcore.tf
#
# Amazon Bedrock AgentCore Runtime deploy target + execution role (R17.4, R18.2).
#
# NOTE ON PROVIDER SUPPORT
# ------------------------
# As of the hashicorp/aws provider v5.x there is no first-class
# `aws_bedrockagentcore_runtime` resource — AgentCore is on the provider roadmap
# but not yet released. There IS a CloudFormation resource type
# (AWS::BedrockAgentCore::Runtime), so we model the runtime with a real
# `aws_cloudformation_stack` rather than an inert placeholder. Where a first-class
# resource exists (the IAM execution role, its trust policy, and the attached
# permission policies) we use real Terraform resources directly.
#
# The runtime itself is gated behind `deploy_agentcore_runtime` (default false)
# because it requires a built + pushed container image (from the AgentCore
# packaging task's Dockerfile). Until that image exists, the execution role is
# still created so IAM can be reviewed/provisioned independently, and this file
# documents exactly where the runtime resource attaches.
###############################################################################

# --- AgentCore execution role ------------------------------------------------
# Trust policy: allow the Bedrock AgentCore service to assume this role. The
# deployed container obtains its AWS credentials from this role (R16.3) — no
# static keys are used anywhere (R16.1).
data "aws_iam_policy_document" "agentcore_assume_role" {
  statement {
    sid     = "AgentCoreAssumeRole"
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["bedrock-agentcore.amazonaws.com"]
    }

    # Scope the trust to this account to avoid the confused-deputy problem.
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_iam_role" "agentcore_execution" {
  name               = "${var.project_name}-agentcore-execution"
  description        = "Execution role assumed by the Ask Gulf AgentCore Runtime container."
  assume_role_policy = data.aws_iam_policy_document.agentcore_assume_role.json

  tags = {
    Name = "${var.project_name}-agentcore-execution"
  }
}

# Attach the Bedrock / DynamoDB / S3 permission policies defined in iam.tf so
# the runtime can invoke models and reach its data stores.
resource "aws_iam_role_policy_attachment" "agentcore_bedrock" {
  role       = aws_iam_role.agentcore_execution.name
  policy_arn = aws_iam_policy.bedrock_invoke.arn
}

resource "aws_iam_role_policy_attachment" "agentcore_dynamodb" {
  role       = aws_iam_role.agentcore_execution.name
  policy_arn = aws_iam_policy.dynamodb_access.arn
}

resource "aws_iam_role_policy_attachment" "agentcore_s3" {
  role       = aws_iam_role.agentcore_execution.name
  policy_arn = aws_iam_policy.s3_access.arn
}

# Allow the runtime to write its own CloudWatch logs.
data "aws_iam_policy_document" "agentcore_logs" {
  statement {
    sid    = "AgentCoreLogging"
    effect = "Allow"
    actions = [
      "logs:CreateLogGroup",
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["arn:aws:logs:${local.region}:${local.account_id}:*"]
  }
}

resource "aws_iam_role_policy" "agentcore_logs" {
  name   = "${var.project_name}-agentcore-logs"
  role   = aws_iam_role.agentcore_execution.id
  policy = data.aws_iam_policy_document.agentcore_logs.json
}

# --- AgentCore Runtime -------------------------------------------------------
# Real deploy target via CloudFormation (AWS::BedrockAgentCore::Runtime), gated
# on deploy_agentcore_runtime so validation/plan works before an image exists.
# When the packaging task has pushed the container image, set:
#   deploy_agentcore_runtime = true
#   agentcore_container_uri  = "<account>.dkr.ecr.<region>.amazonaws.com/ask-gulf:latest"
resource "aws_cloudformation_stack" "agentcore_runtime" {
  count = var.deploy_agentcore_runtime ? 1 : 0

  name = "${var.project_name}-agentcore-runtime"

  template_body = jsonencode({
    Resources = {
      AskGulfRuntime = {
        Type = "AWS::BedrockAgentCore::Runtime"
        Properties = {
          AgentRuntimeName = var.agentcore_runtime_name
          RoleArn          = aws_iam_role.agentcore_execution.arn
          # The single-container image built by the AgentCore packaging task.
          AgentRuntimeArtifact = {
            ContainerConfiguration = {
              ContainerUri = var.agentcore_container_uri
            }
          }
          # The runtime speaks HTTP (FastAPI /ws + /upload-document + /health).
          ProtocolConfiguration = "HTTP"
          EnvironmentVariables = {
            DEPLOYMENT_MODE = "agentcore"
            AWS_REGION      = var.aws_region
            DYNAMODB_TABLE  = var.table_name
            S3_BUCKET       = var.bucket_name
          }
        }
      }
    }
    Outputs = {
      AgentRuntimeArn = {
        Value = { "Fn::GetAtt" = ["AskGulfRuntime", "AgentRuntimeArn"] }
      }
    }
  })

  # AWS::BedrockAgentCore::Runtime is only known to CloudFormation, not to the
  # Terraform AWS provider, so validate the artifact wiring before applying.
  lifecycle {
    precondition {
      condition     = var.agentcore_container_uri != ""
      error_message = "agentcore_container_uri must be set when deploy_agentcore_runtime is true."
    }
  }
}
