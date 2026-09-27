# Ask Gulf Infrastructure (Terraform)

Provisions the AWS resources the Ask Gulf runtime depends on (R17):

| File            | Resources                                                                 |
|-----------------|---------------------------------------------------------------------------|
| `providers.tf`  | Terraform + AWS provider (local SSO profile via `aws_profile`)            |
| `variables.tf`  | Inputs with defaults matching `backend/config.py`                        |
| `dynamodb.tf`   | `ask-gulf-leads` table (single-table, `pk` string key, on-demand)         |
| `s3.tf`         | `ask-gulf-documents` bucket (private, versioned, encrypted)               |
| `iam.tf`        | Policies for Bedrock `InvokeModel`, DynamoDB, and S3                       |
| `agentcore.tf`  | AgentCore execution role + AgentCore Runtime deploy target                |
| `outputs.tf`    | Table name, bucket name, role/policy ARNs, runtime ARN                    |

Defaults match the runtime config: region `us-east-1`, table `ask-gulf-leads`,
bucket `ask-gulf-documents`.

## Local usage (AWS SSO)

```bash
aws sso login --profile <your-sso-profile>
terraform init
terraform plan  -var aws_profile=<your-sso-profile>
terraform apply -var aws_profile=<your-sso-profile>
```

In CI or a deployed context, omit `aws_profile` so the default credential chain
(env vars / instance role / execution role) is used — no static keys are stored
anywhere (R16.1, R16.3).

## Validate without a backend

```bash
terraform init -backend=false
terraform validate
```

## AgentCore Runtime — note on provider support

The hashicorp/aws provider (v5.x) does **not** yet expose a first-class
`aws_bedrockagentcore_runtime` resource (AgentCore is on the provider roadmap).
CloudFormation *does* have `AWS::BedrockAgentCore::Runtime`, so the runtime is
modelled with a real `aws_cloudformation_stack` in `agentcore.tf` rather than an
inert placeholder. Everything that has a first-class Terraform resource — the
execution role, its trust policy, and the Bedrock/DynamoDB/S3 permission
policies — is created directly.

The runtime resource is **gated** behind `deploy_agentcore_runtime` (default
`false`) because it requires a built and pushed container image. That image is
produced by the AgentCore packaging task (`backend/Dockerfile`). Until it
exists, the execution role is still provisioned so IAM can be reviewed and used
independently.

To deploy the runtime once the image is available:

```bash
terraform apply \
  -var deploy_agentcore_runtime=true \
  -var agentcore_container_uri="<acct>.dkr.ecr.us-east-1.amazonaws.com/ask-gulf:latest"
```
