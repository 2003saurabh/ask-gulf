###############################################################################
# outputs.tf
#
# Exports the provisioned resource identifiers the backend and deploy tooling
# need: the DynamoDB table name, the S3 bucket name, and the relevant role /
# policy ARNs (R17).
###############################################################################

output "dynamodb_table_name" {
  description = "Name of the leads / draft-applications DynamoDB table."
  value       = aws_dynamodb_table.leads.name
}

output "dynamodb_table_arn" {
  description = "ARN of the leads DynamoDB table."
  value       = aws_dynamodb_table.leads.arn
}

output "s3_bucket_name" {
  description = "Name of the documents S3 bucket."
  value       = aws_s3_bucket.documents.id
}

output "s3_bucket_arn" {
  description = "ARN of the documents S3 bucket."
  value       = aws_s3_bucket.documents.arn
}

output "agentcore_execution_role_arn" {
  description = "ARN of the AgentCore Runtime execution role."
  value       = aws_iam_role.agentcore_execution.arn
}

output "agentcore_execution_role_name" {
  description = "Name of the AgentCore Runtime execution role."
  value       = aws_iam_role.agentcore_execution.name
}

output "bedrock_invoke_policy_arn" {
  description = "ARN of the Bedrock InvokeModel policy."
  value       = aws_iam_policy.bedrock_invoke.arn
}

output "dynamodb_access_policy_arn" {
  description = "ARN of the DynamoDB access policy."
  value       = aws_iam_policy.dynamodb_access.arn
}

output "s3_access_policy_arn" {
  description = "ARN of the S3 access policy."
  value       = aws_iam_policy.s3_access.arn
}

output "agentcore_runtime_arn" {
  description = <<-EOT
    ARN of the AgentCore Runtime, when created (deploy_agentcore_runtime=true).
    Empty until a container image is provided and the runtime is deployed.
  EOT
  value = var.deploy_agentcore_runtime ? aws_cloudformation_stack.agentcore_runtime[0].outputs["AgentRuntimeArn"] : ""
}
