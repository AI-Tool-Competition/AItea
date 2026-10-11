.PHONY: setup dev start check backend-check frontend-check build

setup:
	uv sync --project backend --frozen
	cd frontend && npx --yes pnpm@10.26.2 install --frozen-lockfile

dev:
	python3 scripts/dev.py

start:
	python3 scripts/dev.py --production

backend-check:
	cd backend && uv run pytest
	cd backend && uv run ruff check app tests

frontend-check:
	cd frontend && npx --yes pnpm@10.26.2 typecheck
	cd frontend && npx --yes pnpm@10.26.2 exec rstest run tests/unit/core/tea/api.test.ts tests/unit/components/tea/draft-safety.dom.test.tsx

check: backend-check frontend-check

build:
	cd frontend && SKIP_ENV_VALIDATION=1 npx --yes pnpm@10.26.2 build
