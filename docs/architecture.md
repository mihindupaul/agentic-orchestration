# Architecture

A request in plain words becomes vials moving on the workcell. Each part does one job and knows only its
neighbours:

```
 host (Mac or Linux)                                                   container or VM
 ─────────────────────────────────────────────────────────────────    ────────────────────────
 agent        request -> Claude (plans only) -> WorkflowSpec (data)
                -> validator (the deck) -> a person approves
                -> compiler -> Orca workflow
 orca_workcell  Orca schedules one thread per vial over the arm
                and the instruments; drivers call the controller
 workcell_controller  one owner of the hardware;  ── TCP ──────────>  workcell_sim (devices)
                deck file: names -> coordinates                        ros2: RViz view, noVNC
```

**Simulator** (`workcell_sim`, stdlib only): a world model (racks, vials, the gripper, the balance pan, station
wells), a queued-motion SCARA arm with inverse kinematics, balance, scanner and heater servers, one TCP port per
device, fault injection and deterministic time scaling. Devices stay simple: the arm takes x, y, z moves and
gripper open/close; the instruments read or heat whatever stands in them. No device knows about bays, wells or
stations. An admin port (sim only) loads racks, reports state and injects faults.

**Controller** (`workcell_controller`), in layers:
- `deck.py` + `decks/workcell.json`: bay/well addresses and station names -> arm coordinates and heights. On real
  hardware these would be taught positions. `workcell_sim/config.py` is the physical truth; a test keeps the two
  in step.
- `operations.py`: labware operations from device commands: `pick("3 A1")`, `place("scanner")`, `scan`, `weigh`,
  `heat` (starts heating and returns), `temperature`, `heater_off`.
- `controller.py`: the service and the one owner of the hardware: one operation per piece of hardware at a time
  (a second request is refused, not queued) and a fault latch (arm fault, lost reply or lost vial: everything is
  refused until `clear_fault`). It makes no decisions about samples.
- `api.py`: newline-delimited JSON on :8700, e.g. `{"cmd": "pick", "where": "3 A1"}` -> `{"ok": true, ...}`.
- `cli.py`: an interactive shell straight to the devices, for learning and debugging.

**Rack jobs are workflows, not controller code.** Which vials, in what order, how many scan retries, what to do
with a vial that will not read: an Orca workflow composes these from the controller's operations.

**Orca plug-in** (`orca_workcell`): the cell as Orca resources. The arm is an Orca transporter (`CellArm`,
teachpoints from the deck file); the code reader, balance and heater are separate Orca devices; rack wells are
plate pads; every vial is labware with a thread of its own, so a vial can sit in the heater while the arm serves
others. `core.py` (Orca-free) maps each controller outcome for Orca: branchable results as data, busy/fault ->
refused, bad request -> rejected, lost vial or arm fault -> failed, lost reply -> unknown. `deployment/run.py`
embeds Orca in-process. Orca itself is not modified.

**Planning agent** (`agent`): Claude, through the SDK's tool runner, looks at the cell and the deck with tools and
hands in a `WorkflowSpec`: processes built from a fixed menu of steps (scan, weigh, heat), per-vial rules,
destination wells, transit wells for swaps and routes decided mid-flight. The model never writes code. Plain code
does everything that has to be right: the validator checks the spec against the live deck, a person approves it,
and the compiler builds the Orca topology and workflow and runs it. Per-vial decisions are made in plain code
inside each vial's thread while it runs; no model is called during a run. A PySide6 window (Describe > Plan > Run) is
the way in.

**Visualisation** (`ros2/`): a ROS 2 node hosts the simulator and publishes the arm, racks, vials and instrument
activity to RViz, viewed in the browser over noVNC. It runs in a container by default, or in a VM. The host side
reaches the simulator only over the device ports, as it would reach real hardware.

More detail: `src/agent/README.md` (the agent), `docs/orca-integration.md` (Orca and lessons learned),
`src/workcell_controller/README.md` (devices, the shell, faults), `docs/simulator-vm.md` (hosting the simulator),
`docs/assumptions.md` (every geometry or protocol guess).
