# Architecture

The model is the least trusted part. Temporal is what makes the whole run durable. Deterministic code decides what may happen.

## Components (local profile, `./start_local.sh`)

```mermaid
flowchart LR
  U([Approver / Admin]) -->|HTTP :8080| API[api<br/>FastAPI: JWT verify + RBAC<br/>serves the UI<br/><b>no GitHub token</b>]
  U -. sign-in, PKCE .-> AUTH[auth<br/>dev OIDC issuer<br/>alice · bob · carol]
  AUTH -. JWKS .-> API
  API -->|start / update / query| T[(Temporal<br/>history, timers,<br/>search attributes)]
  T <-->|poll| W[worker<br/>activities<br/><b>only holder of the token</b>]
  W --> LLM[Ollama]
  W --> GH[(GitHub<br/>public sandbox repo)]
  W --> RB[runbooks/<br/>retriever]
  U -. debug .-> TUI[Temporal UI :8233<br/>payloads decoded locally]
```

Everything binds to `127.0.0.1`. Payloads in Temporal history are encrypted (Fernet); the key lives only in the api and worker.

## One run = one workflow

```mermaid
flowchart TD
  S([trigger: workflow id = fix-owner-repo-runid]) --> A[fetch_logs<br/>redact, input guard]
  A -->|blocked| H[needs_human]
  A --> B[analyze · LLM]
  B --> C[retrieve_runbook]
  C -->|no match| H
  C --> D[propose_fix · LLM, JSON mode]
  D --> E{server verifies<br/>cite · apply once · max 1 removed line<br/>policy · output guard}
  E -->|fail| H
  E --> F[open_pr · idempotent per branch]
  F --> G{{wait for decision<br/>durable, with deadline}}
  G -->|approved by another approver<br/>with the exact diff hash| X[execute_fix<br/>merge pinned to SHA, CI on new head]
  G -->|rejected| Z[close_pr + delete branch]
  G -->|deadline| Z --> H
  X --> OK([executed])
```

Failure policy: transient errors (network, 5xx, 429) retry with backoff inside Temporal. Permanent errors (4xx, allowlist) are non-retryable and end the run as `needs_human`. If a PR already exists, the saga closes it. Guard blocks, policy violations and unusable model output are **results**, not exceptions.

## Approval

```mermaid
sequenceDiagram
  participant B as Bob (approver)
  participant API as api
  participant T as Temporal
  participant W as worker
  B->>API: decision {approved, diff_hash} + Bearer JWT
  API->>API: verify JWT, require runs:approve
  API->>T: update decide(Decision)
  T->>T: validator: run must be awaiting_approval
  T->>T: handler: not the triggerer, diff hash matches, permission present
  alt denied
    T-->>API: denied (recorded in history, run keeps waiting)
    API-->>B: 403
  else accepted
    T-->>API: approved
    T->>W: execute_fix (merge pinned to approved SHA)
    API-->>B: 200 (status approved, then executed)
  end
```

## Design decisions

| Decision | Why |
|---|---|
| Temporal instead of LangGraph checkpoints | Checkpoints save state; nothing resumes a crashed run. Temporal replays the workflow on any worker, retries each step, and times out the human wait. |
| Workflow code has no I/O | Replay must be deterministic. All side effects live in activities, so a retry never repeats a decision. |
| Denied approvals do not end the run | A viewer's mistaken click must not kill a run an approver is about to approve. Every attempt is an audit event. |
| Approval bound to the diff hash, merge pinned to the head SHA | What was reviewed is exactly what merges. |
| "Re-run" dispatches a fresh run on the branch | GitHub's re-run replays the original (broken) commit. Found by the live test. |
| API holds no GitHub token | The api can be exposed to users; only the worker can touch GitHub. |
| Encrypted payloads + codec endpoint for the Temporal UI | History is a data store: it must not hold CI log excerpts in clear text, yet stay debuggable locally. |
| One `/invocations` endpoint | Same contract as AgentCore, so the AWS profile keeps working with the same UI. |
| Dev OIDC issuer, same verifier | The local demo exercises real JWT verification (signature, issuer, client, expiry), not a header toggle. |

## Data

| Where | What | Sensitive? |
|---|---|---|
| Temporal history | step inputs/outputs: redacted log tail, analysis, runbook excerpts, diff, PR info, audit events | Encrypted with `CODEC_KEY` |
| Search attributes | `CiStatus`, `CiRepo`, `CiTriggeredBy` | Plain (used for listing); no content |
| `.secrets/github_token` | GitHub token (Docker secret) | Yes. Mounted only into the worker and the seed helper. |
| `.env` | repo name, model, encryption key | Key is sensitive; gitignored |

## AWS profile (adapters kept, infra not updated)

`BedrockLLM`, `BedrockKBRetriever`, `BedrockGuard` and `CognitoVerifier` are still wired behind the same seams. The Terraform in `infra/terraform` predates Temporal: deploying it needs a Temporal endpoint and a Fargate worker, which this repo does not yet provide.
