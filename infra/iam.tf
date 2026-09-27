###############################################################################
# iam.tf
#
# IAM roles and policies for the Ask Gulf runtime (R17.3). The runtime needs to:
#   - invoke Bedrock models (Sonnet router + Haiku agents)  -> bedrock:InvokeModel
#   - read/write the ask-gulf-leads DynamoDB table
#   - read/write the ask-gulf-documents S3 bucket
#
# The AgentCore execution role (defined in agentcore.tf) attaches these
# policies so the deployed container gets credentials from the role rather than
# static keys (R16.3, R16.1).
###############################################################################

data "aws_caller_identity" "current" {}

data "aws_region" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  region     = data.aws_region.current.name

  # Bedrock foundation model ARNs the runtime is allowed to invoke.
  bedrock_model_arns = [
    for m in var.bedrock_model_ids :
    "arn:aws:bedrock:${local.region}::foundation-model/${m}"
  ]
}

# --- Bedrock InvokeModel -----------------------------------------------------
data "aws_iam_policy_document" "bedrock_invoke" {
  statement {
    sid    = "BedrockInvokeModel"
    effect = "Allow"
    actions = [
      "bedrock:InvokeModel",
      "bedrock:InvokeModelWithResponseStream",
    ]
    resources = local.bedrock_model_arns
  }
}

resource "aws_iam_policy" "bedrock_invoke" {
  name        = "${var.project_name}-bedrock-invoke"
  description = "Allows invoking the Sonnet router and Haiku agent models."
  policy      = data.aws_iam_policy_document.bedrock_invoke.json
}

# --- DynamoDB table access ---------------------------------------------------
data "aws_iam_policy_document" "dynamodb_access" {
  statement {
    sid    = "LeadsTableAccess"
    effect = "Allow"
    actions = [
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
      "dynamodb:DeleteItem",
      "dynamodb:Query",
      "dynamodb:Scan",
      "dynamodb:BatchGetItem",
      "dynamodb:BatchWriteItem",
    ]
    resources = [
      aws_dynamodb_table.leads.arn,
      "${aws_dynamodb_table.leads.arn}/index/*",
    ]
  }
}

resource "aws_iam_policy" "dynamodb_access" {
  name        = "${var.project_name}-dynamodb-access"
  description = "Read/write access to the ask-gulf-leads table."
  policy      = data.aws_iam_policy_document.dynamodb_access.json
}

# --- S3 bucket access --------------------------------------------------------
data "aws_iam_policy_document" "s3_access" {
  statement {
    sid       = "DocumentsBucketList"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.documents.arn]
  }

  statement {
    sid    = "DocumentsObjectAccess"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
    ]
    resources = ["${aws_s3_bucket.documents.arn}/*"]
  }
}

resource "aws_iam_policy" "s3_access" {
  name        = "${var.project_name}-s3-access"
  description = "Read/write access to the ask-gulf-documents bucket."
  policy      = data.aws_iam_policy_document.s3_access.json
}
