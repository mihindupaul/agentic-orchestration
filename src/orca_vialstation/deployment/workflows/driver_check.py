"""driver_check: a sample Orca workflow that exercises every workcell action and outcome.

Run it to test the driver after a change, in simulation or against the cell:

    make orca-sim WORKFLOW=driver_check
    make orca-live WORKFLOW=driver_check VARS="heat_c=50 heat_deadline_s=90"

It walks one vial (bay 3, well `well`) through the cell and records a check per step in `results`:
  1. prepare     home, tare the balance, read the tube map of bay 3 and of the empty bay
  2. empty pick  pick from a well with no vial: the driver must answer with data (picked=False), not fail
  3. scan        pick the vial, set it down in the code reader, scan with up to `scan_tries` attempts
  4. weigh       set it on the balance, read it, take it back
  5. heat        dock it in the heater, start heating, poll until STABLE or `heat_deadline_s` runs out,
                 then switch the heater off either way and check that it reports off
  6. return      put the vial back in its well and go home
Branching and waiting are decided here, in the workflow; the driver only runs single operations. A check
that fails makes the last action raise, so Orca pauses the thread and shows the failure.
"""

import asyncio
import time

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

BAY = 3                      # where the rack sits (an Orca deck position, fixed when the workflow is built)
# Orca's labware catalog has no 96-vial rack; a deep-well 96 plate has the same SBS footprint and grid.
rack = PlateTemplate("check_rack", labware_type="Thermo_Nunc_96_well_plate_1300uL_Rb")
results: dict = {}           # checks and readings, for whoever ran the workflow

VARIABLES = {
    "well": VariableDefinition(type="str", default="B1", description="well of the vial to use in bay 3"),
    "empty_bay": VariableDefinition(type="int", default=8, min=1, max=8, description="a bay with an empty rack"),
    "scan_tries": VariableDefinition(type="int", default=3, min=1, max=10, description="scan attempts"),
    "heat_c": VariableDefinition(type="float", default=40.0, min=25.0, max=100.0, unit="C",
                                 description="heater target"),
    "heat_deadline_s": VariableDefinition(type="float", default=120.0, min=1.0, max=3600.0, unit="s",
                                          description="how long to wait for the heater to become stable"),
}


def check(name: str, ok: bool, detail: str = "") -> None:
    results.setdefault("checks", []).append({"check": name, "ok": bool(ok), "detail": detail})


def build_workflow(topology: Topology) -> WorkflowTemplate:
    workcell = topology.device("vial_workcell", VialWorkcell)
    at_bay = {"device": workcell, "inputs": [rack], "deck_positions": {rack: f"bay{BAY}"}}

    @orca.action(**at_bay)
    async def prepare(ctx: ActionContext):
        results.clear()
        cell, well, empty_bay = ctx.device(), (await ctx.param("well", str)).upper(), await ctx.param("empty_bay", int)
        check("home", (await cell.home()).get("at") == "home")
        check("tare", await cell.tare() == {})
        own, empty = await cell.rack_map(BAY), await cell.rack_map(empty_bay)
        check("rack map: vial present", own["present"] and well in own["occupied"],
              f"bay {BAY}: {len(own['occupied'])} vials")
        check("rack map: empty bay", empty["present"] and not empty["occupied"], f"bay {empty_bay}")

    @orca.action(**at_bay)
    async def empty_pick(ctx: ActionContext):
        empty_bay = await ctx.param("empty_bay", int)
        r = await ctx.device().pick(bay=empty_bay, well="H12")
        check("empty well answers with data", r.get("picked") is False, r.get("reason", ""))

    @orca.action(**at_bay)
    async def scan(ctx: ActionContext):
        cell, well, tries = ctx.device(), (await ctx.param("well", str)).upper(), await ctx.param("scan_tries", int)
        picked = await cell.pick(bay=BAY, well=well)
        check("pick", picked.get("picked") is True, picked.get("at", ""))
        set_down = await cell.place("scanner")
        check("set down in scanner lets go", set_down.get("holding") is False)
        code, attempts = None, 0
        while code is None and attempts < tries:          # a no-read is data: the workflow retries
            attempts += 1
            code = (await cell.scan())["code"]
        results["code"], results["scan_attempts"] = code, attempts
        check("scan", code is not None, f"{code} after {attempts} attempt(s)")
        check("lift out of scanner", (await cell.pick("scanner")).get("picked") is True)

    @orca.action(**at_bay)
    async def weigh(ctx: ActionContext):
        cell = ctx.device()
        set_down = await cell.place("balance")
        check("set down on balance lets go", set_down.get("holding") is False)
        grams = (await cell.weigh())["grams"]
        results["grams"] = grams
        check("weigh", 0.5 < grams < 10, f"{grams:.5f} g")
        check("take back from balance", (await cell.pick("balance")).get("picked") is True)

    @orca.action(**at_bay)
    async def heat(ctx: ActionContext):
        cell = ctx.device()
        # float(...) so that an option given as a whole number (heat_c=50) is accepted too
        target, deadline_s = float(await ctx.param("heat_c")), float(await ctx.param("heat_deadline_s"))
        await cell.place("heater")
        started = await cell.heat(target)
        check("heat returns at once", started["target"] == target and started["celsius"] < target,
              f"{started['celsius']:.1f} C at start")
        t0, states, reading = time.monotonic(), set(), {}
        while time.monotonic() - t0 < deadline_s:          # how long to wait is the workflow's decision
            reading = await cell.temperature()
            states.add(reading["state"])
            if reading["stable"]:
                break
            await asyncio.sleep(0.2)
        results["heater"] = {"celsius": reading.get("celsius"), "stable": reading.get("stable", False),
                             "waited_s": round(time.monotonic() - t0, 1), "states": sorted(states)}
        if reading.get("stable"):
            check("heater stable", abs(reading["celsius"] - target) <= 0.5, f"{reading['celsius']:.1f} C")
        else:
            check("heater deadline handled", True, f"not stable after {deadline_s:g} s, switched off")
        await cell.heater_off()
        off = await cell.temperature()
        check("heater reports off", off["state"] == "OFF" and off["target"] is None)
        check("lift out of heater", (await cell.pick("heater")).get("picked") is True)

    @orca.action(**at_bay)
    async def return_and_report(ctx: ActionContext):
        cell, well = ctx.device(), (await ctx.param("well", str)).upper()
        check("return to well", (await cell.place(bay=BAY, well=well)).get("holding") is False)
        check("home", (await cell.home()).get("at") == "home")
        failed = [c["check"] for c in results["checks"] if not c["ok"]]
        results["passed"] = not failed
        if failed:
            raise RuntimeError(f"driver_check failed: {', '.join(failed)}")

    @orca.method
    async def check_driver(ctx: MethodContext):
        yield prepare
        yield empty_pick
        yield scan
        yield weigh
        yield heat
        yield return_and_report

    @orca.thread(labware=rack, start=(f"vial_workcell/bay{BAY}", MANUAL_PLACE),
                 end=(f"vial_workcell/bay{BAY}", LEAVE_IN_PLACE))
    async def check_thread(ctx: ThreadContext):
        yield check_driver

    @orca.workflow(name="driver_check")
    def driver_check(wf: WorkflowContext):
        for name, definition in VARIABLES.items():
            wf.variable(name, definition)
        wf.start(check_thread)

    return driver_check
