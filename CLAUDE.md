# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository. Personal
notes go in `CLAUDE.local.md` (git ignores it).

## The project
Agentic lab automation, with an imaginary sample-handling workcell designed from scratch for education as the
worked example: a device simulator, a controller, an Orca driver plug-in (so Cheshire Labs Orca orchestrates the
cell unmodified) and an AI planning agent that turns a request in plain words into an Orca workflow. A public,
educational project: code, commits and docs are read by others, so keep them clear and self-explanatory.

## Architecture
@docs/architecture.md

## Where things live
One `pyproject.toml`, packages under `src/`; non-Python parts get their own top-level folder.

| Path | What | Read before changing it |
|---|---|---|
| `src/workcell_sim/` | the device simulator (stdlib only) | `docs/assumptions.md` |
| `src/workcell_controller/` | deck file, operations, the service (`make ctl`), the device shell (`make cli`) | its `README.md` |
| `src/orca_workcell/` | the Orca plug-in: drivers and resources (`cell.py`, `core.py`), Orca embedded (`runtime.py`) | `docs/orca-integration.md` |
| `src/agent/` | the planning agent: `spec.py`, `tools.py`, `planner.py`, `validator.py`, `compiler.py`, `ui.py` | its `README.md` |
| `ros2/` | the simulator's ROS 2 host and RViz view; `docker/` (default) and `vm/` | `docs/simulator-vm.md` |
| `examples/agent_requests/` | example requests as text files (`# expect: plan` or `no plan`) | its `README.md` |
| `tests/` | one folder per package, the real stack in-process | the testing pattern below |

Controller service details: `run(op, **args)` for `home pick place scan weigh tare heat temperature heater_off
rack_map`. A second request for busy hardware -> `ControllerBusy`; arm fault, timeout or lost vial latch a fault
until `clear_fault`; a bad op/address/args -> `InvalidRequest`; an empty well -> `GripperEmpty` (not a fault).

## Commands
- `make install` installs the simulator, controller and pytest into the active Python (e.g. a `.venv`);
  `make orca-install` creates `.venv-orca` with everything: Orca, the agent and its window.
- `make test` runs pytest with `.venv-orca` if present, else `.venv`; tests that need Orca, the SDK or PySide6
  skip when those are missing.
- One test: `python -m pytest tests/controller/test_controller.py::test_arm_fault_latches_until_cleared -q`
  (`pythonpath = ["src"]` is set in `pyproject.toml`).
- Simulator: `make sim-up` (container + RViz) or `make sim` (headless); `make sim-reset` restores the demo deck
  (full racks in bays 3 and 2, empty rack in bay 8) without a restart. `SIM_HOST=<address>` points the targets at
  another simulator (a VM).
- `make ctl` runs the controller service (`CTL_PORT`, default 8700); `make agent-ui` opens the agent's window;
  `make cli` is the device shell (not while `make ctl` runs: one owner of the hardware).
- The agent needs `ANTHROPIC_API_KEY` in `.env` (copy `.env.example`; git ignores `.env`).
- No linter or formatter is configured.

## Testing pattern
Tests run the real stack in-process, no mocks: `Simulator(timing=Timing(time_scale=5000)).start(ports=FREE)`
with every port 0 (ephemeral), then `Controller("127.0.0.1", ports).start()` on the returned ports (see `with_ctl`
in `tests/controller/test_controller.py`). Set up state and faults through `sim.admin({...})`:
- `load_rack` / `remove_rack` (`{"slot", "barcode", "fill": [wells], "cap"}`), `state`, `vial`, `events`,
  `faults`, `rack_map`, `reset`. There is no `layout`/`well_xy`: clients take positions from the deck file. Sim
  tests use the world model directly (`sim.world.well_xy`, `sim.layout.station_xy`).
- `inject` a `fault` from `KNOWN_FAULTS` in `workcell_sim/faults.py` (`scanner_noread`, `heater_timeout`,
  `heater_overshoot`, `balance_unstable`, `gripper_miss`, `gripper_slip`, `arm_collision`) with a `count`.
  Faults are one-shots consumed by the devices; `clear_faults` resets them.
- Agent tests replace the API with a scripted stand-in client (`tests/agent/test_planner.py`): never make billed
  API calls in tests. Window tests run headless (`QT_QPA_PLATFORM=offscreen`).

## Rules
- The workcell is a completely imaginary design made up for education, never a model of a real instrument (no
  "realistic", "based on", "approximate"). Call it the "workcell". Do not name, compare it to or attribute its
  layout or behaviour to any commercial product or manufacturer, and never add third-party screenshots, footage
  or proprietary material.
- Deployment boundary (`docs/simulator-vm.md`): the simulator and RViz run in the container or VM; everything else
  (controller, Orca, the agent) runs on the host and reaches the simulator only over TCP. Host-side code never
  imports `workcell_sim` and takes the simulator address as configuration (default 127.0.0.1).
- Hardware stays simple: no bay, well or station knowledge in device protocols or the simulator's client ports.
  Positions come only from the controller's deck file. The controller never imports simulator internals and may
  use the admin port only for rack maps (`rack_map`).
- Changing the physical layout means changing both `workcell_sim/config.py` (the physical truth) and
  `workcell_controller/decks/workcell.json` (the controller's copy); `tests/controller/test_deck.py` fails until
  they agree.
- Do not modify Orca. Integration goes through `use_device_factory` and a driver provider; keep Orca-facing logic
  in `core.py` and the drivers in `cell.py` thin.
- The agent plans only: the model outputs a `WorkflowSpec` (data, never code), plain code validates and compiles
  it, a person approves it, and no model is called while a plan runs.
- Python >= 3.10; stdlib only for the simulator and the controller. Time-dependent tests use
  `Timing(time_scale=5000)`.
- Every geometry or protocol guess is listed in `docs/assumptions.md`; add yours there.
- Secrets stay in `.env`; never print or commit an API key.

## Git
Normal commits on `main` or a branch; no force-push or history rewrites. Ideas not yet planned: "Possible
improvements" in `README.md`.
