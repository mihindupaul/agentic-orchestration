"""The compiler: a WorkflowSpec in, an Orca system that runs it out. No model involved: plain, tested code.

The compiler builds an Orca topology and workflow from data, not from a file per job:

  spec                          Orca
  ---------------------------   ---------------------------------------------------------------------------
  the wells it uses             the topology: just those rack wells (source, destination and transit wells;
                                layout() in cell.py), the arm, the code reader, the balance and the heater
  one vial (a well of a source  one thread: starts at its source well (MANUAL_PLACE), ends at its destination
  row)                          well (LEAVE_IN_PLACE)
  the process's steps           the thread's method: one action per step, at that step's instrument
  values (tries, celsius, ...)  written into the actions; the plan is fixed once it is approved

Orca moves each vial to the instrument of its next action, and schedules every thread of every process over
the shared hardware, so the processes of a spec run at the same time. What each action finds is recorded per
vial in `CompiledPlan.results`.

Moves into wells that are full now (a chain, a swap) need two things on top:
  - Orca waits with a move into a well until the vial there has been picked. That alone is not enough when
    both vials have steps: a vial waiting in the scanner for its destination blocks the very vial it waits
    for. So a vial whose destination holds a vial that moves out waits, before its first step, until that
    vial has left its well (its first action emits "<vial>/left").
  - a cycle (A1 -> H1, H1 -> A1) would wait forever, so one vial of each cycle first parks in the transit well
    the plan names (`orca.park`), then goes on with its steps. Nobody waits for that vial: it leaves its well
    with only the arm, so the waits never form a loop.

A process with routes decides each vial's destination mid-flight: the thread may end in any of the route's
wells (Orca takes a list of end candidates), waits until the vial's last step has measured it, decides in
plain code, and parks the vial in the chosen well, where the thread then ends.
"""

from __future__ import annotations

import asyncio
import itertools
import time
from dataclasses import dataclass, field

import orca.orca as orca
from orca.runtime.store_factory import InMemoryRuntimeStoreFactory
from orca.sdk.build import SystemBuild, Topology, build_system
from orca.sdk.labware import PlateTemplate
from orca.spawn import LEAVE_IN_PLACE, MANUAL_PLACE
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.workflow_context import WorkflowContext
from orca.workflow_models.workflow_templates import WorkflowTemplate

from .scheduler.cell import VIAL_TYPE, Balance, CellArm, CodeReader, Heater, layout, well_position
from .scheduler.core import ControllerConfig
from workcell_controller.deck import Deck

from .spec import Heat, Process, Scan, Weigh, WorkflowSpec, decide, decide_route
from .validator import Report, cycles

INSTRUMENT = {Scan: "code_reader", Weigh: "balance", Heat: "heater"}      # step -> the Orca device it runs at


@dataclass
class CompiledPlan:
    spec: WorkflowSpec
    topology: Topology
    workflow: WorkflowTemplate
    config: ControllerConfig
    results: dict = field(default_factory=dict)       # vial name -> what its actions found


def vial_name(bay: int, well: str) -> str:
    return f"vial_bay{bay}_{well}"


# ------------------------------------------------------------------------------------------- topology
def build_topology(spec: WorkflowSpec, config: ControllerConfig | None = None, deck: Deck | None = None) -> Topology:
    """The cell as Orca sees it, with rack positions only for the wells the plan uses: each source well, its
    destination well and the transit wells.
    (Orca builds a route between every pair of positions, so fewer positions build much faster.)"""
    config = config or ControllerConfig.from_env()
    used = {(t.bay, t.well) for t in spec.transit}
    for p in spec.processes:
        for well, _ in p.pairs():
            used |= {(p.source.bay, well)} | {p.target(well, dst) for dst in p.candidates()}
    bays = sorted({bay for bay, _ in used})
    pads, teachpoints = layout(deck or Deck.load(), bays=bays, wells=used)
    return Topology(
        locations={**pads, "code_reader": CodeReader("code_reader", config), "balance": Balance("balance", config),
                   "heater": Heater("heater", config)},
        transporters=[CellArm("arm", config, teachpoints)],
    )


