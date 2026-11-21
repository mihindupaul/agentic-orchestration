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
- `VialWorkcell` (`src/orca_vialstation/device.py`) is an Orca `Device` with `KIND = "vial_workcell"` and one
  site per rack bay (`bay1` .. `bay8`). Its methods are the controller's operations: `pick`, `place`, `scan`,
  `weigh`, `tare`, `heat`, `temperature`, `heater_off`, `home`, `rack_map`.
- Its drivers implement cheshire-drivers' `BaseDriver`:
  - `VialWorkcellDriver` (live) sends each call through the Orca-free `VialStationCore` to the vialctl
    service. Busy or faulted -> `DriverRefusedError`; a request that can never work -> `DriverRejectedError`;
    a lost vial or arm fault -> `DriverError`; a lost reply -> `DriverOutcomeUnknownError`. Results a
    workflow can branch on (no-read, heater not yet stable, empty well) come back as data.
  - `SimVialWorkcellDriver` is an in-memory stand-in for Orca's `PURE_SIM` runs: no controller needed.
- When Orca places a rack on a bay, `_do_notify_placed` tells the driver, which (in simulation) loads that bay
  in the simulator through its admin port.
- Without a bound provider the device builds its own drivers. Hosted deployments can bind
  `VialWorkcellProvider` (`factory.py`) with `use_device_factory`.

## The deployment package
`src/orca_vialstation/deployment/` is an ordinary Orca deployment package: `topology.py` (one device,
`vial_workcell`) and `workflows/`. `workflows/one_vial.py` is a method of four actions (scan, weigh, heat until
stable, return) on a rack placed in bay 3.
```
make orca-sim          # PURE_SIM: Orca alone, in-memory workcell
make sim-up            # then, for a live run against the simulator:
make ctl               #   controller service on :8700 (separate terminal)
make orca-live         #   the workflow through vialctl to the simulator
```
`workflows/driver_check.py` is the driver's test workflow: it calls every action and checks each result (home,
tare, tube maps, an empty-well pick that must answer with data, scan with retries, weigh, heat with a deadline,
heater off, return), and raises at the end if a check failed so Orca pauses and shows it. Its options are Orca
workflow variables: `well`, `empty_bay`, `scan_tries`, `heat_c`, `heat_deadline_s`.
```
make orca-sim  WORKFLOW=driver_check
make orca-live WORKFLOW=driver_check VARS="heat_c=50 heat_deadline_s=90"
```
`workflows/two_rows.py` is a rack job: every vial in rows A and B of bay 3 is scanned (with retries) and
weighed, then placed in the same well of bay 8. It uses two racks (source in bay 3, destination in bay 8, the
second thread joins the first one's method) and one Orca action per vial (`vial_A1` .. `vial_B12`). A well with
no vial is skipped, a vial that never scans goes back to its own well, and `prepare` refuses to start if any
destination well is taken. `make orca-live WORKFLOW=two_rows`.

To see the branches, inject faults before a live run, e.g. two `scanner_noread` (the scan takes three attempts)
and one `heater_timeout` with a short `heat_deadline_s` (the heater is switched off after the deadline).

In a live run a thread that starts with `MANUAL_PLACE` waits until its rack is registered at its bay. `run.py`
does that registration itself when a thread asks for it (the same step as Orca's "I placed it" remedy);
`--no-auto-place` leaves it to an operator. Addresses come from `VIALCTL_HOST/PORT` and
`SIM_ADMIN_HOST/PORT`.

## Two models: one device, or separate hardware
`orca_vialstation.deployment` treats the whole cell as one Orca device (`VialWorkcell`). It is simple, but
Orca reserves a device for the length of an action, so only one thing happens at a time.

`orca_vialstation.cell_deployment` (`src/orca_vialstation/cell.py`) describes the hardware the way Orca expects:
- `CellArm`: an Orca transporter (`ITransporterDriver`). Orca reserves it only while it moves a vial; a pick or
  place at a taught position becomes a controller pick or place (`bay3_C1` -> `3 C1`, `code_reader` -> `scanner`).
  Teachpoints come from the controller's deck file.
- `CodeReader`, `Balance`, `Heater`: separate Orca devices, each with its own controller connection.
- Rack wells are Orca positions (plate pads), and every vial is labware with its own thread.
- The controller locks per piece of hardware, and every station is a set-down well, so a vial can sit in the
  heater while the arm and the other instruments serve other vials.

Two workflows run together over it: `scan_weigh_rows` (rows C and E: code reader, balance, bay 8) and `heat_rows`
(rows D and F: heater with `heat_c` and `hold_s`, bay 8).
```
make orca-live SIM_HOST=192.168.64.4 PACKAGE=cell WORKFLOW="scan_weigh_rows heat_rows"
```
Orca builds a route between every pair of positions the arm can reach, so build time grows with the square of the
positions; the topology teaches only rows C-F of bays 3 and 8 (99 positions, about 40 s to build).


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
