.PHONY: install test cov lint demo local local-fast seed down image compose-check tf-check
install:        ## install dependencies
	uv sync
test:           ## run the test suite (starts an in-process Temporal dev server; no Docker, no network once the CLI is cached)
	uv run pytest -q
cov:            ## tests with coverage
	uv run pytest -q --cov=agent --cov-report=term-missing
lint:           ## ruff
	uv run ruff check .
demo:           ## UI only, sample data, nothing else needed: http://localhost:8777/?demo=1
	cd ui && python3 -m http.server 8777
local:          ## the full stack in Docker (Temporal + Ollama + api/UI + worker)
	./start_local.sh
local-fast:     ## same, with a scripted model: no model download
	./start_local.sh --fast
seed:           ## reset the sandbox repo to a broken state and pick the failed run
	./start_local.sh seed
down:           ## stop the stack
	./start_local.sh down
image:          ## the application image
	docker build -t tws-harness:local .
compose-check:  ## validate the compose files with throwaway values
	CODEC_KEY=x DEMO_REPO=o/r docker compose -f docker-compose.yml config -q
	CODEC_KEY=x DEMO_REPO=o/r docker compose -f docker-compose.yml -f compose.ollama.yml config -q
tf-check:       ## AWS profile only: format + validate Terraform
	cd infra/terraform && terraform fmt -check -recursive && terraform init -backend=false -input=false >/dev/null && terraform validate
