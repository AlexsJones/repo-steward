# Repo Steward — simple verbs over the systemd user units install.sh creates.
# Run `make help` for the full list. PORT overrides the dashboard port:
#   make serve PORT=9000
PORT   ?= 8377
SC     := systemctl --user
SVC    := repo-steward.service
TIMER  := repo-steward.timer
DASH   := repo-steward-dash.service
UPTIME := repo-steward-uptime.timer

.DEFAULT_GOAL := help
.PHONY: help install serve start stop restart tick timer-on timer-off status logs open uninstall

help: ## List targets
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[1m%-11s\033[0m %s\n",$$1,$$2}'

install: ## Install/refresh the systemd units + dashboard (runs install.sh)
	./install.sh

serve: ## Run the dashboard in the foreground, no systemd (Ctrl-C to stop)
	STEWARD_PORT=$(PORT) python3 server.py

start: ## Start the dashboard service in the background
	@$(SC) start $(DASH) && echo "dashboard: http://localhost:$(PORT)/"

stop: ## Stop the dashboard service
	$(SC) stop $(DASH)

restart: ## Restart the dashboard service
	$(SC) restart $(DASH)

tick: ## Run one steward tick now (reads ledgers, regenerates dashboard.html)
	@$(SC) start $(SVC) && echo "tick started — follow it with: make logs"

timer-on: ## Enable scheduled ticks
	$(SC) enable --now $(TIMER)

timer-off: ## Pause scheduled ticks (leaves the dashboard up)
	$(SC) stop $(TIMER)

status: ## Show dashboard / tick / timer state
	@printf 'dashboard: %s\n' "$$($(SC) is-active $(DASH))"
	@printf 'tick:      %s\n' "$$($(SC) is-active $(SVC))"
	@printf 'timer:     %s\n' "$$($(SC) is-active $(TIMER))"

logs: ## Tail the tick log
	tail -n 40 -f logs/tick.log

open: ## Open the dashboard in a browser
	@xdg-open http://localhost:$(PORT)/ >/dev/null 2>&1 || echo "open http://localhost:$(PORT)/"

uninstall: ## Disable and stop every steward unit
	-$(SC) disable --now $(TIMER) $(DASH) $(UPTIME) $(SVC)
