.PHONY: install test cov lint demo preview image tf-check
install:      ## install dependencies
	uv sync
test:         ## run the test suite
	uv run pytest -q
cov:          ## tests with coverage
	uv run pytest -q --cov=agent --cov-report=term-missing
lint:         ## ruff
	uv run ruff check .
demo:         ## UI in demo mode (sample data, no AWS/GitHub): http://localhost:8777/?demo=1
	cd ui && python3 -m http.server 8777
preview:      ## full local stack with fake LLM/GitHub and a stub login: http://localhost:8765
	PYTHONPATH=. uv run python scripts/ui_preview.py
image:        ## ARM64 image (what AgentCore runs)
	docker build --platform linux/arm64 -t ci-analyzer:local .
tf-check:     ## format + validate Terraform
	cd infra/terraform && terraform fmt -check -recursive && terraform init -backend=false -input=false >/dev/null && terraform validate
