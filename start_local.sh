#!/usr/bin/env bash
# TWS Pipeline Harness: one command to run the whole thing locally (Temporal + Ollama + API/UI + worker) in Docker.
#
#   ./start_local.sh              start everything (first run asks for your GitHub sandbox repo + token)
#   ./start_local.sh --fast       no model download: a scripted model that fixes the demo failure only
#   ./start_local.sh --small      use the 3B model (faster, weaker)        --gpu  use an NVIDIA GPU for Ollama
#   MODEL=<ollama model> ./start_local.sh      pick any other Ollama model
#   ./start_local.sh seed         reset the sandbox repo to a broken state and find the failed run id
#   ./start_local.sh chaos        durability demo: kill the worker mid-run, bring it back, watch the run finish
#   ./start_local.sh status | logs [service] | down | reset
set -euo pipefail
cd "$(dirname "$0")"

CMD="up"; FAST=0; SMALL=0; GPU=0
for a in "$@"; do
  case "$a" in
    up|seed|chaos|status|logs|down|reset) CMD="$a" ;;
    --fast) FAST=1 ;; --small) SMALL=1 ;; --gpu) GPU=1 ;;
    -h|--help) sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) [ "$CMD" = "logs" ] && LOG_SVC="$a" || { echo "unknown argument: $a (try --help)" >&2; exit 2; } ;;
  esac
done
LOG_SVC="${LOG_SVC:-}"

say()  { printf '\033[1;33m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m ok\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

# ---- .env helpers (portable: no bash 4, no GNU sed) -------------------------------------------------
get_env() { [ -f .env ] && awk -F= -v k="$1" '$1==k {sub(/^[^=]*=/,""); sub(/[ \t]+#.*$/,""); print; exit}' .env || true; }
set_env() {
  touch .env
  if grep -q "^$1=" .env; then
    awk -v k="$1" -v v="$2" 'BEGIN{FS=OFS="="} $1==k {print k "=" v; next} {print}' .env > .env.tmp && mv .env.tmp .env
  else printf '%s=%s\n' "$1" "$2" >> .env; fi
}

compose() {
  local files=(-f docker-compose.yml)
  [ "$(get_env LLM_BACKEND)" = "ollama" ] && files+=(-f compose.ollama.yml)
  [ "$GPU" = 1 ] && files+=(-f compose.gpu.yml)
  docker compose "${files[@]}" "$@"
}

preflight() {
  command -v docker >/dev/null || die "Docker is not installed. Get Docker Desktop or Docker Engine: https://docs.docker.com/get-docker/"
  docker info >/dev/null 2>&1 || die "Docker is installed but not running. Start Docker Desktop and retry."
  docker compose version >/dev/null 2>&1 || die "'docker compose' (v2) is required."
  command -v curl >/dev/null || die "curl is required."
  command -v openssl >/dev/null || die "openssl is required (it generates the encryption key)."
}

configure() {
  [ -f .env ] || cp .env.example .env
  # encryption key for Temporal payloads (a valid Fernet key: 32 random bytes, url-safe base64)
  [ -n "$(get_env CODEC_KEY)" ] || set_env CODEC_KEY "$(openssl rand -base64 32 | tr '+/' '-_' | tr -d '\n')"

  # which model
  if [ "$FAST" = 1 ]; then set_env LLM_BACKEND scripted
  else
    set_env LLM_BACKEND ollama
    if [ "$SMALL" = 1 ]; then set_env MODEL "qwen2.5-coder:3b"
    elif [ -n "${MODEL:-}" ]; then set_env MODEL "$MODEL"          # MODEL=llama3.1:8b ./start_local.sh
    elif [ -z "$(get_env MODEL)" ]; then set_env MODEL "qwen2.5-coder:7b"; fi
  fi

  # sandbox repo
  local repo; repo="${DEMO_REPO:-$(get_env DEMO_REPO)}"
  if [ -z "$repo" ]; then
    echo
    echo "The agent needs a PUBLIC sandbox repo that it is allowed to change (it opens PRs there, and merges after approval)."
    echo "Create one first, for example:  gh repo create <you>/ci-sandbox --public"
    read -r -p "Sandbox repo (owner/repo): " repo
  fi
  printf '%s' "$repo" | grep -Eq '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$' || die "'$repo' is not in owner/repo form"
  set_env DEMO_REPO "$repo"

  # token -> Docker secret file (never in .env, never an env var, only the worker container can read it)
  mkdir -p .secrets && chmod 755 .secrets
  if [ ! -s .secrets/github_token ]; then
    local tok="${GITHUB_TOKEN:-}"
    if [ -z "$tok" ]; then
      echo
      echo "A GitHub token is needed ONLY by the worker. Create a fine-grained token at https://github.com/settings/personal-access-tokens/new"
      echo "  Repository access: only $repo     Permissions: Contents RW, Pull requests RW, Actions RW, Workflows RW (Metadata R)"
      read -r -s -p "Paste token (input hidden): " tok; echo
    fi
    [ -n "$tok" ] || die "no token provided"
    ( umask 022; printf '%s' "$tok" > .secrets/github_token )
    chmod 644 .secrets/github_token   # the container user must read it; the directory is what protects it
  fi
  local code
  code="$(curl -s -o /tmp/tws_repo.json -w '%{http_code}' -H "Authorization: Bearer $(cat .secrets/github_token)" \
          -H 'Accept: application/vnd.github+json' "https://api.github.com/repos/$repo" || true)"
  case "$code" in
    200) grep -q '"push": *true' /tmp/tws_repo.json || die "the token can read $repo but not push to it (needs Contents: read and write)";
         grep -q '"private": *false' /tmp/tws_repo.json && ok "$repo is public, token can push" || ok "$repo (private) reachable, token can push" ;;
    401) rm -f .secrets/github_token; die "GitHub rejected the token (401). Re-run to enter a new one." ;;
    404) die "$repo was not found with this token. Check the name, or that the token is scoped to it." ;;
    *)   die "could not verify the token against GitHub (HTTP $code)" ;;
  esac
  rm -f /tmp/tws_repo.json
}

