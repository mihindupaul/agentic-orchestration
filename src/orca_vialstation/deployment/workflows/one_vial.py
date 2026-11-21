"""One vial through the workcell, as a method of discrete Orca actions.

An operator puts the rack in bay 3 (MANUAL_PLACE: in a live run Orca waits until the rack is registered
there) and it stays there afterwards (LEAVE_IN_PLACE). Each action is a
few controller operations; the workflow, not the controller, decides the order and when the heater has
waited long enough.
"""

import asyncio

import orca.orca as orca
from orca.sdk.build import Topology
from orca.sdk.labware import PlateTemplate
from orca.spawn import LEAVE_IN_PLACE, MANUAL_PLACE
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.workflow_context import WorkflowContext
from orca.workflow_models.workflow_templates import WorkflowTemplate

from orca_vialstation.device import VialWorkcell

BAY, WELL, HEAT_C = 3, "A1", 45.0
# Orca's labware catalog has no 96-vial rack; a deep-well 96 plate has the same SBS footprint and grid.
rack = PlateTemplate("rack", labware_type="Thermo_Nunc_96_well_plate_1300uL_Rb")
results: dict = {}          # what the actions found, for whoever ran the workflow


def build_workflow(topology: Topology) -> WorkflowTemplate:
    workcell = topology.device("vial_workcell", VialWorkcell)
    at_bay = {"device": workcell, "inputs": [rack], "deck_positions": {rack: f"bay{BAY}"}}

    @orca.action(**at_bay)
    async def scan_vial(ctx: ActionContext):
        cell = ctx.device()
        picked = await cell.pick(bay=BAY, well=WELL)
        if not picked["picked"]:
            raise RuntimeError(f"no vial in bay {BAY} {WELL}")
        await cell.place("scanner")
        results["code"] = (await cell.scan())["code"]
        await cell.pick("scanner")

    @orca.action(**at_bay)
    async def weigh_vial(ctx: ActionContext):
        cell = ctx.device()
        await cell.place("balance")
        results["grams"] = (await cell.weigh())["grams"]
        await cell.pick("balance")

    @orca.action(**at_bay)
    async def heat_vial(ctx: ActionContext):
        cell = ctx.device()
        await cell.place("heater")
        await cell.heat(HEAT_C)
        while not (await cell.temperature())["stable"]:      # the workflow decides how long to wait
            await asyncio.sleep(0.2)
        results["heated_c"] = (await cell.temperature())["celsius"]
        await cell.heater_off()
        await cell.pick("heater")

    @orca.action(**at_bay)
    async def return_vial(ctx: ActionContext):
        await ctx.device().place(bay=BAY, well=WELL)

    @orca.method
    async def process_one_vial(ctx: MethodContext):
        yield scan_vial
        yield weigh_vial
        yield heat_vial
        yield return_vial

    @orca.thread(labware=rack, start=(f"vial_workcell/bay{BAY}", MANUAL_PLACE),
                 end=(f"vial_workcell/bay{BAY}", LEAVE_IN_PLACE))
    async def rack_thread(ctx: ThreadContext):
        yield process_one_vial

    @orca.workflow(name="one_vial")
    def one_vial(wf: WorkflowContext):
        wf.start(rack_thread)

    return one_vial
