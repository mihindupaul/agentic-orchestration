"""The compiler: WorkflowSpec -> Orca topology and workflow, and a compiled plan running end to end.

Needs Orca (.venv-orca) and the agent extra; skipped otherwise. No API calls."""

import asyncio

import pytest

pytest.importorskip("orca")
pytest.importorskip("anthropic")                     # importing the agent package loads the planner
pytest.importorskip("pydantic")
from orca.runtime.store_factory import InMemoryRuntimeStoreFactory  # noqa: E402

from agent.compiler import build_topology, compile_spec, run, vial_name  # noqa: E402
from agent.spec import WorkflowSpec  # noqa: E402
from orca_workcell.core import ControllerConfig  # noqa: E402
from workcell_controller import Controller  # noqa: E402
from workcell_controller.api import serve  # noqa: E402
from workcell_sim import Simulator, Timing  # noqa: E402

FREE = {k: 0 for k in ("arm_dashboard", "arm_motion", "arm_feedback", "balance", "scanner", "heater", "admin")}


def spec(**processes) -> WorkflowSpec:
    """spec(scan_weigh=("CE", [scan, weigh]), heat=("DF", [heat 40/20])): every process from bay 3 to bay 8."""
    return WorkflowSpec.model_validate({
        "name": "test_plan", "summary": "test",
        "processes": [{"id": pid, "source": {"bay": 3, "rows": rows}, "steps": steps,
                       "destination": {"bay": 8, "wells": "same"}} for pid, (rows, steps) in processes.items()]})


SCAN_WEIGH = [{"op": "scan", "tries": 3}, {"op": "weigh"}]
HEAT = [{"op": "heat", "celsius": 40, "hold_s": 20}]


def test_the_topology_has_rack_wells_only_for_the_rows_the_plan_uses():
    topo = build_topology(spec(one=("C", SCAN_WEIGH)), ControllerConfig())
    wells = sorted(name for name in topo.locations if name.startswith("bay"))
    assert len(wells) == 2 * 12 and wells[0] == "bay3_C1" and "bay8_C12" in wells and "bay3_D1" not in wells
    assert {"code_reader", "balance", "heater"} <= set(topo.locations)


def test_the_plan_of_the_hand_written_workflows_compiles_to_the_same_vial_journeys():
    """scan_weigh_rows + heat_rows, written by hand, against the same two processes as a spec."""
    from orca_workcell.deployment.topology import build_topology as hand_topology
    from orca_workcell.deployment.workflows import heat_rows, scan_weigh_rows

    def journeys(workflow):
        return sorted((t.start_position_id, tuple(t.end_position_ids)) for t in workflow.entry_thread_templates)

    hand = hand_topology(InMemoryRuntimeStoreFactory())
    by_hand = journeys(scan_weigh_rows.build_workflow(hand)) + journeys(heat_rows.build_workflow(hand))
    plan = compile_spec(spec(scan_weigh=("CE", SCAN_WEIGH), heat=("DF", HEAT)), ControllerConfig())
    assert journeys(plan.workflow) == sorted(by_hand)
    assert len(by_hand) == 48 and ("bay3_D7", ("bay8_D7",)) in by_hand
    assert vial_name(3, "C1") in {t.labware_template.name for t in plan.workflow.entry_thread_templates}


def test_a_compiled_plan_runs_both_processes_through_the_cell():
    """Rows A (scan, weigh) and B (heat to 40 C) of bay 3 into bay 8, live against the simulator and controller."""
    async def go():
        sim = Simulator(timing=Timing(time_scale=5000.0))
        ports = await sim.start(ports=FREE)
        wells = [f"{r}{c}" for r in "AB" for c in range(1, 13)]
        sim.admin({"cmd": "load_rack", "slot": 3, "barcode": "RK-3", "fill": wells})
        sim.admin({"cmd": "load_rack", "slot": 8, "barcode": "RK-8", "fill": []})
        sim.admin({"cmd": "inject", "fault": "scanner_noread", "count": 3})       # the first vial never reads
        ctl = await Controller("127.0.0.1", ports).start()
        server = await serve(ctl, "127.0.0.1", 0)
        try:
            plan = compile_spec(spec(scan_weigh=("A", SCAN_WEIGH),
                                     heat=("B", [{"op": "heat", "celsius": 40, "hold_s": 0}])),
                                ControllerConfig(ctl_port=server.sockets[0].getsockname()[1]))
            results = await asyncio.wait_for(run(plan), 240)
        finally:
            server.close()
            await ctl.stop()
            await sim.stop()
        return sim, results

    sim, results = asyncio.run(go())
    assert sorted(sim.admin({"cmd": "rack_map", "slot": 8})["occupied"]) == sorted(
        f"{r}{c}" for r in "AB" for c in range(1, 13))                          # every vial ended in bay 8
    assert sim.admin({"cmd": "rack_map", "slot": 3})["occupied"] == []
    statuses = [r["status"] for r in results.values()]
    assert statuses.count("no_read") == 1 and statuses.count("done") == 23
    a2 = results[vial_name(3, "A2")]
    assert a2["code"] == "RK-3-A2" and a2["grams"] > 0 and "celsius" not in a2
    b5 = results[vial_name(3, "B5")]
    assert abs(b5["celsius"] - 40) <= 0.5 and "code" not in b5
    assert sim.admin({"cmd": "vial", "slot": 8, "well": "B5"})["vial"]["peak_c"] >= 39.5
    assert sim.admin({"cmd": "state"})["state"]["heater"]["state"] == "OFF"                # switched off after the last heat


