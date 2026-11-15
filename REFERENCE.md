simple-ai-agent-practice
A LangGraph agent platform built in phases: starts as a hello-world agent on local Ollama, grows into multi-agent, RAG, memory, and an AWS deployment.

Working rules
No code without an approved plan. Each phase gets a plan (plan mode) first.
Every decision made goes in this file.
Run everything with uv run. Never commit .env.
Keep the LLM behind get_llm() (src/agent/llm.py) and settings in src/agent/config.py, so swapping local Ollama for an AWS model later is a one-file change.
Environment (verified 2026-11-14)
Ollama 0.40.16 at http://localhost:11434
Installed model: qwen3:latest (the only one)
Python 3.14.6 on the machine. If langgraph/langchain deps fail to install, pin Python 3.12 with uv python pin 3.12.
uv installed at /opt/homebrew/bin/uv
Phase 1 decisions (hello world)
Topic	Decision
Package manager	uv (pyproject.toml + uv.lock)
Interface	CLI: one-shot arg and interactive chat loop
Graph	START -> chat node (ChatOllama) -> END, MessagesState
Layout	src/agent/ package; later agents/, rag/, memory/ modules
Model	qwen3:latest, env OLLAMA_MODEL
Ollama URL	http://localhost:11434, env OLLAMA_BASE_URL
History	In-session only (no persistence yet)
qwen3 <think> output	Final: ChatOllama(reasoning=False) in llm.py; verified no think text.
Python	uv pinned 3.12 (.python-version); deps: langgraph 1.2, langchain-ollama 1.1, python-dotenv; dev: pytest
Files: src/agent/{config,llm,graph,__main__}.py, tests/test_graph.py (fake model, no Ollama needed), .env.example, .gitignore, README.md. build_graph(llm=None) accepts an injected model for tests.

Out of scope for Phase 1: tools, memory/checkpointing, RAG, multiple agents, API server, Docker, AWS.

