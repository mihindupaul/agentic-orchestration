"""scan_weigh_rows: every vial in rows C and E of bay 3 is scanned and weighed, then ends in the same well
of bay 8. One Orca thread per vial: bay 3 -> code reader -> balance -> bay 8. Orca moves the vials with the
arm and schedules all threads (and those of any workflow running alongside) over the shared hardware.
A vial that does not read after `scan_tries` attempts is recorded as no_read and still moved on."""

import orca.orca as orca
from orca.sdk.build import Topology
from orca.spawn import LEAVE_IN_PLACE, MANUAL_PLACE
from orca.variables.variable_definition import VariableDefinition
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.thread_context import ThreadContext
from orca.workflow_models.workflow_context import WorkflowContext
from orca.workflow_models.workflow_templates import WorkflowTemplate

from orca_vialstation.cell import Balance, CodeReader, well_position

from ._vials import named, vial

SRC_BAY, DST_BAY = 3, 8
WELLS = [f"{row}{col}" for row in "CE" for col in range(1, 13)]
results: dict = {"wells": {}}


def build_workflow(topology: Topology) -> WorkflowTemplate:
    reader = topology.device("code_reader", CodeReader)
    balance = topology.device("balance", Balance)
    results["wells"] = {}

    def vial_thread(well: str):
        lw = vial(well)

        async def scan(ctx: ActionContext):
            tries, code, attempts = int(await ctx.param("scan_tries")), None, 0
            while code is None and attempts < tries:
                attempts += 1
                code = (await ctx.device().scan())["code"]
            results["wells"][well] = {"status": "ok" if code else "no_read", "code": code, "scan_attempts": attempts}

        async def weigh(ctx: ActionContext):
            results["wells"][well]["grams"] = (await ctx.device().weigh())["grams"]

        scan_a = orca.action(device=reader, inputs=[lw])(named(scan, f"scan_{well}"))
        weigh_a = orca.action(device=balance, inputs=[lw])(named(weigh, f"weigh_{well}"))

        async def method(ctx: MethodContext):
            yield scan_a
            yield weigh_a

        m = orca.method(named(method, f"scan_weigh_{well}"))

        async def thread(ctx: ThreadContext):
            yield m

        return orca.thread(labware=lw, start=(well_position(SRC_BAY, well), MANUAL_PLACE),
                           end=(well_position(DST_BAY, well), LEAVE_IN_PLACE))(named(thread, f"vial_{well}"))

    threads = [vial_thread(well) for well in WELLS]

    @orca.workflow(name="scan_weigh_rows")
    def scan_weigh_rows(wf: WorkflowContext):
        wf.variable("scan_tries", VariableDefinition(type="int", default=3, min=1, max=10))
        for t in threads:
            wf.start(t)

    return scan_weigh_rows
