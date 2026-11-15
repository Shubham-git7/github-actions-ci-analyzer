# Operations

## Deploy

```bash
# 1. image (ARM64 is required by AgentCore)
ECR=$(terraform -chdir=infra/terraform output -raw ecr_repository_url)
docker build --platform linux/arm64 -t $ECR:vN . && docker push $ECR:vN       # tags are immutable
# 2. infra (the GitHub token is write-only: never stored in state)
cd infra/terraform
TF_VAR_github_token="<fine-grained PAT>" terraform apply \
  -var repo_allowlist=owner/repo -var image_tag=vN -var model_id=<bedrock inference profile id>
```

Order matters on first deploy: create the ECR repo first (`-target=aws_ecr_repository.this`), push the image, then apply the rest.

## Runbook content

Upload Markdown to the runbook bucket (headings `## ...` are the citable sections), then re-ingest:

```bash
aws s3 cp runbooks/ci-fixes.md s3://$(terraform output -raw runbook_bucket)/
aws bedrock-agent start-ingestion-job --knowledge-base-id $(terraform output -raw knowledge_base_id) --data-source-id <id>
```

A fix that does not cite a retrieved section is never opened as a PR.

## Users and roles

```bash
uv run python scripts/create_user.py $(terraform output -raw cognito_user_pool_id) person@example.com approver
```

Roles: `viewer` read; `approver` read + approve; `admin` read + approve + trigger. Nobody can approve a run they triggered.

## Rotate the GitHub token

Create a new fine-grained PAT (Contents, Pull requests, Actions: read/write; Metadata: read; only allowlisted repos; short expiry),
then `TF_VAR_github_token=<new> terraform apply -var github_token_version=<n+1> ...`. Restart is automatic (runtime update).

## Change the model

`-var model_id=us.anthropic.claude-sonnet-5-5` (any Converse-capable inference profile). Verify access first: some accounts need a
Marketplace payment instrument for Anthropic models. If a model rewrites more than asked, the policy rejects it; it does not merge.

## Observe

| Question | Where |
|---|---|
| What happened to run X? | UI "Activity", or `audit` action, or DynamoDB `PK=AUDIT#X` |
| Why was a fix rejected? | the run's `error` (e.g. `fix removes 4 lines…`, `does not cite…`) |
| App errors | CloudWatch log group `/aws/bedrock-agentcore/runtimes/<name>-DEFAULT` (structured `{"audit": ...}` lines included) |
| Proxy errors | `/aws/lambda/<name>-bff` |

Suggested alarms (not created by Terraform yet): Lambda 5xx, runtime 5xx, spike in `needs_human`, any `self_approval_blocked`.

## Incident: suspected token or runbook compromise

1. Revoke the GitHub PAT (GitHub settings). The agent can no longer touch repos.
2. `aws cognito-idp admin-disable-user` for suspicious accounts; `admin-user-global-sign-out`.
3. Block writes to the runbook bucket; review CloudTrail and S3 versioning for runbook changes.
4. Review audit events for `decision` entries in the window.

## Cost drivers

Bedrock tokens per analysis, Knowledge Base ingestion/queries, DynamoDB on-demand, CloudFront/Lambda (negligible at low volume), Secrets Manager.

## Tear down

`terraform destroy -var repo_allowlist=x -var github_token=x`, then delete the S3 buckets' contents if versioning blocks deletion.
