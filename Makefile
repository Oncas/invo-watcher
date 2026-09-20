# Wrapper around the invo-agent CLI so the same short commands work on the
# Windows checkout and on the VPS, where the venv is not on PATH and the
# config lives at /etc/invo-agent.env instead of ./.env.
#
# Local overrides (VPS_HOST etc.) go in Makefile.local, which is not committed.
-include Makefile.local

SHELL := /bin/bash

# Windows venvs put entry points in Scripts/, POSIX ones in bin/. A checkout
# shared between the two can carry both, so a native .venv-linux wins when the
# Windows .venv/Scripts is present but unrunnable here.
VENV_BIN := $(strip \
  $(if $(wildcard .venv-linux/bin/python),.venv-linux/bin,\
  $(if $(wildcard .venv/Scripts/invo-agent.exe),.venv/Scripts,.venv/bin)))
INVO     := $(VENV_BIN)/invo-agent

# On the VPS the config is /etc/invo-agent.env and must be exported by hand.
# Locally there is no such file and pydantic auto-loads ./.env from the cwd.
ENV_FILE := /etc/invo-agent.env
LOAD     := $(if $(wildcard $(ENV_FILE)),set -a && . $(ENV_FILE) && set +a &&,)

VPS_USER ?= invo
VPS_KEY  ?= ~/.ssh/invo_ci_ed25519
VPS_DIR  ?= /opt/invo-agent

.DEFAULT_GOAL := help
.PHONY: help status dex-status dex-login portfolios logs events once dry-run \
        service restart journal test

help:
	@echo "usage: make <target>    (runs against this checkout)"
	@awk 'BEGIN{FS=":.*##"} \
	  /^##@/{printf "\n%s\n", substr($$0,5); next} \
	  /^[a-zA-Z_-]+:.*##/{printf "  %-14s %s\n", $$1, $$2}' $(MAKEFILE_LIST)
	@echo ""
	@echo "  vps-<target>   run any target above on the VPS over SSH"
	@echo "                 e.g. make vps-dex-status (needs VPS_HOST set)"
	@echo ""

##@ Inspect (read-only, safe against the running agent)

status: ## Config, login state, allowlist, last poll, open copies
	@$(LOAD) $(INVO) status

dex-status: ## Turnkey session expiry and Hyperliquid account equity
	@$(LOAD) $(INVO) dex-status

logs: ## Print resolved log paths, then tail agent.log and events.jsonl
	@$(LOAD) $(INVO) logs -n 40

events: ## Find a ticker across events.jsonl AND rotated history: make events C=ETH
	@test -n "$(C)" || { echo "usage: make events C=ETH"; exit 2; }
	@$(LOAD) LOG_DIR=$${LOG_DIR:-$$HOME/.invo-agent/logs}; \
	  grep -rhi -- "$(C)" "$$LOG_DIR" 2>/dev/null | tail -60 \
	  || echo "no events for $(C) under $$LOG_DIR"

##@ Act (these write state or hit the network)

dex-login: ## Re-auth the wallet via OTP; rewrites dex-session.json (needs a TTY)
	@$(LOAD) $(INVO) dex-login

portfolios: ## Re-sync the allowlist from follows; marks new/dropped portfolios
	@$(LOAD) $(INVO) portfolios

once: ## Poll Involio exactly once and exit, without trading
	@$(LOAD) $(INVO) once

dry-run: ## Poll continuously, logging what it would trade but placing no orders
	@$(LOAD) $(INVO) dry-run

test: ## Run the pytest suite
	@$(VENV_BIN)/pytest -q

##@ Service (VPS only)

service: ## Whether the systemd unit is running, and its last log lines
	@systemctl status invo-agent --no-pager

restart: ## Restart the unit and show its new state (needs sudo)
	@sudo systemctl restart invo-agent && systemctl status invo-agent --no-pager

journal: ## Follow the unit's stdout live; Ctrl-C to stop
	@journalctl -u invo-agent -n 200 -f

C ?=

# make vps-dex-status -> runs `make dex-status` on the box over SSH.
vps-%:
	@test -n "$(VPS_HOST)" || { echo "set VPS_HOST in Makefile.local"; exit 2; }
	@ssh -i $(VPS_KEY) $(VPS_USER)@$(VPS_HOST) 'cd $(VPS_DIR) && make $*'
