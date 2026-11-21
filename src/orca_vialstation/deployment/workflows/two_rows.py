"""two_rows: scan and weigh every vial in rows A and B of bay 3, then put each one in the same well of bay 8.

    make orca-sim  WORKFLOW=two_rows
    make orca-live WORKFLOW=two_rows                 # with make sim-up (or the VM) and make ctl running

Two racks take part: the source rack in bay 3 and the destination rack in bay 8. Each vial is its own Orca
action that needs both racks on the deck, so Orca's log and run view show progress vial by vial:

    pick 3 <well> -> dock in the code reader -> scan (up to `scan_tries` attempts) -> lift out
    -> set down on the balance -> weigh -> take back -> place 8 <well>

The workflow makes the decisions, not the controller:
  - a well with no vial is skipped (the pick answers picked=False);
  - a vial that never scans goes back to its own well in bay 3 and is not weighed or moved;
  - before anything moves, `prepare` checks that the destination wells in bay 8 are free.
Per-well outcomes are in `results["wells"]`.
"""

import orca.orca as orca
from orca.sdk.build import Topology
from orca.sdk.labware import PlateTemplate
from orca.spawn import LEAVE_IN_PLACE, MANUAL_PLACE
from orca.variables.variable_definition import VariableDefinition
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.workflow_context import WorkflowContext
from orca.workflow_models.workflow_templates import WorkflowTemplate

from orca_vialstation.device import VialWorkcell

SRC_BAY, DST_BAY = 3, 8
ROWS, COLS = "AB", range(1, 13)
WELLS = [f"{row}{col}" for row in ROWS for col in COLS]       # A1..A12, then B1..B12

# Orca's labware catalog has no 96-vial rack; a deep-well 96 plate has the same SBS footprint and grid.
RACK_TYPE = "Thermo_Nunc_96_well_plate_1300uL_Rb"
source = PlateTemplate("source_rack", labware_type=RACK_TYPE)
destination = PlateTemplate("destination_rack", labware_type=RACK_TYPE)

results: dict = {}


def build_workflow(topology: Topology) -> WorkflowTemplate:
    workcell = topology.device("vial_workcell", VialWorkcell)
    both_racks = {"device": workcell, "inputs": [source, destination],
                  "deck_positions": {source: f"bay{SRC_BAY}", destination: f"bay{DST_BAY}"}}

    @orca.action(**both_racks)
    async def prepare(ctx: ActionContext):
        results.clear()
        results["wells"] = {}
        cell = ctx.device()
        await cell.home()
        src, dst = await cell.rack_map(SRC_BAY), await cell.rack_map(DST_BAY)
        if not dst["present"]:
            raise RuntimeError(f"no rack in bay {DST_BAY}")
        taken = sorted(set(WELLS) & set(dst["occupied"]))
        if taken:
            raise RuntimeError(f"bay {DST_BAY} wells already hold vials: {', '.join(taken)}")
        results["planned"] = [w for w in WELLS if w in src["occupied"]]

    def process(well: str):
        """One Orca action per vial, named vial_<well>, so each shows up on its own in Orca."""
        async def process_vial(ctx: ActionContext):
            cell, tries = ctx.device(), int(await ctx.param("scan_tries"))
            picked = await cell.pick(bay=SRC_BAY, well=well)
            if not picked["picked"]:
                results["wells"][well] = {"status": "empty"}
                return
            await cell.place("scanner")
            code, attempts = None, 0
            while code is None and attempts < tries:            # a no-read is data: retry here
                attempts += 1
                code = (await cell.scan())["code"]
            await cell.pick("scanner")
            if code is None:                                    # give up on this vial: back to its own well
                await cell.place(bay=SRC_BAY, well=well)
                results["wells"][well] = {"status": "no_read", "scan_attempts": attempts}
                return
            await cell.place("balance")
            grams = (await cell.weigh())["grams"]
            await cell.pick("balance")
            await cell.place(bay=DST_BAY, well=well)
            results["wells"][well] = {"status": "ok", "code": code, "grams": grams, "scan_attempts": attempts}
        process_vial.__name__ = process_vial.__qualname__ = f"vial_{well}"
        return orca.action(**both_racks)(process_vial)

    vial_actions = [process(well) for well in WELLS]

    @orca.action(**both_racks)
    async def finish(ctx: ActionContext):
        await ctx.device().home()
        statuses = [w["status"] for w in results["wells"].values()]
        results["summary"] = {s: statuses.count(s) for s in sorted(set(statuses))}

    @orca.method
    async def scan_weigh_and_move(ctx: MethodContext):
        yield prepare
        for action in vial_actions:
            yield action
        yield finish

    @orca.thread(labware=source, start=(f"vial_workcell/bay{SRC_BAY}", MANUAL_PLACE),
                 end=(f"vial_workcell/bay{SRC_BAY}", LEAVE_IN_PLACE))
    async def source_thread(ctx: ThreadContext):
        yield scan_weigh_and_move

    @orca.thread(labware=destination, start=(f"vial_workcell/bay{DST_BAY}", MANUAL_PLACE),
                 end=(f"vial_workcell/bay{DST_BAY}", LEAVE_IN_PLACE))
    async def destination_thread(ctx: ThreadContext):
        yield orca.join()                    # takes part in the source thread's method

    @orca.workflow(name="two_rows")
    def two_rows(wf: WorkflowContext):
        wf.variable("scan_tries", VariableDefinition(type="int", default=3, min=1, max=10,
                                                     description="scan attempts per vial before giving up"))
        wf.start(source_thread)
        wf.thread(destination_thread)

    return two_rows
