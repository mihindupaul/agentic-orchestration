"""The Orca-facing station core: one action per controller operation, outcomes mapped for Orca.

`vial_job` below is what an Orca workflow does with these actions, written as plain Python so it can be
tested without Orca: the job lives in the workflow, built only from station actions."""

import asyncio

import pytest

from orca_vialstation import CoreFailed, CoreRefused, CoreRejected, StationConfig, VialStationCore
from vialctl import Controller
from vialctl.api import serve
from workcell_sim import Simulator, Timing

FREE = {k: 0 for k in ("arm_dashboard", "arm_motion", "arm_feedback", "balance", "scanner", "heater", "admin")}


def with_station(fn):
    """Simulator + controller service on free ports, a full rack in bay 3 and an empty one in bay 8."""
    async def main():
        sim = Simulator(timing=Timing(time_scale=5000.0))
        ports = await sim.start(ports=FREE)
        sim.admin({"cmd": "load_rack", "slot": 3, "barcode": "RK-3", "fill": ["A1", "B1", "C1"]})
        sim.admin({"cmd": "load_rack", "slot": 8, "barcode": "RK-8", "fill": []})
        ctl = await Controller("127.0.0.1", ports).start()
        server = await serve(ctl, "127.0.0.1", 0)
        core = VialStationCore(StationConfig(ctl_port=server.sockets[0].getsockname()[1]))
        try:
            return await fn(sim, core, ctl)
        finally:
            await core.close()
            server.close()
            await ctl.stop()
            await sim.stop()
    return asyncio.run(main())


async def vial_job(core, wells, src_bay: int, dst_bay: int, scan_tries: int = 2) -> dict:
    """Each vial: scan (with retries), weigh, heat to 40 C until stable, then to the same well of the
    destination rack. A vial that will not scan goes back to its source well and the job carries on."""
    results = {}
    for well in wells:
        if not (await core.execute("pick", {"where": f"{src_bay} {well}"}))["picked"]:
            results[well] = {"status": "empty"}
            continue
        await core.execute("place", {"where": "scanner"})
        for _ in range(scan_tries):
            code = (await core.execute("scan"))["code"]
            if code:
                break
        await core.execute("pick", {"where": "scanner"})
        if not code:
            await core.execute("place", {"where": f"{src_bay} {well}"})
            results[well] = {"status": "failed", "reason": "no read"}
            continue
        await core.execute("place", {"where": "balance"})
        grams = (await core.execute("weigh"))["grams"]
        await core.execute("pick", {"where": "balance"})
        await core.execute("place", {"where": "heater"})
        await core.execute("heat", {"celsius": 40})
        while not (await core.execute("temperature"))["stable"]:      # waiting is the workflow's choice
            await asyncio.sleep(0.002)
        await core.execute("heater_off")
        await core.execute("pick", {"where": "heater"})
        await core.execute("place", {"where": f"{dst_bay} {well}"})
        results[well] = {"status": "ok", "code": code, "grams": grams}
    return results


def test_a_job_is_a_sequence_of_station_actions():
    async def go(sim, core, ctl):
        sim.admin({"cmd": "inject", "fault": "scanner_noread", "count": 2})      # A1 never reads
        res = await vial_job(core, ["A1", "B1", "C1", "D1"], 3, 8)
        assert res["A1"] == {"status": "failed", "reason": "no read"}
        assert res["B1"]["status"] == "ok" and res["B1"]["code"] == "RK-3-B1"
        assert res["C1"]["status"] == "ok"
        assert res["D1"] == {"status": "empty"}
        assert sim.admin({"cmd": "rack_map", "slot": 3})["occupied"] == ["A1"]           # failed vial went back
        assert sim.admin({"cmd": "vial", "slot": 8, "well": "C1"})["vial"]["peak_c"] >= 39.5
    with_station(go)


def test_outcomes_map_to_orca_errors():
    async def go(sim, core, ctl):
        assert (await core.execute("pick", {"bay": 3, "well": "H12"}))["picked"] is False   # empty well: data
        with pytest.raises(CoreRejected):
            await core.execute("pick", {"bay": 9, "well": "A1"})                         # no such bay
        with pytest.raises(CoreRejected):
            await core.execute("place", {"station": "scanner"})                          # nothing held
        with pytest.raises(CoreRejected):
            await core.execute("fly")
        other = VialStationCore(core.cfg)
        await other.connect()
        try:
            first = asyncio.create_task(core.execute("pick", {"bay": 3, "well": "A1"}))
            while not ctl.status()["busy"]:                                              # wait until it runs
                await asyncio.sleep(0)
            with pytest.raises(CoreRefused):                                             # busy
                await other.execute("home")
            await first
            sim.admin({"cmd": "inject", "fault": "arm_collision"})
            with pytest.raises(CoreFailed):
                await core.execute("place", {"bay": 3, "well": "A1"})
            with pytest.raises(CoreRefused):                                             # latched fault
                await other.execute("home")
        finally:
            await other.close()
    with_station(go)
