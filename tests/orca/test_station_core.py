"""The Orca-facing station core: one action per controller operation, outcomes mapped for Orca.

`rack_workflow` below is what an Orca workflow does in stage 3, written as plain Python so it can be
tested without Orca: the rack job lives in the workflow, built only from station actions."""

import asyncio
import json

import pytest


from orca_vialstation import CoreFailed, CoreRefused, CoreRejected, StationConfig, VialStationCore, parse_slot
from vialctl import Controller
from vialctl.api import serve
from workcell_sim import Simulator, Timing

FREE = {k: 0 for k in ("arm_dashboard", "arm_motion", "arm_feedback", "balance", "scanner", "heater", "admin")}


def with_station(fn):
    async def main():
        sim = Simulator(timing=Timing(time_scale=5000.0))
        ports = await sim.start(ports=FREE)
        ctl = await Controller("127.0.0.1", ports).start()
        server = await serve(ctl, "127.0.0.1", 0)
        cfg = StationConfig(ctl_port=server.sockets[0].getsockname()[1], sim_admin_host="127.0.0.1",
                            sim_admin_port=ports["admin"], default_fill=0.03)   # 3 vials: A1, B1, C1
        core = VialStationCore(cfg)
        try:
            return await fn(sim, core, ctl)
        finally:
            await core.close()
            server.close()
            await ctl.stop()
            await sim.stop()
    return asyncio.run(main())


async def rack_workflow(core, src_bay: int, dst_bay: int, scan_tries: int = 2) -> dict:
    """Every tube of a rack: scan (with retries), weigh, heat to 40 C and wait until stable, place in the same well of the
    destination rack. A tube that will not scan goes back to its source well and the job carries on."""
    results = {}
    for well in (await core.execute("rack_map", {"bay": src_bay}))["occupied"]:
        if not (await core.execute("pick", {"bay": src_bay, "well": well}))["picked"]:
            results[well] = {"status": "empty"}
            continue
        await core.execute("place", {"station": "scanner"})
        for _ in range(scan_tries):
            code = (await core.execute("scan"))["code"]
            if code:
                break
        await core.execute("pick", {"station": "scanner"})
        if not code:
            await core.execute("place", {"bay": src_bay, "well": well})
            results[well] = {"status": "failed", "reason": "no read"}
            continue
        await core.execute("place", {"station": "balance"})
        grams = (await core.execute("weigh"))["grams"]
        await core.execute("pick", {"station": "balance"})
        await core.execute("place", {"station": "heater"})
        await core.execute("heat", {"celsius": 40})
        while not (await core.execute("temperature"))["stable"]:      # waiting is the workflow's choice
            await asyncio.sleep(0.002)
        await core.execute("heater_off")
        await core.execute("pick", {"station": "heater"})
        await core.execute("place", {"bay": dst_bay, "well": well})
        results[well] = {"status": "ok", "code": code, "grams": grams}
    return results


def test_parse_slot():
    assert parse_slot("slot3") == 3 and parse_slot("vial_station/slot7") == 7 and parse_slot("8") == 8
    with pytest.raises(CoreRejected):
        parse_slot("deck")


def test_rack_job_is_a_workflow_of_station_actions():
    async def go(sim, core, ctl):
        await core.rack_placed("vial_station/slot3", "RK-3", "source")
        await core.rack_placed("vial_station/slot8", "RK-8", "dest")
        sim.admin({"cmd": "remove_rack", "slot": 8})
        sim.admin({"cmd": "load_rack", "slot": 8, "barcode": "RK-8", "fill": []})
        sim.admin({"cmd": "inject", "fault": "scanner_noread", "count": 2})      # A1 never reads
        res = await rack_workflow(core, 3, 8)
        assert res["A1"] == {"status": "failed", "reason": "no read"}
        assert res["B1"]["status"] == "ok" and res["B1"]["code"] == "RK-3-B1"
        assert res["C1"]["status"] == "ok"
        assert sim.admin({"cmd": "rack_map", "slot": 3})["occupied"] == ["A1"]           # failed tube went back
        assert sim.admin({"cmd": "vial", "slot": 8, "well": "C1"})["vial"]["peak_c"] >= 39.5
    with_station(go)


def test_run_protocol_is_one_action(tmp_path):
    async def go(sim, core, ctl):
        await core.rack_placed("slot2", "RK-2", "r")
        proto = tmp_path / "pick.json"
        proto.write_text(json.dumps({"action": "pick"}))
        out = tmp_path / "res.json"
        res = await core.run_protocol(str(proto), {"site": "vial_station/slot2", "well": "A1",
                                                   "results_path": str(out)})
        assert res["picked"] and json.loads(out.read_text())["at"] == "bay 2 A1"
        assert (await core.run_protocol("place", {"station": "scanner"}))["holding"] is False
        assert (await core.run_protocol("scan", {}))["read"]
    with_station(go)


def test_outcomes_map_to_orca_errors():
    async def go(sim, core, ctl):
        await core.rack_placed("slot1", "RK-1", "r")
        assert (await core.execute("pick", {"bay": 1, "well": "H12"}))["picked"] is False   # empty well: data
        with pytest.raises(CoreRejected):
            await core.execute("pick", {"bay": 9, "well": "A1"})                         # no such bay
        with pytest.raises(CoreRejected):
            await core.execute("place", {"station": "scanner"})                          # nothing held
        with pytest.raises(CoreRejected):
            await core.execute("fly")
        other = VialStationCore(core.cfg)
        await other.connect()
        try:
            first = asyncio.create_task(core.execute("pick", {"bay": 1, "well": "A1"}))
            while not ctl.status()["busy"]:                                              # wait until it runs
                await asyncio.sleep(0)
            with pytest.raises(CoreRefused):                                             # busy
                await other.execute("home")
            await first
            sim.admin({"cmd": "inject", "fault": "arm_collision"})
            with pytest.raises(CoreFailed):
                await core.execute("place", {"bay": 1, "well": "A1"})
            with pytest.raises(CoreRefused):                                             # latched fault
                await other.execute("home")
        finally:
            await other.close()
    with_station(go)
