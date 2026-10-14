.DEFAULT_GOAL := help

SYNC ?= auto
API_PORT ?=
WEB_PORT ?=
NAME ?=
PARENT ?= .worktrees
BASE ?=

# Keep every checkout's Python environment and package caches local.
export UV_PROJECT_ENVIRONMENT := $(CURDIR)/backend/.venv
export UV_CACHE_DIR := $(CURDIR)/.dev/uv-cache
export UV_PYTHON_INSTALL_DIR := $(CURDIR)/.dev/python
export npm_config_cache := $(CURDIR)/.dev/npm-cache
export PNPM_STORE_DIR := $(CURDIR)/.dev/pnpm-store
export NEXT_TELEMETRY_DISABLED := 1
export NAME PARENT BASE
ifneq ($(strip $(API_PORT)),)
export TEA_API_PORT := $(API_PORT)
endif
ifneq ($(strip $(WEB_PORT)),)
export TEA_WEB_PORT := $(WEB_PORT)
endif
export TEA_API_URL := http://127.0.0.1:$(if $(strip $(TEA_API_PORT)),$(TEA_API_PORT),8000)
export SYNC

PNPM := npx --yes pnpm@10.26.2

.PHONY: help setup dev dev-foreground dev-stop dev-status dev-list dev-clean dev-check start worktree check backend-check frontend-check build archive-inventory collect-resources

help:
	@printf '%s\n' \
	  'make dev              同步依赖，后台启动前后端并检查健康状态' \
	  'make dev-foreground   前台启动，Ctrl+C 停止本次服务' \
	  'make dev-status       查看当前工作树的服务状态' \
	  'make dev-list         查看本仓库各工作树的服务状态' \
	  'make dev-stop         取消当前工作树的安装/启动，或停止运行服务' \
	  'make dev-clean        停止服务并清理运行日志，不删除业务数据' \
	  'make setup            使用 uv 和 pnpm 同步锁定依赖' \
	  'make dev-check        检查开发启动与工作树工具' \
	  'make check            运行后端、前端类型与茶业务交互检查' \
	  'make build / start    构建 / 启动本地生产预览' \
	  'make archive-inventory COUNTRY=ID  导出资料目录，可选 OWNER=账号ID' \
	  'make collect-resources OUTPUT=data/campaigns/run ORIGINALS=2  并发采集；RESUME=1 续跑' \
	  'make worktree NAME=feature/foo  创建隔离开发工作树' \
	  '选项：SYNC=auto|force|skip，API_PORT=8000，WEB_PORT=3000'

setup:
	python3 scripts/dev.py setup --sync force

dev:
	python3 scripts/dev.py start

dev-foreground:
	python3 scripts/dev.py start --foreground

dev-stop:
	python3 scripts/dev.py stop

dev-status:
	python3 scripts/dev.py status

dev-list:
	python3 scripts/dev.py list

dev-clean:
	python3 scripts/dev.py clean

dev-check:
	uv run --project backend --locked pytest -q backend/tests/test_dev_runner.py backend/tests/test_worktree.py --basetemp=.dev/pytest-dev
	uv run --project backend --locked ruff check --config backend/pyproject.toml scripts backend/tests/test_dev_runner.py backend/tests/test_worktree.py

start:
	python3 scripts/dev.py start --production

worktree:
	python3 scripts/worktree.py

archive-inventory: export TEA_INVENTORY_COUNTRY = $(COUNTRY)
archive-inventory: export TEA_INVENTORY_OWNER = $(OWNER)
archive-inventory:
	cd backend && uv run --locked python -m app.archive_inventory

collect-resources: export TEA_CAMPAIGN_OUTPUT = $(if $(strip $(OUTPUT)),$(abspath $(OUTPUT)))
collect-resources: export TEA_CAMPAIGN_ITEMS = $(ITEMS)
collect-resources: export TEA_CAMPAIGN_ORIGINALS = $(if $(strip $(ORIGINALS)),$(ORIGINALS),0)
collect-resources: export TEA_CAMPAIGN_BATCHES = $(if $(strip $(BATCHES)),$(BATCHES),1)
collect-resources: export TEA_CAMPAIGN_BUDGET = $(if $(strip $(BUDGET)),$(BUDGET),600)
collect-resources: export TEA_CAMPAIGN_RESUME = $(RESUME)
collect-resources: export TEA_CAMPAIGN_EXTEND = $(EXTEND)
collect-resources:
	uv run --project backend --locked python scripts/collect_resources.py

backend-check:
	uv run --project backend --locked pytest backend/tests --basetemp=.dev/pytest
	uv run --project backend --locked ruff check --config backend/pyproject.toml backend/app backend/tests scripts

frontend-check:
	cd frontend && $(PNPM) typecheck
	cd frontend && $(PNPM) exec rstest run tests/unit/core/tea tests/unit/components/tea

check: backend-check frontend-check

build:
	cd frontend && SKIP_ENV_VALIDATION=1 $(PNPM) build