Phase 1.5 decisions (tools)
Topic	Decision
Tools	get_current_datetime(timezone="local"), web_search(query, max_results=5) in src/agent/tools.py
Web search backend	DuckDuckGo via ddgs (no API key)
Graph	START -> chat -> tools_condition -> tools (ToolNode) -> chat -> END
LLM	get_llm().bind_tools(tools); build_graph(llm=None, tools=TOOLS) for test injection
System prompt	Prepended in the chat node, not stored in state
CLI	Prints [tool] name(args) lines before the final answer; recursion limit 10
Tool errors	Tools return error strings instead of raising, so the graph never crashes
Out of scope	Calculator, memory, RAG, multi-agent, API server, AWS
Phase 1.6 decisions (graph visualization)
Topic	Decision
Output	mermaid.live link (https://mermaid.live/edit#pako:...), printed by the CLI and saved in docs/graphs/<name>.md (link + ```mermaid block)
Trigger	uv run python -m agent viz regenerates all docs; pytest guard fails if committed docs are stale
Scope	GRAPHS registry in src/agent/viz.py; generic helper works for any compiled graph
Detail	Top-level graph (xray=False); use xray=True for graphs with subgraphs
Privacy	Link is built locally (zlib + base64), no network call; PNG via mermaid.ink is out of scope (third party)
Reserved	A one-shot prompt that is exactly viz is treated as the subcommand
Colours (Phase 1.7)
Topic	Decision
Theme	Mermaid dark via frontmatter config (theme + themeVariables) in viz.py constants DARK_FRONTMATTER
Nodes	DARK_NODE_STYLES: default slate/indigo, start green, end rose, visible borders
Edges	linkStyle post-processing in _style_edges: normal solid light-grey, conditional (dashed) amber and thicker; curve basis
Tuning	Change constants in viz.py only, then rerun viz (staleness test enforces regeneration)
Out of scope	Role-based per-node colours, legend, edge labels
Standing rule: every new graph must be added to the GRAPHS registry and docs regenerated with uv run python -m agent viz before finishing.

Phase 1.8 decisions (MCP client: AWS docs via Docker)
Topic	Decision
Client library	langchain-mcp-adapters (MultiServerMCPClient, load_mcp_tools)
Transport	stdio; agent spawns docker run --rm --interactive ... mcp/aws-documentation:latest
Config	mcp_servers.json at repo root, mcpServers format. Loader honours command, args, env, disabled; autoApprove is ignored (all discovered tools can be called without prompts)
Lifecycle	One persistent session per run in src/agent/mcp.py::agent_tools(); container stops on exit
Failure	Docker/image/server failure or 60s timeout -> warning, continue with local tools only
Async	chat node has sync + async impls (RunnableLambda(func, afunc)); CLI runs on asyncio + graph.astream. MCP tools are async-only, so run the graph async whenever MCP tools are loaded
Viz	Registry builds graph with local tools only (offline, deterministic); MCP tools are discovered at runtime and not drawn
Out of scope	Other MCP servers, tool approval prompts, MCP server mode
Phase 4a decisions (RAG + eval)
Corpus: docs/imp/ (currently leave_policy.txt; title typo "TrainWithShuhbham" is in the source doc, not edited).

Topic	Decision
Embeddings	nomic-embed-text via OllamaEmbeddings (ollama pull nomic-embed-text), env EMBED_MODEL
Vector store	Chroma persisted to .chroma/ (git-ignored), telemetry off; accessed only via src/agent/rag/store.py::get_store() (swap point for AWS)
Wiring	Retrieval as a tool search_company_docs(query) in TOOLS; model decides when to call
Chunking	RecursiveCharacterTextSplitter ~600 chars / 80 overlap, doc title prefixed to each chunk, metadata source, chunk
Ingestion	uv run python -m agent ingest [--reset]; idempotent (per-source replace, deterministic IDs)
Retrieval	top_k = 5
Eval	uv run python -m agent eval [--no-rag] [--min-score 0.8]; golden set evals/rag_golden.json, regex checks, no LLM judge
Eval isolation	Eval graph = get_current_datetime + search_company_docs only (no web, no MCP); --no-rag is the baseline
Eval protocol	Golden set is written before the first run and is not weakened to pass; fixes go in prompt/chunking
Reserved CLI words	viz, ingest, eval as the first argument are subcommands, not prompts
Out of scope	Reranking, hybrid search, PDF/DOCX loaders, Bedrock/OpenSearch
Note: "How many leaves do employees get" is ambiguous and needs aggregation (EL 15 + CL 7 + SL 7 = 29 paid days, plus holidays and event leaves); no sentence in the doc gives a total.

Phase 3a decisions (memory: Mem0 + user registration)
Why: the agent guessed who founded TrainWithShubham after being told. It must recall, not guess.

Topic	Decision
Backend	Mem0 platform (hosted) via mem0ai MemoryClient, key MEM0_API_KEY in .env (never printed). Access only via src/agent/memory/store.py (AWS swap point)
Privacy	User chat text is sent to Mem0's cloud (third party). Docs/RAG content is not stored
Identity	Chat loop asks "Who am I speaking with?"; name -> slug user_id in git-ignored users.json (new = registered, known = welcomed back). --user NAME skips the prompt. No passwords
Wiring	Always-on nodes: recall (before chat; Mem0 search, top 5, injected into system prompt) and remember (after final answer; saves the turn). Not tool-dependent, since qwen3 skips tools
Prompt	Strict no-guess: facts about user/people/company only from recalled memories or search_company_docs, else "I don't have that on record". Jarvis tone kept
CLI look	You: prompt, JARVIS: replies
Failure	Mem0 down/no key/timeout -> one warning, continue without memory
Mem0 caveat	add may be asynchronous; a search right after can miss the fact for a few seconds
Eval	agent eval --memory: cross-session recall, other user must not see it, no guessing, RAG still works. Checks are not weakened to pass
Out of scope	Auth, multi-tenant server, self-hosted Mem0, deletion UI, history summarising, AWS
Phase 3b decisions (fixes after first real use, 2026-11-15)
Found in a real chat: (1) the whole first sentence was registered as the user's name; (2) a founder fact told by one user was invisible to another because memory was strictly per user.

Topic	Decision
Name capture	Extract the name from "I am X / my name is X / this is X / call me X" or a bare 1-3 word reply; otherwise ask again. If the first message had more than the name, it is answered as the first question
Scope	Two pools: personal (user_id = the person) and shared company pool (COMPANY_ID). Recall searches both and labels them in the prompt
Routing on save	remember classifies the user's message with the local model: COMPANY / PERSONAL / NONE (questions, greetings, no fact -> not stored). Anything unparseable -> PERSONAL (private by default)
Attribution	Stored text is prefixed with the speaker ("Rohan says: ..."), so facts are not stored as "the user is..." and the answer can cite the source
Trust	No auth, so any user can write to the shared pool; accepted for this practice app. Attribution keeps it auditable. A stricter option (company pool written only by a command) was offered and not chosen
Eval safety	The memory eval swaps config.COMPANY_ID to a throwaway pool (zz-eval-company) and deletes only zz-eval-* ids, so real company memories are never touched
Eval change	Founder-visibility case flips from "other user must NOT see it" to "other user MUST see it" (decision change, documented, not a weakened check). Personal facts must still be invisible to others
Phase 6a decisions (guardrails: Amazon Bedrock ApplyGuardrail)
Model is local Ollama, so Bedrock cannot wrap the call; the standalone ApplyGuardrail API checks text without a model. The console guardrail RunbookOps-gr (id zj38zyw7k6sv, DRAFT only) is another project's and is not modified.

Topic	Decision
Resource	New trainwithshubham-agent-gr via Terraform in infra/terraform/guardrails/; app pins a numbered version, refuses DRAFT. apply only after explicit user OK
Content filters	Hate/insults/sexual/violence/misconduct HIGH both ways; prompt attack HIGH input only; Standard tier, profile us.guardrail.v1:0
Denied topics	Babita Ji (placeholder definition), legal/medical/financial advice, salary/confidential HR data of named people, revealing system prompt. Neutral blocked message
PII	BLOCK secrets (cards, SSN, bank, password, PIN, AWS keys); ANONYMIZE email/phone/address; NAME allowed (registration + memory). Input and output
Grounding	Grounding + relevance 0.7 on output; source = RAG text + recalled memories; skipped with no sources. No Automated Reasoning
Placement	Nodes guard_input (before recall), guard_tools (after tools, untrusted web/MCP/RAG text), guard_output (before remember). Blocked input never reaches the model, recall or Mem0; anonymized text replaces the original
Failure	Fail closed on AWS error ("safety check unavailable"). No guardrail id configured -> off with loud warning
Privacy/cost	Checked text goes to AWS; several calls per turn (per-text-unit pricing). trace never logged
Out of scope	KMS, org enforcement, streaming, local fallback, editing RunbookOps-gr
Phase 5a decisions (Terraform: S3 + EC2)
Style: HashiCorp terraform-style-guide skill (+ its SECURITY.md). Code in infra/terraform/.

Topic	Decision
Provider	hashicorp/aws ~> 6.0, Terraform >= 1.14; region us-west-2, profile default (variables aws_region, aws_profile)
S3	Bucket agentic-ai-for-devops-d (fixed name, no suffix), versioning on, public access fully blocked. No SSE config resource (user choice); S3 still applies default SSE-S3
EC2	Amazon Linux 2023 (SSM parameter AMI), t3.micro, IMDSv2 required, encrypted gp3 root, default VPC, SG with no inbound
Access	No SSH key; connect via SSM Session Manager. IAM role = AmazonSSMManagedInstanceCore + get/put/list on the one bucket
Verified	terraform fmt, init -backend=false, validate only. plan/apply not run
Out of scope	Custom VPC, remote state, KMS, load balancer, deploying the agent
Roadmap
[x] Phase 1: hello-world single-node agent (done 2026-11-14; tests pass, live qwen3 one-shot and multi-turn verified)
[x] Phase 1.5: tools (date/time, web search) (done 2026-11-14; 7 tests pass; live date, search and no-tool prompts verified)
Known limitation: qwen3 (small local model) can misread search snippets (e.g. reported the langgraph-prebuilt version as the LangGraph release). The tool output is correct; treat answers as needing verification. Consider a prompt tweak or bigger model later.
[x] Phase 1.6: graph visualization (mermaid.live) (done 2026-11-14; 10 tests pass incl. staleness guard; docs/graphs/agent.md)
[x] Phase 1.7: dark-theme colours for diagrams (done 2026-11-14; 11 tests pass; curve set via curve_style=CurveStyle.BASIS because langgraph overrides the frontmatter flowchart.curve)
[x] Phase 1.8: MCP client (AWS docs via Docker) (done 2026-11-14; 17 tests pass; live: server tools discovered = read_documentation, read_sections, search_documentation, recommend; search_documentation called for an S3 question; no leftover containers; bad image falls back to local tools with a warning)
Note: the server container may print an INFO "Processing request" log line on stderr despite FASTMCP_LOG_LEVEL=ERROR; harmless.
Image must be pulled once: docker pull mcp/aws-documentation:latest
[x] Phase 4a: RAG over docs/imp + accuracy eval (done 2026-11-14; 36 tests pass; see results below)
Changes beyond the original plan (after measuring): section-aware chunking (split_sections, headings stay with their body; 8 chunks), nomic search_document:/search_query: prefixes (PrefixedEmbeddings), stronger prompt (always search first; search again per item for multi-item questions), extra eval metric context_ok (what the model actually received from the tool).
Eval validity fixes (found by reading answers, not by weakening): the first run showed 85% but the sample answer wrongly said Sick Leave was "not specified" (the check was too lenient: it accepted any "15" and "7"); also the unanswerable regex rejected "not explicitly mentioned", and a retrieval pattern matched a heading stranded in the previous chunk. Checks were tightened/fixed, then everything was re-measured on the unchanged system first.
Results (13 questions, qwen3 + nomic-embed-text, single run each; files in evals/results/):
Run	Answer accuracy	Retriever hit	Model-context hit	Tool-use correct
No RAG (baseline)	8% (1/13)	n/a	n/a	8%
RAG, before fixes (strict checks)	77% (10/13)	91%	73%	92%
RAG, after fixes	85% (11/13)	82%	82%	92%
Sample question "How many leaves ... TrainWithSHubham": passes after fixes (15/7/7 listed, via two searches); it failed before (Sick Leave dropped).
Remaining failures: (1) typo variant: single search, Sick Leave silently omitted; (2) "When is a medical certificate required for sick leave?": qwen3 says "Let me retrieve that" but emits no tool call, then answers/guesses. The model skipping or under-using the tool is the main weakness; raw-question retriever hit fell 91%->82% with 8 smaller chunks (SL chunk ranks below top 5 for "how many leaves").
Next ideas (not done): always-retrieve node or multi-query retrieval for aggregation, bigger top_k/reranking, a larger chat model. Single run per question, so +-1 question of noise is possible.
[x] Phase 3a: memory via Mem0 + user registration + no-guess prompt (done 2026-11-15; 51 tests pass)
Built: src/agent/memory/{store,users,eval}.py; graph is START -> recall -> chat -> (tools -> chat)* -> remember -> END; State adds user_id, user_name, memories; CLI asks for the name, --user, You:/JARVIS:.
Deviation from plan: remember saves only the user's message, not the model's answer (an answer may be a guess and would be stored as fact).
Mem0 SDK 2.2.1 (v3 API): search(query, filters={"user_id":..}, top_k=), add(messages, user_id=), delete_all(user_id=) (filters= is rejected). A stored fact was searchable within ~3s.
Memory eval (agent eval --memory, 7 cases, live Mem0 + qwen3, single run, answers read by hand): 100% (recall 2/2, user isolation 2/2, no-guess 2/2, RAG 1/1). Files: evals/results/memory_after.json.
RAG eval re-run with the new nodes: 92% (12/13; retriever 82%, model-context 91%, tool-use 100%), vs 85% before; the difference is within single-run noise. Typo variant still fails.
3b re-run (7 cases incl. shared company fact visible to another user, personal fact still private): 100%, answers read by hand; evals/results/memory_after_3b.json. 55 tests pass.
Limits: small eval, one run; qwen3 sometimes drops the "JARVIS:" prefix on refusals (the CLI adds it); no auth, so anyone who types a name is that user.
[ ] Phase 2: multiple agents (supervisor/router)
[ ] Phase 3: memory (checkpointer, then long-term store)
[ ] Phase 4: RAG (vector store, embeddings via Ollama)
[ ] Phase 5: AWS deployment (Bedrock or hosted model; container on e.g. AgentCore/ECS/Lambda)
Commands (filled in as built)
Tests: uv run pytest
One-shot: uv run python -m agent "hello"
Chat loop: uv run python -m agent
Graph diagrams: uv run python -m agent viz