PY ?= python3
DOCKER ?= docker
COMPOSE = $(DOCKER) compose -f ros2/docker/compose.yaml
VIEW_URL = http://localhost:6080/vnc.html?autoconnect=1&resize=scale
# address of the simulator: 127.0.0.1 for the container or a local run, the VM's address for the UTM VM
SIM_HOST ?= 127.0.0.1

.PHONY: install test orca-install orca-sim orca-live orca-test sim-up sim-view sim-down sim-logs sim-record sim ctl demo
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
UV ?= uv
ORCA_PY = .venv-orca/bin/python
orca-install:   # separate env for Orca (it installs its own PyLabRobot fork)
	$(UV) venv --python 3.12 .venv-orca
	$(UV) pip install --python $(ORCA_PY) "cheshire-orca @ git+https://github.com/Cheshire-Labs/orca.git@v2.3.0"
	$(UV) pip install --python $(ORCA_PY) -e ".[dev]"
WORKFLOW ?= scan_weigh_rows heat_rows
ORCA_VARS = $(foreach v,$(VARS),--var $(v))
orca-live:      # Orca runs the workflows through vialctl to the simulator (needs the sim and ctl): VARS="heat_c=50"
	VIALCTL_HOST=127.0.0.1 $(ORCA_PY) -m orca_vialstation.deployment.run --mode live $(WORKFLOW) $(ORCA_VARS)
orca-sim:       # ... Orca alone, with in-memory instruments (no simulator, nothing to watch)
	$(ORCA_PY) -m orca_vialstation.deployment.run --mode sim $(WORKFLOW) $(ORCA_VARS)
orca-test:      # the whole suite, including the tests that run inside Orca
	$(ORCA_PY) -m pytest -q
ctl:            # controller service, API on :8700 (then e.g. python -m vialctl do pick 3 A1)
	$(PY) -m vialctl serve --sim-host $(SIM_HOST)
demo:           # one tube through scan/weigh/heat using the controller's labware operations
	$(PY) examples/run_demo.py --host $(SIM_HOST)
