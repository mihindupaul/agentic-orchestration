"""heat_rows: every vial in rows D and F of bay 3 is heated to `heat_c`, held there for `hold_s`, then ends
in the same well of bay 8. One Orca thread per vial: bay 3 -> heater -> bay 8.

The heater is its own device, so while a vial heats only the heater is reserved: Orca uses the arm, the
code reader and the balance for other vials meanwhile (for example those of scan_weigh_rows). The heater
stays at temperature between vials, so the next vial starts warm, and is switched off after the last one."""

import asyncio
import time

import orca.orca as orca
from orca.sdk.build import Topology
from orca.spawn import LEAVE_IN_PLACE, MANUAL_PLACE
from orca.variables.variable_definition import VariableDefinition
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.workflow_context import WorkflowContext
from orca.workflow_models.workflow_templates import WorkflowTemplate

from orca_vialstation.cell import Heater, well_position

from ._vials import named, vial

SRC_BAY, DST_BAY = 3, 8
WELLS = [f"{row}{col}" for row in "DF" for col in range(1, 13)]
results: dict = {"wells": {}}


def build_workflow(topology: Topology) -> WorkflowTemplate:
    heater = topology.device("heater", Heater)
    results["wells"] = {}

    def vial_thread(well: str):
        lw = vial(well)

        async def heat(ctx: ActionContext):
            cell, target = ctx.device(), float(await ctx.param("heat_c"))
            hold_s, t0, stable_since = float(await ctx.param("hold_s")), time.monotonic(), None
            await cell.heat(target)
            while True:                                   # only the heater is reserved while this waits
                reading = await cell.temperature()
                if reading["stable"]:
                    stable_since = stable_since or time.monotonic()
                    if time.monotonic() - stable_since >= hold_s:
                        break
                await asyncio.sleep(0.2)
            results["wells"][well] = {"status": "ok", "celsius": reading["celsius"],
                                      "in_heater_s": round(time.monotonic() - t0, 1)}
            if len(results["wells"]) == len(WELLS):      # last vial: switch the heater off
                await cell.heater_off()

        heat_a = orca.action(device=heater, inputs=[lw])(named(heat, f"heat_{well}"))

        async def method(ctx: MethodContext):
            yield heat_a

        m = orca.method(named(method, f"heat_vial_{well}"))

        async def thread(ctx: ThreadContext):
            yield m

        return orca.thread(labware=lw, start=(well_position(SRC_BAY, well), MANUAL_PLACE),
                           end=(well_position(DST_BAY, well), LEAVE_IN_PLACE))(named(thread, f"vial_{well}"))

    threads = [vial_thread(well) for well in WELLS]

    @orca.workflow(name="heat_rows")
    def heat_rows(wf: WorkflowContext):
        wf.variable("heat_c", VariableDefinition(type="float", default=40.0, min=25.0, max=100.0, unit="C"))
        wf.variable("hold_s", VariableDefinition(type="float", default=20.0, min=0.0, max=3600.0, unit="s",
                                                 description="time at temperature for each vial"))
        for t in threads:
            wf.start(t)

    return heat_rows