# ------------------------------------------------------------------------------------------- workflow
def compile_workflow(spec: WorkflowSpec, topology: Topology, results: dict,
                     report: Report | None = None) -> WorkflowTemplate:
    """One Orca workflow for the whole spec: one thread per vial, for every process. `report` (the validator's)
    limits the threads to wells that hold a vial and says which vials go via a transit well; without it, every
    well of the source gets a thread and the transit wells are given to the cycles in order."""
    moves = {p.id: report.moves[p.id] if report else p.pairs() for p in spec.processes}
    by_place = {(p.source.bay, well): p for p in spec.processes for well, _ in moves[p.id]}
    goes_to = {(p.source.bay, well): (p.destination.bay, target) for p in spec.processes for well, target in moves[p.id]}
    routes = report.routes if report else {loop[0]: (t.bay, t.well) for loop, t in zip(cycles(goes_to), spec.transit)}

    def wait_for_leaving(source: tuple, process: Process) -> str | None:
        """The event this vial waits for before its first step: the vial now in its destination well has left."""
        target = goes_to[source]
        occupant = by_place.get(target)                # the process of the vial standing there now, if it moves
        if not occupant or target == source or not process.steps or not occupant.steps or target in routes:
            return None    # nobody there to wait for, it is this vial, neither can block the other at an
                           # instrument, or the vial there leaves by parking (waiting for it could loop)
        return f"{vial_name(*target)}/left"

    devices = {"code_reader": topology.device("code_reader", CodeReader),
               "balance": topology.device("balance", Balance),
               "heater": topology.device("heater", Heater)}
    coolest = min(heat_targets(spec), default=None)
    threads = [vial_thread(process, well, target, devices, results, coolest,
                           via=routes.get((process.source.bay, well)),
                           wait_for=wait_for_leaving((process.source.bay, well), process))
               for process in spec.processes for well, target in moves[process.id]]

    def workflow(wf: WorkflowContext):
        for thread in threads:
            wf.start(thread)

    return orca.workflow(name=spec.name)(_named(workflow, spec.name))


def heat_targets(spec: WorkflowSpec) -> set:
    """Every temperature the plan can heat to: the heat steps' own and the ones their rules change to."""
    return {t for p in spec.processes for s in p.steps if isinstance(s, Heat)
            for t in [s.celsius, *(r.then.celsius for r in s.rules if r.then != "skip" and r.then.celsius)]}


def vial_thread(process: Process, well: str, target: str, devices: dict, results: dict, coolest: float | None,
                via: tuple | None = None, wait_for: str | None = None):
    """The thread of one vial: source well -> (the transit well `via`) -> each step's instrument -> its
    destination well `target`. With `wait_for`, the first step waits for that event (the vial in the
    destination well has left it).

    Each step is a method of its own, and every way a step can run (as written, or as a rule changes it) is
    built in advance. A step with rules is decided by the thread, in plain code, once the vial's earlier steps
    have finished: the thread waits for the event the previous action emits. So a skipped step never takes the
    vial to that instrument, and a changed step runs with its own values."""
    name = vial_name(process.source.bay, well)
    vial = PlateTemplate(name, labware_type=VIAL_TYPE)
    record = results.setdefault(name, {"process": process.id, "well": well, "status": "waiting",
                                       "to": f"bay {process.destination.bay} {target}"})
    if via:
        record["via"] = f"bay {via[0]} {via[1]}"
    if process.routes:
        record["to"] = "decided after the steps"
    ends = list(dict.fromkeys(well_position(*process.target(well, dst)) for dst in process.candidates()))
    numbers = itertools.count()                        # Orca needs a unique name for every action and method

    def method_for(i: int, step) -> object:
        variant = f"{name}_{i}_{step.op}_{next(numbers)}"
        action = orca.action(device=devices[INSTRUMENT[type(step)]], inputs=[vial])(
            _named(step_action(step, record, f"{name}/{i}", coolest, left_event=f"{name}/left" if i == 0 else None),
                   variant))

        async def method(ctx: MethodContext):
            yield action
        return orca.method(_named(method, f"{variant}_m"))

    # (rule or None for "as written") -> method, for every step
    methods = [{None: method_for(i, step),
                **{id(r): method_for(i, step.model_copy(update=r.then.model_dump(exclude_none=True)))
                   for r in step.rules if r.then != "skip"}}
               for i, step in enumerate(process.steps)]

    async def thread(ctx: ThreadContext):
        if via:                                        # a cycle: wait in the transit well, so the source is free
            yield orca.park(well_position(*via))
        if wait_for:                                   # the vial in our destination has to leave it first
            await ctx.wait_for(wait_for)
        last_run = None                                # index of the last step this vial went through
        for i, step in enumerate(process.steps):
            rule = None
            if step.rules:
                if last_run is not None:               # wait until the earlier steps have measured the vial
                    await ctx.wait_for(f"{name}/{last_run}")
                todo, rule = decide(step, record)
                if rule is not None:
                    record.setdefault("rules", []).append(f"{step.op}: {rule.when.describe()} -> "
                                                          f"{_outcome(rule)}")
                if todo is None:
                    continue                           # skipped: the vial does not go to this instrument
            last_run = i
            yield methods[i][None if rule is None else id(rule)]
        if process.routes:                             # where the vial goes, from what its steps measured
            await ctx.wait_for(f"{name}/{last_run}")
            destination, route = decide_route(process, record)
            bay, to = process.target(well, destination)
            record["to"] = f"bay {bay} {to}"
            if route is not None:
                record.setdefault("rules", []).append(f"route: {route.when.describe()} -> {destination.describe()}")
            yield orca.park(well_position(bay, to))    # the thread ends here: one of its end candidates

    end = ends if process.routes else well_position(process.destination.bay, target)
    return orca.thread(labware=vial, start=(well_position(process.source.bay, well), MANUAL_PLACE),
                       end=(end, LEAVE_IN_PLACE))(_named(thread, name))


