# Demo stages

The project is built and shown in stages. Each stage is one part of the content series: it has its own
folder here, runs on its own with a couple of commands, and only uses what earlier stages built.
The main code lives in `src/`; this folder is the presenter's view of it.

| # | Stage | Shows | State |
|---|---|---|---|
| 1 | [Simulator and controller](stage1_simulator/) | A virtual workcell whose devices only understand coordinates and simple commands, and the controller layer on the Mac that knows the cell: a deck file turns "bay 3, A1" or "scanner" into coordinates, and pick / place / scan / weigh / heat become device commands. Driven from a CLI that shows every wire line. A small controller service owns the hardware and runs one operation at a time | ready |
| 2 | Orca driver | Orca sees the workcell as a device through a thin driver plug-in, no changes to Orca: one Orca action per controller operation (pick, place, scan, weigh, heat). The driver reports each outcome the way Orca expects (result, refused, rejected, failed, unknown). A first workflow (one vial: scan, weigh, heat until stable) runs in Orca, live against the simulator | built, demo to write |
| 3 | Orca workflows | Rack jobs built by hand as Orca workflows from the station actions, with options (wells, heater temperature and hold time, destination rack), branching (a tube that will not scan goes back to its source) and Orca's recovery: a fault the controller cannot fix pauses the run and an operator decides. Done when hand-built workflows are fully supported and run reliably | planned |
| 4 | Agentic workflows | A planning agent (Claude through the Claude API and the SDK's tool runner) turns a plain-language request into a workflow spec: data from a fixed menu of steps and per-vial rules, never code. Plain code checks it against the live deck, a person approves it, and a compiler builds the Orca workflow that runs on the simulator; the model is not called during the run, and it asks when something is missing. A small PySide6 window shows the plan, the run state and every vial, and can stop the run | built, evals and demo to write |

## Learning goals
The project is also a way to learn by building: refreshing coding skills and learning AI-assisted and agentic
development in the open, one stage at a time. Each stage has a target:

| Stage | What you practise |
|---|---|
| 1 Simulator and controller | TCP protocols, robot kinematics, simulation design, keeping hardware simple and putting the cell's knowledge in a controller layer (deck file, coordinate transforms, labware operations), a service that owns the hardware (one operation at a time, fault latch), testing against real ports |
| 2 Orca driver | Extending an existing framework without modifying it, driver and plug-in patterns, mapping outcomes onto another system's error model, async Python |
| 3 Orca workflows | Workflow orchestration, branching and retries in the workflow rather than the controller, failure policies, recovery decisions and incidents |
| 4 Agentic workflows | Agentic programming: system prompt and tool design, an agent loop (the SDK's tool runner), structured output as the contract, validating AI output in plain code, compiling it to a workflow, human approval before the cell moves, evals |

AI helps write code and text throughout, and the posts say so. Each stage also records what broke and what was
learned (see the `POST.md` in its folder).

The number of stages is not fixed; add, split or reorder rows as the series takes shape.

## Adding a stage
- Folder `stageN_<name>/` with a `README.md`: what the stage shows, how to run it, what to look at.
- Demo scripts only call public interfaces (device ports, controller API, Orca); the reusable code goes in `src/`.
- When a stage is finished, tag the repo `stageN-<name>` so viewers can check out exactly that state.
- Then make its posts with the routine in [CONTENT.md](CONTENT.md) and add a "Posts" line to the stage README.