def test_rules_skip_light_vials_and_heat_codes_starting_with_10_hotter():
    """The two rules from the design discussion, live: 'if the weight is less than 10 g do not heat' and
    'if the scanned code starts with 10, heat it to 80 C'. Bay 3 holds ordinary codes, bay 4 codes starting
    with 10; in both rows the first vial is light (about 1.8 g), the others heavy (about 11.8 g)."""
    steps = [{"op": "scan"}, {"op": "weigh"},
             {"op": "heat", "celsius": 40, "hold_s": 0, "rules": [
                 {"when": {"value": "grams", "lt": 10}, "then": "skip"},
                 {"when": {"value": "code", "starts_with": "10"}, "then": {"celsius": 80}}]}]
    plan_spec = WorkflowSpec.model_validate({"name": "rules_plan", "summary": "test", "processes": [
        {"id": "ordinary", "source": {"bay": 3, "rows": "A"}, "steps": steps, "destination": {"bay": 8}},
        {"id": "tens", "source": {"bay": 4, "rows": "A"}, "steps": steps, "destination": {"bay": 7}}]})

    async def go():
        sim = Simulator(timing=Timing(time_scale=5000.0))
        ports = await sim.start(ports=FREE)
        row_a = [f"A{c}" for c in range(1, 13)]
        for slot, barcode in ((3, "RK"), (4, "10X")):
            sim.admin({"cmd": "load_rack", "slot": slot, "barcode": barcode, "fill": row_a, "content_g": 10.0})
            sim.world.racks[slot].wells[(0, 0)].content_g = 0.0                  # A1: the light one
        for slot in (7, 8):
            sim.admin({"cmd": "load_rack", "slot": slot, "barcode": f"RK-{slot}", "fill": []})
        ctl = await Controller("127.0.0.1", ports).start()
        server = await serve(ctl, "127.0.0.1", 0)
        try:
            plan = compile_spec(plan_spec, ControllerConfig(ctl_port=server.sockets[0].getsockname()[1]))
            results = await asyncio.wait_for(run(plan), 240)
        finally:
            server.close()
            await ctl.stop()
            await sim.stop()
        return sim, results

    sim, results = asyncio.run(go())
    peak = lambda slot, well: sim.admin({"cmd": "vial", "slot": slot, "well": well})["vial"]["peak_c"]  # noqa: E731
    light, ordinary, tens = results[vial_name(3, "A1")], results[vial_name(3, "A2")], results[vial_name(4, "A5")]
    assert light["grams"] < 10 and "celsius" not in light and light["rules"] == ["heat: grams < 10.0 -> skip"]
    assert peak(8, "A1") is None and peak(7, "A1") is None                     # skipped: never in the heater
    assert results[vial_name(4, "A1")]["rules"] == ["heat: grams < 10.0 -> skip"]   # the first rule wins
    assert abs(ordinary["celsius"] - 40) <= 0.5 and "rules" not in ordinary
    assert all(peak(8, f"A{c}") < 41 for c in range(2, 13))                   # never in a heater still at 80 C
    assert tens["code"].startswith("10") and abs(tens["celsius"] - 80) <= 0.5
    assert tens["rules"] == ["heat: code starts with '10' -> celsius 80.0"] and peak(7, "A5") >= 79.5
    assert tens["cooled_to"] == 40                     # cooled to the plan's lowest target before the next vial
    assert all(r["status"] == "done" for r in results.values()) and len(results) == 24
    assert sim.admin({"cmd": "state"})["state"]["heater"]["state"] == "OFF"


def test_a_process_with_no_steps_moves_each_vial_straight_to_the_destination_rack():
    """A plain rack-to-rack move: no instrument visited, every vial ends in the same well of the other rack."""
    async def go():
        sim = Simulator(timing=Timing(time_scale=5000.0))
        ports = await sim.start(ports=FREE)
        sim.admin({"cmd": "load_rack", "slot": 3, "barcode": "RK-3", "fill": [f"C{c}" for c in range(1, 13)]})
        sim.admin({"cmd": "load_rack", "slot": 8, "barcode": "RK-8", "fill": []})
        ctl = await Controller("127.0.0.1", ports).start()
        server = await serve(ctl, "127.0.0.1", 0)
        try:
            plan = compile_spec(spec(move=("C", [])), ControllerConfig(ctl_port=server.sockets[0].getsockname()[1]))
            results = await asyncio.wait_for(run(plan), 240)
        finally:
            server.close()
            await ctl.stop()
            await sim.stop()
        return sim, results

    sim, results = asyncio.run(go())
    assert sorted(sim.admin({"cmd": "rack_map", "slot": 8})["occupied"]) == sorted(f"C{c}" for c in range(1, 13))
    assert sim.admin({"cmd": "rack_map", "slot": 3})["occupied"] == []
    events = [e["event"] for e in sim.admin({"cmd": "events"})["events"]]
    assert not any("scan" in e or "weigh" in e or "heat" in e for e in events)   # no instrument was used
    assert len(results) == 12 and all(r["status"] == "done" for r in results.values())


def test_the_topology_teaches_only_the_wells_a_plan_uses():
    plan = WorkflowSpec.model_validate({"name": "t", "summary": "t", "processes": [
        {"id": "a", "source": {"bay": 3, "wells": ["A1", "B1"]}, "steps": [], "destination": {"bay": 8}},
        {"id": "b", "source": {"bay": 3, "columns": [12]}, "steps": [], "destination": {"bay": 8}}]})
    wells = sorted(n for n in build_topology(plan, ControllerConfig()).locations if n.startswith("bay"))
    assert len(wells) == 2 * (2 + 8) and "bay3_A1" in wells and "bay8_H12" in wells and "bay3_C1" not in wells
