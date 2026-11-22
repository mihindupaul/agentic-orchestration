# agentic-orchestration

Learning to build agentic orchestration, step by step: software in which workflows, and an AI agent that plans
them, drive machines. The worked example is lab automation: an imaginary sample-handling workcell (SCARA arm,
balance, code scanner, heated well and eight SBS rack bays), designed from scratch, with a simulator, a
controller, an Orca driver plug-in and a planning agent.

```
"scan and weigh row C, skip the heat under 2 g"
        │
planning agent ──(spec, checked, approved)──> Orca ──(driver plug-in)──> workcell controller ──TCP──> devices
  src/agent            compiled at runtime            orca_workcell      src/workcell_controller   src/workcell_sim
  (Claude API)                                                              (Mac)                     (ROS2 + RViz)
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
`src/orca_workcell` presents the workcell to Orca as an arm (a transporter) and three instruments (devices), and
Orca workflows run against the simulator, live, through the controller. Two workflows running together kept every
instrument busy at once: 48 vials scanned, weighed or heated in 14.5 minutes, with all the scanning and weighing
done while the heater worked. See [`docs/orca-integration.md`](docs/orca-integration.md) for how it fits together.

Thanks to Cheshire Labs: Orca's reservation-based scheduling and clear driver interfaces made it possible to
orchestrate this workcell without changing a line of Orca, and its source and examples were the guide for the
plug-in. Orca and cheshire-drivers are source-available under the Server Side Public License (SSPL-1.0); this
repository uses them as dependencies only and includes none of their code.

## The planning agent
`src/agent` turns a request in plain words into a run on the cell. Claude (through the Claude API and the SDK's
tool runner) looks at the cell and the deck with a few tools and hands in a **workflow spec**: data, never code,
built from a fixed menu of steps (scan, weigh, heat) and per-vial rules ("skip the heat for vials under 2 g",
"heat codes starting with 10 to 80 C"). Plain code does everything that has to be right: it checks the spec
against the live deck, a person approves it, and a compiler builds the Orca workflow (one thread per vial) that
runs through the controller. The model is not called while a plan runs. When something is missing, Claude asks
instead of guessing.

A small PySide6 window ties it together for demos: type or load a request, see the plan and the deck check,
approve, then watch Orca's run state and every vial's status, and stop the run if needed. See
[`src/agent/README.md`](src/agent/README.md) for how it works and why, and
[`examples/agent_requests/`](examples/agent_requests/) for requests to try.

## Quick start
Get the simulator running in your browser and let the agent plan and run a job on it.

**You need**
- macOS or Linux, with `git` and `make`
- [Docker](https://docs.docker.com/get-docker/), running: [OrbStack](https://orbstack.dev) or Docker Desktop
  on a Mac
- [uv](https://docs.astral.sh/uv/getting-started/installation/), which also fetches the Python 3.12 that Orca needs
- an [Anthropic API key](https://console.anthropic.com/) for the agent's planning (running a plan costs nothing)

**1. Install** (once)
```
git clone https://github.com/mihindupaul/agentic-orchestration.git
cd agentic-orchestration
make orca-install                 # .venv-orca: Orca, the controller, the agent and its window
cp .env.example .env              # then put your key in .env: ANTHROPIC_API_KEY=sk-ant-...  (git ignores .env)
```

**2. Start the simulator** (terminal 1)
```
make sim-up                       # the simulator and its 3D view in a container; the first build takes a few minutes
make sim-view                     # opens http://localhost:6080/vnc.html in your browser
```
The browser shows the workcell: a full rack in bay 3, an empty rack in bay 8, the arm, the code reader, the
balance and the heater.

**3. Start the controller** (terminal 2, leave it running)
```
make ctl
```

**4. Open the agent** (terminal 3)
```
make agent-ui
```
Click **Load example ▾**, pick `11_plain_move` (or type your own request), then **Plan**. Claude reads the deck
and proposes a plan; check it in the Plan tab, then **Run on the cell** and approve. Watch the arm in the browser
and every vial's status in the Run tab. **Stop** ends a run early.

**Stop everything**
- close the agent window
- terminal 2: `Ctrl-C` stops the controller
- terminal 1: `make sim-down` stops and removes the container (the next `make sim-up` starts with a fresh deck)

**More to try**
- `make test`: the test suite (no API calls, no container needed)
- `make orca-live`: the two hand-written Orca workflows, running on the simulator together (needs steps 2 and 3)
- `.venv-orca/bin/python demos/stage1_simulator/workcell_cli.py`: drive the cell by hand, one command at a time
  (pick 3 A1, place scanner, scan, heat 60, ...), with every wire line shown
- no Docker? `make sim` runs the simulator alone, without the 3D view. For a full Ubuntu VM instead of the
  container, see [ros2/vm/README.md](ros2/vm/README.md).

Tests cover the simulator, the deck file against the simulated cell, labware operations, the controller service
(one operation at a time, fault latch, invalid requests, API) and the CLI. Status of each part:

| Part | State |
|---|---|
| `workcell_sim` | tested |
| `workcell_controller` (deck, operations, service, API) | tested against the simulator |
| `orca_workcell` (drivers, devices, deployment package) | drivers tested against the simulator in Orca 2.3.0's environment (`make orca-test`); workflows run live through the controller to the simulator |
| `agent` (planner, validator, compiler, window) | tested without the API (a scripted stand-in) and against the simulator; plans run live on the simulator |
| `ros2/` | scene tested on the Mac; runs in the container (`ros2/docker`) or the UTM VM (`ros2/vm`) |

## Demo stages
The project is built and presented in stages, one per part of a content series: simulator and controller, Orca
driver, hand-built Orca workflows, then agentic workflows (an AI agent that plans the workflow, plain code that
checks, compiles and runs it).
See [`demos/`](demos/) for the stage plan and runnable demos for each stage.

See `docs/architecture.md`, `docs/orca-integration.md`, `docs/assumptions.md` and `docs/provenance.md`.

## Possible improvements
Directions this could grow in, kept here as a record of what the project does not do yet. None of them is
planned.

1. **Controller reflection.** The controller would describe itself through its API: its devices and their
   operations, the stations, the bays and the deck geometry. The Orca topology (`orca_workcell/cell.py`,
   `layout()`) and the agent's `describe_cell` would then be derived from that description instead of being
   written for this one cell, so a different deck or an extra instrument would need no code change on the Orca
   or agent side.
2. **Evals.** Run the example requests in `examples/agent_requests/` as a set and score the results (the right
   processes, steps and rules, a question where one is expected), to measure prompt and model changes.
3. **Saved plans.** Save an approved spec and run it again later, from the window or the command line, without
   calling the model.
4. **A time estimate before a run**, from operation timings measured on the simulator, shown with the job size.
5. **Recovery after a stop.** A stopped run leaves a vial in an instrument or the gripper; a recovery step would
   find it and put it back.
6. **A richer rule vocabulary**: comparing a vial with the others (above the row average), per-vial
   destinations, well mappings other than "the same well".
7. **Refining an accepted plan** in the window ("make it 50 C"), which the command line can do but the window
   no longer offers.
8. **An Orca device agent**, so plans could also run through Orca's own daemon and command line (Orca's
   WebSocket gateway) rather than only embedded in-process.
9. **An MCP server** over the agent's tools, so other AI clients could plan for the cell too.

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
not affiliated with or endorsed by Cheshire Labs, DOBOT, Anthropic or any other company named here.