def _outcome(rule) -> str:
    return "skip" if rule.then == "skip" else ", ".join(f"{k} {v}" for k, v in rule.then.model_dump(exclude_none=True).items())


def step_action(step, record: dict, done_event: str, coolest: float | None, left_event: str | None = None):
    """The body of one action: what the instrument does with the vial standing in it. It records what it found
    and then emits `done_event`, which a thread waits for before deciding a later step's rules. (Orca reads a
    method one step ahead, so a thread must wait for that event rather than trust the order of its yields:
    docs/orca-integration.md, lesson 11.) The first step also emits `left_event` as it starts: the vial is at
    the instrument, so its well is free for the vial waiting to go there."""
    body = {Scan: _scan, Weigh: _weigh, Heat: _heat}[type(step)]

    async def action(ctx: ActionContext):
        if left_event:
            await ctx.emit(left_event)
        if record["status"] == "waiting":
            record["status"] = "running"
        await body(ctx.device(), step, record, coolest)
        await ctx.emit(done_event)
    return action


async def _scan(reader, step: Scan, record: dict, coolest) -> None:
    code, attempts = None, 0
    while code is None and attempts < step.tries:
        attempts += 1
        code = (await reader.scan())["code"]
    record.update(code=code, read=code is not None, scan_attempts=attempts)
    if code is None:
        record["status"] = "no_read"                 # recorded, and the vial still goes on to its destination


async def _weigh(balance, step: Weigh, record: dict, coolest) -> None:
    record["grams"] = (await balance.weigh())["grams"]


async def _heat(heater, step: Heat, record: dict, coolest: float | None) -> None:
    t0 = time.monotonic()
    await heater.heat(step.celsius)
    await _until(heater, lambda r: r["stable"], hold_s=step.hold_s)
    record.update(celsius=step.celsius, in_heater_s=round(time.monotonic() - t0, 1))
    # The next vial must not go into a heater hotter than its own target. If this plan heats to more than one
    # temperature, cool to the lowest of them before letting the vial (and the heater) go.
    if coolest is not None and step.celsius > coolest + 0.5:
        await heater.heat(coolest)
        await _until(heater, lambda r: r["celsius"] <= coolest + 0.5)
        record["cooled_to"] = coolest


async def _until(heater, ready, hold_s: float = 0.0) -> None:
    """Poll the heater until `ready(reading)` has held for `hold_s` seconds. Only the heater is reserved
    meanwhile: the arm and the other instruments serve other vials."""
    since = None
    while True:
        if ready(await heater.temperature()):
            since = since or time.monotonic()
            if time.monotonic() - since >= hold_s:
                return
        else:
            since = None
        await asyncio.sleep(0.2)


# ------------------------------------------------------------------------------------------- the whole plan
def compile_spec(spec: WorkflowSpec, config: ControllerConfig | None = None, report: Report | None = None) -> CompiledPlan:
    """Topology and workflow for `spec`, with the validator's `report` on it. Building them is cheap; `build()`
    is the slow part."""
    config = config or ControllerConfig.from_env()
    topology = build_topology(spec, config)
    results: dict = {}
    return CompiledPlan(spec, topology, compile_workflow(spec, topology, results, report), config, results)


def finish(plan: CompiledPlan) -> dict:
    """After a completed run every vial has reached its destination: it is done (a no-read stays no_read). A
    vial of a process with no steps (a plain move) never ran an action, so it is still "waiting" until here."""
    for record in plan.results.values():
        if record["status"] in ("running", "waiting"):
            record["status"] = "done"
    return plan.results


async def run(plan: CompiledPlan, on_state=None, on_submitted=None) -> dict:
    """Build the Orca system for `plan`, run it live, leave the heater off, and return the per-vial results.
    `on_state(text)` hears "building", "running" and "finished"; `on_submitted(runtime, execution_id)` is
    passed to run_workflow."""
    say = on_state or (lambda text: None)
    from .scheduler.core import CellCore
    from .scheduler.runtime import run_workflow

    say("building")
    built = await build(plan)
    try:
        say("running")
        await run_workflow(built, plan.spec.name, on_submitted=on_submitted)
    finally:
        say("finished")
        if heat_targets(plan.spec):                  # the heater is left at the last target: switch it off
            core = CellCore(plan.config)
            try:
                await core.execute("heater_off")
            finally:
                await core.close()
    return finish(plan)


async def build(plan: CompiledPlan) -> SystemBuild:
    """The runnable Orca system for a compiled plan (this is where Orca computes the arm's routes)."""
    return await build_system(plan.spec.name, plan.topology, InMemoryRuntimeStoreFactory(), workflow=plan.workflow,
                              description=plan.spec.summary)


def _named(func, name: str):
    """Orca names actions, methods and threads after their function, so each generated one gets its own name."""
    func.__name__ = func.__qualname__ = name
    return func
