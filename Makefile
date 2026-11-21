PY ?= python3
DOCKER ?= docker
COMPOSE = $(DOCKER) compose -f ros2/docker/compose.yaml
VIEW_URL = http://localhost:6080/vnc.html?autoconnect=1&resize=scale
# address of the simulator: 127.0.0.1 for the container or a local run, the VM's address for the UTM VM
SIM_HOST ?= 127.0.0.1

.PHONY: install test sim-up sim-view sim-down sim-logs sim-record sim ctl demo
install:
	$(PY) -m pip install -e ".[dev]"
test:
	$(PY) -m pytest -q
sim-up:         # simulator + RViz in a container (needs Docker: OrbStack or Docker Desktop)
	$(COMPOSE) up -d --build
	@echo "view: $(VIEW_URL)"
sim-view:       # open the RViz view in the browser
	open "$(VIEW_URL)" 2>/dev/null || xdg-open "$(VIEW_URL)"
sim-down:
	$(COMPOSE) down
sim-logs:
	$(COMPOSE) logs -f
sim-record:     # record the view straight from the container display: make sim-record SECS=60 NAME=take1
	ros2/docker/record.sh $(or $(SECS),30) $(or $(NAME),clip)
sim:            # no container, no view: the simulator alone on this machine, 20x faster than real time
	$(PY) -m workcell_sim --time-scale 20 --demo-racks
ctl:            # controller service, API on :8700 (then e.g. python -m vialctl do pick 3 A1)
	$(PY) -m vialctl serve --sim-host $(SIM_HOST)
demo:           # one tube through scan/weigh/heat using the controller's labware operations
	$(PY) examples/run_demo.py --host $(SIM_HOST)
