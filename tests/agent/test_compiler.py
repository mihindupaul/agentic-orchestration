"""The compiler: WorkflowSpec -> Orca topology and workflow, and a compiled plan running end to end.

Needs Orca (.venv-orca) and the agent extra; skipped otherwise. No API calls."""

import asyncio

import pytest

pytest.importorskip("orca")
pytest.importorskip("anthropic")                     # importing the agent package loads the planner
pytest.importorskip("pydantic")

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


# ------------------------------------------------------------------------- destination wells and transit wells
def moves(*processes, transit=()) -> WorkflowSpec:
    """moves((id, source wells, destination wells, steps), ..., transit=[well]): all in bay 3."""
    return WorkflowSpec.model_validate({
        "name": "moves", "summary": "test", "transit": [{"bay": 3, "well": w} for w in transit],
        "processes": [{"id": pid, "source": {"bay": 3, "wells": src}, "steps": steps,
                       "destination": {"bay": 3, "wells": dst}} for pid, src, dst, steps in processes]})


SWAP_AND_CHAIN = moves(("a_to_h", ["A1", "A2"], ["H1", "H2"], [{"op": "scan"}]),
                       ("h_to_a", ["H1", "H2"], ["A1", "A2"], [{"op": "scan"}]),
                       ("chain", ["C1", "D1"], ["D1", "E1"], [{"op": "scan"}]),     # D1 moves on to a free E1
                       transit=["F1", "F2"])


def test_the_topology_teaches_destination_and_transit_wells():
    wells = sorted(n for n in build_topology(SWAP_AND_CHAIN, ControllerConfig()).locations if n.startswith("bay"))
    assert wells == sorted(f"bay3_{w}" for w in ("A1", "A2", "H1", "H2", "C1", "D1", "E1", "F1", "F2"))


def test_vials_swap_rows_through_transit_wells_and_a_chain_waits_for_its_well(caplog):
    """A1 <-> H1 and A2 <-> H2 swap (A1 and A2 wait in F1 and F2), C1 -> D1 while D1 -> E1; every vial is scanned.
    The simulator's vials keep their codes, so the code in each well afterwards shows where each vial went."""
    import logging

    from agent.validator import validate
    caplog.set_level(logging.INFO)                         # Orca reports a deadlock rescue at INFO

    async def go():
        sim = Simulator(timing=Timing(time_scale=5000.0))
        ports = await sim.start(ports=FREE)
        sim.admin({"cmd": "load_rack", "slot": 3, "barcode": "RK", "fill": ["A1", "A2", "H1", "H2", "C1", "D1"]})
        ctl = await Controller("127.0.0.1", ports).start()
        server = await serve(ctl, "127.0.0.1", 0)
        try:
            config = ControllerConfig(ctl_port=server.sockets[0].getsockname()[1])
            from agent.inventory import ControllerInventory
            report = await asyncio.to_thread(validate, SWAP_AND_CHAIN, ControllerInventory(port=config.ctl_port))
            assert report.ok, report.text()
            plan = compile_spec(SWAP_AND_CHAIN, config, report)
            results = await asyncio.wait_for(run(plan), 240)
        finally:
            server.close()
            await ctl.stop()
            await sim.stop()
        return sim, results

    sim, results = asyncio.run(go())
    code = {w: sim.admin({"cmd": "vial", "slot": 3, "well": w}).get("vial", {}).get("barcode")
            for w in ("A1", "A2", "H1", "H2", "C1", "D1", "E1", "F1", "F2")}
    assert code == {"A1": "RK-H1", "A2": "RK-H2", "H1": "RK-A1", "H2": "RK-A2",
                    "C1": None, "D1": "RK-C1", "E1": "RK-D1", "F1": None, "F2": None}
    assert results[vial_name(3, "A1")]["via"] == "bay 3 F1" and "via" not in results[vial_name(3, "H1")]
    assert all(r["status"] == "done" for r in results.values())
    assert "Deadlock resolution" not in caplog.text          # the plan's own order, not Orca's rescue


def test_a_route_sends_heavy_vials_to_bay_8_and_the_others_back_to_their_wells():
    """Weigh C1-C6; over 2 g -> bay 8, otherwise back to the same well of bay 3. Decided in the thread, mid-flight."""
    plan_spec = WorkflowSpec.model_validate({"name": "route", "summary": "test", "processes": [
        {"id": "sort", "source": {"bay": 3, "wells": [f"C{c}" for c in range(1, 7)]}, "steps": [{"op": "weigh"}],
         "routes": [{"when": {"value": "grams", "gt": 2.0}, "then": {"bay": 8}}], "destination": {"bay": 3}}]})

    async def go():
        sim = Simulator(timing=Timing(time_scale=5000.0))
        ports = await sim.start(ports=FREE)
        sim.admin({"cmd": "load_rack", "slot": 3, "barcode": "RK", "fill": [f"C{c}" for c in range(1, 7)]})
        sim.admin({"cmd": "load_rack", "slot": 8, "barcode": "R8", "fill": []})
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
    in_bay = {s: set(sim.admin({"cmd": "rack_map", "slot": s})["occupied"]) for s in (3, 8)}
    heavy = {r["well"] for r in results.values() if r["grams"] > 2.0}
    assert heavy and len(heavy) < 6                                   # both routes taken
    assert in_bay[8] == heavy and in_bay[3] == {f"C{c}" for c in range(1, 7)} - heavy
    for r in results.values():
        assert r["to"] == (f"bay 8 {r['well']}" if r["well"] in heavy else f"bay 3 {r['well']}")
        assert ("rules" in r) == (r["well"] in heavy)                 # the route rule is recorded when it decided
