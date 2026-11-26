# The planning agent

A person describes vial work in plain words; the agent turns it into a **workflow spec** that plain code checks,
compiles into an Orca workflow and runs after a person approves it. The model plans; it never runs anything,
and it is not called while a plan runs.

```
request ──> planner ──> WorkflowSpec ──> validator ──> approval ──> compiler ──> Orca ──> controller ──> cell
            (Claude + tools)  (data)       (the deck)    (a person)   (plain code)
```

Built step by step:

| Step | Part | State |
|---|---|---|
| 1 | Planner: system prompt + tools + the SDK's tool runner -> `WorkflowSpec` | done |
| 2 | Compiler: `WorkflowSpec` -> Orca workflow built at runtime, with per-vial rules | done |
| 3 | Validator: the spec against the live deck, errors Claude can fix | done |
| 4 | Approval and execution on the simulator (`--run`, the window's Run step) | done |
| 5 | Qt window: request, plan, questions, run, status, stop | done |
| 6 | Evals: the example requests run as a set, to measure prompt and model changes | next |

| File | What it does |
|---|---|
| `spec.py` | the `WorkflowSpec`: the only shape a plan can take (processes, steps from a fixed menu, rules) |
| `inventory.py` | where the deck state comes from: the live controller, or a fixed demo deck |
| `tools.py` | what Claude may call while planning: `describe_cell`, `rack_inventory`, `submit_workflow`, `mark_plan_ready` |
| `planner.py` | the Claude API call (the SDK's tool runner), the checks on its result, the conversation |
| `validator.py` | the spec against the deck: racks present, vials there, destination wells free or emptied, no sharing, transit wells for swaps |
| `compiler.py` | spec -> Orca topology and workflow, one thread per vial; evaluates the rules; runs it |
| `ui.py` | the window (PySide6) |
| `__main__.py` | the command line |
| `prompts/system_prompt.md` | the system prompt |
| `examples.py` | loads the example requests in [`examples/agent_requests/`](../../examples/agent_requests/) |

## How an agent like this works

**The model never acts; it asks.** Claude only ever answers with text or with a request to call a tool. Our
code decides whether to run it, runs it, and sends the result back. Everything that can move hardware stays
in our code.

**Three things shape what Claude does**, and all three are sent with every request:

- the **system prompt** (`prompts/system_prompt.md`): the job, the cell in brief, how to plan, and when *not*
  to submit. It describes the job and the reasons, not a script of every case.
- the **tools** (`tools.py`): each one is a name, a description and an input schema. Claude chooses a tool by
  its description, so the description says what the tool is for and when to use it. We decide which tools
  exist: here, what Claude needs to know (`describe_cell`, `rack_inventory`), how it hands in its result
  (`submit_workflow`) and how it says the result is final (`mark_plan_ready`).
- the **request**: the person's words, as the first user message.

**The tool runner is the agent loop** (`client.beta.messages.tool_runner`): send the request, run the tools
Claude asks for, send the results back, repeat until Claude answers without a tool call. It is a beta helper
in the regular `anthropic` SDK (not the separate Claude Agent SDK).

**The output is data with a schema.** `submit_workflow`'s input schema is the `WorkflowSpec` Pydantic model,
so the same class describes the spec to Claude, checks what Claude sends, and is what the compiler reads. A
spec that does not fit is rejected with the exact field errors, and Claude corrects it.

**The deck is checked in plain code.** `submit_workflow` also runs the validator: a missing rack, an empty row,
a destination well already taken or two processes sharing wells come back to Claude as errors it can fix. Empty
wells inside a row are warnings: those wells are skipped, and the compiler builds threads only for wells that
hold a vial. The deck is checked once more just before a run, since it may have changed since planning.

**Logic is data too.** "Do not heat vials under 10 g" or "heat codes starting with 10 to 80 C" become *rules*
on a step: one test on a value an earlier step measured (`grams < 10`, `code starts with "10"`), and an
outcome from a fixed list (skip the step, or run it with changed values). The spec checks that a rule only
tests something measured before it. While the plan runs, each vial's thread waits until its earlier steps
have finished (an event the previous action emits), then decides in plain code, before the vial moves: a vial
that skips the heat never goes to the heater. The same goes for where a vial ends up: *routes* on a process ("over
1 g to bay 8, otherwise back to its own well") are decided once its steps are done, and the vial is parked in the
chosen well; the thread may end in any of them (Orca takes a list of end candidates). Growing what the agent can express means adding a test or an
outcome in code, once, for every plan after it.

**Vials go where the person says.** A destination is a bay (the source bay too) and the same wells, other rows,
other columns or chosen wells, paired with the source in order. A destination well may hold a vial now if the
plan moves that vial away: Orca does not put a vial into a well until the one there has been picked, and a vial
whose destination is still full waits, before its first step, until that vial has left (otherwise the two could
block each other at an instrument). A swap ("row A with row H") is a cycle no waiting can solve: one vial of each
cycle has to stand somewhere else first. Claude picks those **transit wells** from `rack_inventory` (empty, and
used by nothing else in the plan) and lists them in the spec; the validator checks them and gives one to each
cycle, and that vial parks there (`orca.park`) before its steps. Without enough transit wells the spec comes back
to Claude with the cycles named.

**The compiler is plain code** (`compiler.py`): the spec's bays and wells become the Orca topology, each vial a
thread from its source well to its destination well, each step an action at its instrument.
The plan of the two hand-written workflows compiles to the same vial journeys (tested). When a plan heats to
more than one temperature, the heater cools to the lowest one before the next vial goes in, and it is switched
off when the run ends.

**A plan runs only once Claude marks it ready.** An accepted spec is not yet a plan to run: Claude calls
`mark_plan_ready` when the spec does what the person asked and nothing is left for them to decide. Only then
does the window offer **Run** (and the command line `--run`). An accepted spec that is not marked ready means
Claude wants an answer first, and the window asks it in the popup; a new submission clears the mark.

**Claude may ask instead of planning.** When something is missing (no temperature given) or the cell cannot
do what was asked, Claude replies without a spec. The window shows the reply in a popup with an answer box:
OK continues the same conversation (Claude sees every earlier turn and plans again), Abort stops planning. The
command line asks the same question in the terminal.

**Check how the reply ended before trusting it** (`planner.py`): `refusal` (declined), `max_tokens` (cut off),
or still `tool_use` after the iteration limit are errors, not plans.

## Settings and why

- `claude-sonnet-5-5` with adaptive thinking: Claude decides how much to think. Effort `high`. Sonnet costs half
  as much per token as Opus 5.5, and planning against a fixed menu of steps is well within its reach; switching
  model is one line (`MODEL` in `planner.py`).
- No forced tool choice: Sonnet 5.5 (and Opus 5.5) reject it. The system prompt says when to submit, and when not to.
- Server-side fallback (`fallbacks="default"`, beta `server-side-fallback-2026-07-01`): if a safety classifier
  declines a request, the API re-runs it on another model in the same call instead of just stopping.

## Try it

```
make orca-install                     # .venv-orca with Orca, the agent and the window (PySide6)
cp .env.example .env                  # then put your ANTHROPIC_API_KEY in .env (git ignores it)
make sim-up   # or the VM: ros2/vm/deploy.sh
make ctl      # the controller service (SIM_HOST=192.168.64.4 for the VM)

.venv-orca/bin/python -m agent.ui                                    # the window
.venv-orca/bin/python -m agent --example 03 --offline                # plan an example request, demo deck
.venv-orca/bin/python -m agent "scan and weigh row C of bay 3, then bay 8" --run   # plan, approve, run
```

**The window** walks through three steps, one page each, with a step bar on top (**1 Describe › 2 Plan › 3 Run**):

1. **Describe**: type a request or load an example, then **Start planning**. Claude's replies show in the
   conversation below, in order with each tool call it makes (planning is the only time Claude is involved; the
   Run step's feed is Orca's alone); a question comes up in a popup (OK answers and continues, Abort stops).
2. **Plan**: opens by itself once Claude marks a plan ready: the plan as a tree (steps, rules, routes), the job
   size and the deck check. Not right? Click **Describe** in the step bar, change the request and plan again;
   right: **Go to run**. The step bar switches between the steps at any time.
3. **Run**: **Run on the cell** asks for approval, checks the deck once more and runs; Orca's phase, every vial's
   status (code, grams, temperature, the rule or route that decided) and a feed of what Orca does. After a
   completed run, **Run again** runs the same plan once more (a swap swaps back); the deck is checked first, so a
   plan that no longer fits is refused with the reason. **Stop** uses Orca's operator stop and switches the heater
   off; a vial in an instrument or the gripper at that moment stays there.

The **⚙** at the top right sets the controller's host and port and **Demo deck** (plan without the controller;
such a plan can be reviewed but not run). The pill next to it shows every few seconds whether the controller
answers: green connected, amber with a fault latched, red not connected, grey demo deck.

**Example requests** live as text files in [`examples/agent_requests/`](../../examples/agent_requests/):
`python -m agent --list`, then `python -m agent --example 03 --offline`, or **Load example** in the window. Each
says whether a plan should come back (`# expect: plan` or `# expect: no plan`), and a command-line run reports
whether it did. Edit them or add your own.

Planning calls the API and costs a little; running a plan does not. The tests (`tests/agent/`) replace the API
with a stand-in and cost nothing.
