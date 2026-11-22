# agentic-orchestration

Learning to build agentic orchestration, step by step: software in which workflows, and later an AI agent, drive
machines. The worked example is lab automation: an imaginary sample-handling workcell (SCARA arm, balance, code
scanner, heated well and eight SBS rack bays), designed from scratch, with a simulator, a controller and an Orca
driver plug-in.

```
Orca ──(driver plug-in)──> vialctl controller ──TCP──> arm | balance | scanner | heater  (simulator)
         orca_vialstation      src/vialctl                  src/workcell_sim
                               (Mac)                         (ROS2 + RViz)
```

The split is deliberate. The devices are kept simple: the arm moves to x, y, z and opens or closes its gripper,
the instruments read or heat whatever is in front of them. The controller on the Mac owns everything that knows
the cell: the deck file (where each bay, well and station is), labware operations such as "pick 3 A1" or "place
scanner", and a small service that owns the hardware and runs one operation at a time. Rack jobs are not in the
controller: they are Orca workflows built from its operations (stages 2 and 3).

## Built on Orca
The workflow layer is [Orca](https://github.com/Cheshire-Labs/orca) by [Cheshire Labs](https://cheshirelabs.io), a lab
automation scheduler and workflow orchestrator, with its driver layer
[cheshire-drivers](https://github.com/Cheshire-Labs/cheshire-drivers). Orca is used unmodified: the plug-in in
`src/orca_vialstation` presents the workcell to Orca as an arm (a transporter) and three instruments (devices), and
Orca workflows run against the simulator, live, through the controller. Two workflows running together kept every
instrument busy at once: 48 vials scanned, weighed or heated in 14.5 minutes, with all the scanning and weighing
done while the heater worked. See [`docs/orca-integration.md`](docs/orca-integration.md) for how it fits together.

Thanks to Cheshire Labs: Orca's reservation-based scheduling and clear driver interfaces made it possible to
orchestrate this workcell without changing a line of Orca, and its source and examples were the guide for the
plug-in. Orca and cheshire-drivers are source-available under the Server Side Public License (SSPL-1.0); this
repository uses them as dependencies only and includes none of their code.

## Quick start
```
python3 -m venv .venv && source .venv/bin/activate   # Python >= 3.10
make install
make test
make sim-up      # simulator + RViz in a container (Docker: OrbStack or Docker Desktop)
make sim-view    # the 3D view in your browser
python demos/stage1_simulator/workcell_cli.py       # pick 3 A1, place scanner, scan, heat 60, ...
make sim-down    # stop it
```
No Docker? `make sim` runs the simulator alone, without the 3D view; the CLI works the same. For a full Ubuntu
VM instead of a container, see [ros2/vm/README.md](ros2/vm/README.md). The controller can also run as a service:
`make ctl`, then e.g. `python -m vialctl do pick 3 A1`.
Tests cover the simulator, the deck file against the simulated cell, labware operations, the controller service
(one operation at a time, fault latch, invalid requests, API) and the CLI. Status of each part:

| Part | State |
|---|---|
| `workcell_sim` | tested |
| `vialctl` (deck, operations, service, API) | tested against the simulator |
| `orca_vialstation` (drivers, devices, deployment package) | drivers tested against the simulator in Orca 2.3.0's environment (`make orca-test`); workflows run live through vialctl to the simulator |
| `ros2/` | scene tested on the Mac; runs in the container (`ros2/docker`) or the UTM VM (`ros2/vm`) |

## Demo stages
The project is built and presented in stages, one per part of a content series: simulator and controller, Orca
driver, hand-built Orca workflows, then agentic workflows (an AI agent that builds and checks Orca workflows).
See [`demos/`](demos/) for the stage plan and runnable demos for each stage.

See `docs/architecture.md`, `docs/orca-integration.md`, `docs/assumptions.md` and `docs/provenance.md`.

## Acknowledgements
Orca builds on the work of the [PyLabRobot](https://github.com/PyLabRobot/pylabrobot) community. The 3D view uses
[ROS 2](https://docs.ros.org) and RViz, and the browser view uses [noVNC](https://github.com/novnc/noVNC).

## Disclaimer
Independent, educational project. The workcell is an imaginary design made up for this project: it does not
represent any real instrument, and its layout, geometry, protocols, timings and behaviour are all invented (see
`docs/assumptions.md`). Not affiliated with or endorsed by any instrument maker, and it contains no proprietary
software, protocols, designs or media.
It is a demonstration, not a real or professional-grade system: it has no safety features and must not be
connected to real hardware or used for any safety-critical purpose.
Licensed under the MIT License, see [LICENSE](LICENSE).

DOBOT and other product names are used only to describe compatibility. This is an independent educational project,
not affiliated with or endorsed by Cheshire Labs, DOBOT or any other company named here.