wait_http() { # url seconds label
  local i=0; while ! curl -fsS -o /dev/null "$1" 2>/dev/null; do i=$((i+1)); [ "$i" -gt "$2" ] && die "$3 did not become ready; see: ./start_local.sh logs"; sleep 1; done
}

summary() {
  local rid; rid="$(get_env DEMO_RUN_ID)"
  cat <<MSG

  \033[1;32mTWS Pipeline Harness is running\033[0m

    App (sign in)        http://localhost:8080
    Temporal UI          http://localhost:8233      (payloads are decoded for you via the local codec endpoint)
    Model                $( [ "$(get_env LLM_BACKEND)" = scripted ] && echo "scripted (no LLM)" || echo "$(get_env MODEL) via Ollama" )
    Sandbox repo         https://github.com/$(get_env DEMO_REPO)

  Demo, 2 minutes:
    1. Open the app, Sign in as Alice (admin).
    2. $( [ -n "$rid" ] && echo "Analyze the prefilled run ($rid)." || echo "No failed run yet. Run './start_local.sh seed', then reload." )
    3. Watch the stages, then the Temporal UI (workflow history, retries, the wait for approval).
    4. Sign out, sign in as Bob (approver). Alice cannot approve her own run; Bob can. Approve -> PR merged -> workflow re-run.
    5. Durability: './start_local.sh chaos' kills the worker mid-run; the run still finishes.

  Stop: ./start_local.sh down     Reset state: ./start_local.sh reset
MSG
}

case "$CMD" in
  up)
    preflight; configure
    say "Building the image and starting Temporal, auth and Ollama"
    base=(temporal auth); [ "$(get_env LLM_BACKEND)" = "ollama" ] && base+=(ollama)
    compose up -d --build "${base[@]}"
    if [ "$(get_env LLM_BACKEND)" = "ollama" ]; then
      say "Pulling model $(get_env MODEL) (first run only; several GB)"
      compose run --rm ollama-pull
    fi
    say "Starting api and worker"
    compose up -d api worker
    wait_http http://localhost:8080/ping 90 "the api"
    wait_http http://localhost:8233 60 "the Temporal UI"
    ok "all services are up"
    printf '%b\n' "$(summary)"
    ;;
  seed)
    preflight; [ -f .env ] || die "run ./start_local.sh first"
    compose --profile tools build seed >/dev/null
    out="$(compose --profile tools run --rm --no-deps seed | tee /dev/stderr)"
    id="$(printf '%s\n' "$out" | sed -n 's/^DEMO_RUN_ID=//p' | tail -1)"
    [ -n "$id" ] || die "seeding did not produce a run id"
    set_env DEMO_RUN_ID "$id"
    compose up -d --no-deps api >/dev/null
    wait_http http://localhost:8080/ping 60 "the api"
    ok "sandbox is broken and ready. Failed run id: $id (the app form is prefilled; reload the page)"
    ;;
  chaos)
    preflight
    echo "Durability demo: the worker is killed hard while a run is in flight, then restarted."
    say "Killing the worker now"; compose kill worker
    echo "   In the app, trigger an analysis (or look at one in progress). It stays in Temporal, nothing is lost."
    echo "   Open http://localhost:8233 and find the workflow: it is waiting for a worker."
    read -r -p "Press Enter to bring the worker back... " _
    compose up -d worker; ok "worker restarted: the run resumes exactly where it stopped (see its history in the Temporal UI)"
    ;;
  status) preflight; compose ps ;;
  logs)   preflight; compose logs -f --tail=100 $LOG_SVC ;;
  down)   preflight; compose down; ok "stopped (state kept; './start_local.sh reset' to wipe it)" ;;
  reset)  preflight; compose --profile tools down
          docker volume rm "${COMPOSE_PROJECT_NAME:-tws-harness}_temporal-data" >/dev/null 2>&1 || true
          ok "run history wiped. Downloaded models, .env and .secrets/ are kept (purge everything with: docker compose down -v; delete .secrets/ to forget your token)." ;;
esac
