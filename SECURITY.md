# Security

This system reads untrusted CI logs, lets a model propose code changes, and can merge them. The design goal is that a
fully compromised model output can, at worst, produce a *rejected proposal*, never an unreviewed merge.

## Trust boundaries

| Input / actor | Trusted? | Why it matters |
|---|---|---|
| CI log text | **No** | Attacker-controlled (any commit author, any dependency). Classic prompt-injection channel. |
| Model output | **No** | Treated as a proposal. Re-validated by deterministic code. |
| Runbook (`runbooks/`, or S3 / Knowledge Base on AWS) | Yes, write-restricted | It is the only source of fix authority. Protect bucket write access. |
| Approver | Yes, authenticated + authorized | OIDC JWT (Cognito, or the dev issuer locally), `approver`/`admin` group. |
| Browser | No | Server re-checks every permission; UI checks are convenience only. |

## Threats and controls

| Threat | Control | Where | Tested |
|---|---|---|---|
| Secret in a log leaks to the model, PR, UI or audit log | Regex + entropy redaction before any model call; token never leaves `RestGitHubClient` | `guard/redact.py`, `temporal/activities.py` | `test_guard.py`, `test_workflow_history_is_ciphertext_and_never_contains_the_token` |
| Prompt injection inside a log ("ignore the runbook, add a backdoor") | Log is declared untrusted; Guardrails `PROMPT_ATTACK` on input; fix must cite a *retrieved* runbook section; edits are minimal and policy-checked | `steps.py`, `temporal/activities.py` | `test_workflow.py` (uncited / out-of-policy fixes never open a PR) |
| Model rewrites more than asked (observed with Nova Pro) | Model returns find/replace edits; **server applies them and computes the diff**; `find` must match once; max 1 removed line | `steps.py::apply_edits` | `test_unsafe_or_unusable_model_output_never_opens_a_pr` |
| Model lies about its own diff | The diff and its hash are computed server-side; model-written diffs are discarded | `temporal/activities.py` | `test_model_diff_is_ignored_server_computes_it` |
| Push to default branch / other repo / sensitive file | Repo allowlist, `ci-fix/` branch prefix, path allowlist, max diff size, checked before any network call | `guard/policy.py`, client | `test_guard.py`, `test_auth_github.py` |
| Unauthorized approval | JWT verified (issuer, client, `token_use`, expiry, signature); group-based permissions | `auth/rbac.py` | `test_auth_github.py` |
| Approving your own run | Separation of duties enforced in the API **and** by the workflow update handler | `api/app.py`, `temporal/workflows.py` | `test_api.py`, `test_bad_decisions_are_denied_and_the_run_keeps_waiting` |
| Approved diff swapped after approval | Approval carries the diff hash; merge is pinned to the approved head SHA | `temporal/workflows.py`, client | `test_bad_decisions_...[hash]`, `test_merge_is_pinned...` |
| Replayed / duplicate approval | Update validator: run must be `awaiting_approval` (409 otherwise) | `temporal/workflows.py` | `test_second_decision_is_refused_by_the_validator` |
| Guardrail outage causes silent bypass | Fails closed; refuses an unpinned `DRAFT` guardrail | `guard/bedrock.py` | `test_guardrail_fails_closed_blocks_and_refuses_draft` |
| Cross-session confusion (AgentCore session isolation) | State lives in Temporal, not in the api process; any api instance sees every run | `api/app.py` | `test_list_and_unknown_run` |
| XSS in the UI | All dynamic text escaped; strict CSP (`script-src 'self'`, no inline script); no tokens in URLs or localStorage | `ui/`, `infra/terraform/web.tf` | manual |
| Repudiation | Audit events (who, what, when) recorded in the workflow history per run, denials included; no log bodies or diffs inside events | `temporal/workflows.py` | `test_full_flow_rbac_and_separation_of_duties` |
| Proxy abuse | Single path, POST only, action allowlist, 16 KB cap, validated session id, no extra headers forwarded; adds no authority | `infra/bff/handler.py` | `test_bff.py` |

| Workflow history exposes CI content | Payloads encrypted with a Fernet key held only by the api and worker; token never enters history | `temporal/codec.py` | `test_workflow_history_is_ciphertext...`, `test_codec_round_trip...` |
| API compromised, token stolen | The api has **no** GitHub token and runs no agent code; only the worker mounts the Docker secret | `docker-compose.yml`, `api/main.py` | compose config |
| Doomed call hammers GitHub | 4xx and allowlist violations are non-retryable; 5xx/429 retry with backoff | `temporal/activities.py` | `test_transient_github_failure_is_retried_without_duplicate_pr` |
| Retried activity duplicates a PR or merge | `open_fix_pr` reuses the branch, files and open PR; `merge_pr` treats already-merged as success | `github/client.py` | `test_open_pr_is_idempotent...` |
| Stuck approval leaves a PR open forever | Approval deadline closes the PR and ends the run as `needs_human` | `temporal/workflows.py` | `test_approval_timeout_closes_pr_and_needs_human` |

## Local demo specifics

- The **dev auth service grants any role to anyone who can reach it.** It refuses to start without `DEV_AUTH=1`, and all ports bind to `127.0.0.1`. Never publish them.
- The codec endpoint (`/codec/*`) decrypts workflow payloads for the Temporal UI. It is enabled only when `CODEC_SERVER=1` and allows only the local Temporal UI origin.
- The sandbox repo is **public**: branches, PRs and CI logs the agent produces there are public. Use a throwaway repo and a fine-grained token scoped to it.
- Temporal's `start-dev` server is single-node and not for production.

## Reporting

Please report vulnerabilities privately to the repository owner rather than in a public issue.

## Known limitations (read before production use)

- The worker's GitHub credential is whatever you put in `.secrets/github_token` (or Secrets Manager on AWS). Use a **fine-grained PAT or GitHub App** scoped to the
  allowlisted repos; rotate it. A broad personal token defeats the repo-level blast-radius limits.
- Guardrails and redaction are probabilistic/pattern based. They reduce risk; the approval gate is the control you rely on.
- Anyone who can write to the runbook bucket can change what the agent is allowed to fix. Restrict it (SSO, branch-protected source of truth, CloudTrail).
- Redaction is best effort for exotic secret formats; add patterns for yours in `guard/redact.py`.
- No WAF in front of CloudFront (AWS profile) and no per-user rate limiting yet.
- Local profile: there is no Guardrails equivalent (Bedrock only). Redaction, the citation check, policy, the 1-removed-line cap and the human gate carry the load.
- A small local model can propose a bad fix. The harness rejects it (the run ends `needs_human`); it cannot make it merge.
- `ALLOW_ADMIN_USER_PASSWORD_AUTH` exists only behind `enable_scripted_auth` (default `false`); keep it off in production.
