# Operations

## Local (the supported way to run this today)

```bash
./start_local.sh            # first run: asks for the sandbox repo + a fine-grained token, pulls the model, starts everything
./start_local.sh --fast     # no model download (scripted model that fixes the demo failure only)
./start_local.sh seed [scenario]   # reset the sandbox repo to broken (missing-build | failing-test | secret-leak | injection), pick the failed run id
./start_local.sh chaos      # kill the worker mid-run, bring it back
./start_local.sh status | logs [service] | down | reset
```

**Token.** Create a fine-grained token at <https://github.com/settings/personal-access-tokens/new>, repository access *only* your sandbox repo,
permissions Contents, Pull requests, Actions and Workflows (read and write), Metadata (read). It is stored in `.secrets/github_token` and mounted
into the worker as a Docker secret. To rotate: delete that file and run `./start_local.sh` again, then revoke the old token on GitHub.

**Model.** `MODEL=` in `.env` (default `qwen2.5-coder:7b`, about 5 GB; `--small` uses the 3B). CPU inference is slow: expect minutes per step, which is
why LLM activities have a 10 minute timeout. NVIDIA users: `--gpu`. Machines that cannot run a model: `--fast`.

**Debugging a run.** Open <http://localhost:8233>, find the workflow `fix-<owner>-<repo>-<run id>`. Every step, retry and the wait for approval is in the
history. The Temporal UI decodes the encrypted payloads through `http://localhost:8080/codec` (local only).

**Approval deadline.** `APPROVAL_TTL_S` (default 24 h). When it passes, the PR is closed and the run ends as `needs_human`.

**State.** Temporal's database is the `temporal-data` volume (survives `down`, removed by `reset`). Ollama models live in `ollama-models`.

**Troubleshooting.** `./start_local.sh logs worker`. A run that ends `needs_human` shows why in the app (policy, guard, no runbook match, GitHub error).
"worker offline" in the app means runs are safe in Temporal and resume when the worker returns.

---

# AWS profile (not updated for Temporal)

The sections below describe the earlier AgentCore deployment. The adapters still exist, but this Terraform does not provision a Temporal endpoint or a worker.

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
