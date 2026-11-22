# Orca integration

Verified against Orca 2.3.0 with cheshire-drivers 1.2.0 (Python 3.12). Orca is not modified.

## Install
Orca wants its own environment: cheshire-drivers installs a fork of PyLabRobot under the name `pylabrobot`.
```
make orca-install      # .venv-orca: Orca from GitHub at v2.3.0, plus this project in editable mode
```
Orca's README marks v2.3.0 as withdrawn while a fixed release is prepared; move the pin in the Makefile when
it lands. Orca and cheshire-drivers are source-available under SSPL-1.0. This repo only depends on them and
does not include their code.

## How the workcell appears in Orca
`src/orca_workcell/cell.py` describes the hardware the way Orca expects:
- `CellArm`: an Orca transporter (`ITransporterDriver`). Orca reserves it only while it moves a vial; a pick or
  place at a taught position becomes a controller pick or place (`bay3_C1` -> `3 C1`, `code_reader` -> `scanner`).
  Teachpoints come from the controller's deck file. Arm controls the cell does not offer (raw coordinates,
  single-axis moves) are refused.
- `CodeReader`, `Balance`, `Heater`: separate Orca devices, each with its own controller connection, and each
  driver offers only its own instrument's operations.
- Rack wells are Orca positions (plate pads), and every vial is labware with its own thread.
- The controller locks per piece of hardware, and every station is a set-down well, so a vial can sit in the
  heater while the arm and the other instruments serve other vials.

Every driver call goes through the Orca-free `CellCore` (`core.py`) to the controller service, and
the outcome becomes the driver error Orca acts on: busy or faulted -> `DriverRefusedError`; a request that can
never work -> `DriverRejectedError`; a lost vial or arm fault -> `DriverError`; a lost reply ->
`DriverOutcomeUnknownError`. Results a workflow can branch on (no-read, heater not yet stable) come back as
data. `SimStationDriver` is the in-memory stand-in Orca uses in `PURE_SIM` runs.

## The deployment package
`src/orca_workcell/deployment/` is an ordinary Orca deployment package: `topology.py` and `workflows/`.
Two workflows run together over the cell: `scan_weigh_rows` (rows C and E: code reader, balance, bay 8) and
`heat_rows` (rows D and F: heater with `heat_c` and `hold_s`, bay 8).
```
make sim-up                                  # or the VM: ros2/vm/deploy.sh
make ctl                                     # controller service on :8700 (SIM_HOST=192.168.64.4 for the VM)
make orca-live                               # both workflows at once: WORKFLOW="scan_weigh_rows heat_rows"
make orca-live WORKFLOW=heat_rows VARS="heat_c=50 hold_s=10"
```
`run.py` embeds Orca in its own process: it builds the system from the package, starts a `SystemRuntime` and
submits the workflows. The drivers are ordinary objects in that process, so no Orca daemon or device gateway is
involved (the Orca CLI's live mode expects devices to connect to its daemon over Orca's own WebSocket protocol).

Every vial thread starts with `MANUAL_PLACE`: in a live run it waits until its vial is registered at its well.
`run.py` does that registration itself when a thread asks for it (the same step as Orca's "I placed it"
remedy); `--no-auto-place` leaves it to an operator. The controller address comes from `CONTROLLER_HOST/PORT`.

Orca builds a route between every pair of positions the arm can reach, so build time grows with the square of the
positions; the topology teaches only rows C-F of bays 3 and 8 (99 positions, about 40 s to build). The tests in
`tests/orca/test_cell.py` call the drivers directly, the way Orca does, so they need no build.

## Lessons
1. A custom `Device` subclass with its own `KIND` is the natural fit; posing as a liquid handler is not needed.
2. A thread that starts with `REUSE_EXISTING` cannot be a workflow entry; use `MANUAL_PLACE`.
3. Register a manually placed rack only once its thread is `AWAITING_MANUAL_PLACE`; registering earlier
   records a second rack and the thread waits forever.
4. In `PURE_SIM` a manual place fills the slot without calling the device's placed hook.
5. This release does not call drivers' `_shutdown` when the runtime stops, so `run.py` closes them; an open
   connection otherwise keeps the controller waiting.
6. Labware types must be in Orca's catalog. There is no 96-vial rack, so the rack is declared as a deep-well
   96 plate (same SBS footprint and grid).
7. Device run-mode overrides only push toward simulation; a live device in a simulated run is not possible.
8. `ctx.param(name, float)` rejects a whole number given for a float variable; read with `float(ctx.param(name))`.
9. A failed action pauses its thread for an operator rather than ending the run, so scripts that wait for a run
   to finish need a timeout.
10. `@orca.action` takes its name from the function, so actions made in a loop are renamed (`__name__`) before
    decorating. An action that needs two racks lists both in `inputs`; the second rack's thread
    `yield orca.join()`s the first one's method.
11. Orca reads a method one step ahead: the code between two `yield`s can run before the step before it has
    finished. A method cannot branch on an action's result; put decisions inside actions.
12. Orca reserves a device for the whole of an action, and every action of a thread needs the thread's
    labware at that action's device (even with `inputs=[]`; otherwise it looks for a route to move it).
    So a wait inside an action blocks every other workflow on that device. Measured with `heat_rows` and
    `scan_weigh_rows` submitted together: 24.7 s, against 24.4 s one after the other: no overlap.
    Real overlap needs the Orca-native model: vials as labware threads, the arm as an Orca transporter,
    and scanner, balance and heater as separate devices, so a vial can sit in the heater while the arm
    serves other vials.
13. A transporter driver must answer `ensure_seeded`, `unseed_position` and `reset_world` (used to keep a
    simulated arm's view of the deck in step); on real hardware they do nothing.
14. Labware type categories are checked: a tube type needs a tube template, and none of the catalog's tube
    types builds in this release, so vials are declared with a plate type.
15. The Orca CLI and daemon run live workflows only on devices that connected to the daemon over Orca's own
    WebSocket protocol (a device agent with a heartbeat); devices whose drivers were built in-process count as
    "not connected". Submitting through the runtime directly, as `run.py` does, needs no gateway. The daemon
    also keeps the topology it loaded at start: restart it after changing the topology.
