# Architecture

## Components

```mermaid
flowchart LR
  U([Approver / Admin]) -->|HTTPS| CF[CloudFront<br/>static UI + /api/*]
  CF -->|/| S3[(S3: UI, private, OAC)]
  CF -->|/api/invocations| BFF[Proxy Lambda<br/>1 path, action allowlist]
  BFF -->|Bearer JWT, session id| RT
  subgraph AgentCore Runtime
    RT[FastAPI app<br/>JWT verify + RBAC] --> G[LangGraph harness]
  end
  G --> GH[(GitHub API<br/>fine-grained token)]
  G --> BR[Bedrock model + Guardrails]
  G --> KB[(Knowledge Base<br/>runbooks in S3)]
  G --> DDB[(DynamoDB<br/>checkpoints, run index, audit)]
  U -. sign-in .-> COG[Cognito<br/>MFA + groups]
  COG -. JWT .-> U
  RT -. JWKS .-> COG
```

## The harness (one run)

```mermaid
flowchart TD
  A[fetch_logs] --> B[redact secrets]
  B --> C{input guardrail}
  C -- blocked --> H[needs_human]
  C --> D[analyze root cause]
  D --> E[retrieve runbook]
  E -- no match --> H
  E --> F[propose edits]
  F --> G1{citation verified?<br/>edits apply once?<br/>removed lines ≤ 1?<br/>path/repo/size policy?}
  G1 -- no --> H
  G1 --> G2{output guardrail}
  G2 -- blocked --> H
  G2 --> I[open PR on ci-fix/*<br/>server-computed diff + hash]
  I --> J{{interrupt: await_approval}}
  J -- reject --> R[rejected]
  J -- approve --> K{approver ≠ triggerer?<br/>has runs:approve?<br/>hash matches?}
  K -- no --> R
  K -- yes --> L[merge pinned to head SHA + re-run workflow]
```

## Approval sequence

```mermaid
sequenceDiagram
  participant A as Alice (admin)
  participant API as Runtime (session 1)
  participant DB as DynamoDB
  participant B as Bob (approver)
  participant API2 as Runtime (session 2)
  participant GH as GitHub
  A->>API: trigger(repo, run_id) + JWT
  API->>GH: logs, open PR
  API->>DB: checkpoint (awaiting_approval) + audit
  B->>API2: list (own session)
  API2->>DB: read run index
  API2-->>B: run awaiting approval
  B->>API2: decision(approve, diff_hash) + JWT
  API2->>DB: load checkpoint
  API2->>API2: RBAC, not triggerer, hash matches
  API2->>GH: merge @ approved SHA, re-run
  API2->>DB: audit(decision, fix_executed)
```

## Design decisions

| Decision | Reason |
|---|---|
| **Find/replace edits, applied and diffed server-side** | A weaker model rewrote and reformatted whole files. The server, not the model, defines what changed. |
| **State in DynamoDB, not in the runtime** | AgentCore sessions are isolated microVMs bound to one identity; approvals cross users. |
| **One `/invocations` endpoint with actions** | AgentCore forwards only `/invocations` and `/ping`. Same RBAC for every action. |
| **Proxy Lambda instead of calling AgentCore from the browser** | The runtime does not serve CORS; the proxy keeps the UI same-origin and narrows the surface. Auth `NONE` on the function URL is deliberate (CloudFront OAC would overwrite the user's `Authorization` header). |
| **Citation must match a retrieved source or a real `##` heading** | The model cited `## Heading` (with hashes) and plain names; we normalize but still verify against retrieved text. |
| **Fail closed everywhere** | Guardrail error, no runbook match, unparseable model output, policy violation: all end in `needs_human`, never in a PR. |
| **Ephemeral write-only secret in Terraform** | `secret_string_wo` keeps the token out of state. |

## Data

| Store | Keys | Contents |
|---|---|---|
| DynamoDB `*-state` | `CHECKPOINT_*`, `CHUNK_*`, `WRITES_*` (LangGraph) | graph state per run (redacted logs, analysis, proposal, PR) |
| | `PK=RUNS`, `SK=<run id>` | run index |
| | `PK=AUDIT#<run id>`, `SK=<ts>#<nonce>` | audit events (ids, actors, decisions, reasons) |
| S3 runbooks | `*.md` | the only source of fix authority |
