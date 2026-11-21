"""The Orca device and drivers, run inside Orca. Skipped unless Orca is installed (.venv-orca)."""

import asyncio
import os

import pytest

pytest.importorskip("orca")
from cheshire_drivers.driver_errors import DriverError, DriverRefusedError, DriverRejectedError  # noqa: E402

from orca_vialstation.driver import SimVialWorkcellDriver, VialWorkcellDriver  # noqa: E402
from orca_vialstation.station_core import StationConfig  # noqa: E402
from vialctl import Controller  # noqa: E402
from vialctl.api import serve  # noqa: E402
from workcell_sim import Simulator, Timing  # noqa: E402
from workcell_sim.simulator import load_demo_racks  # noqa: E402

FREE = {k: 0 for k in ("arm_dashboard", "arm_motion", "arm_feedback", "balance", "scanner", "heater", "admin")}


def with_stack(fn):
    """Simulator + controller service on free ports; the deployment reads the addresses from the env."""
    async def main():
        sim = Simulator(timing=Timing(time_scale=500.0))
        load_demo_racks(sim.world)
        ports = await sim.start(ports=FREE)
        ctl = await Controller("127.0.0.1", ports).start()
        server = await serve(ctl, "127.0.0.1", 0)
        env = {"VIALCTL_PORT": str(server.sockets[0].getsockname()[1]), "SIM_ADMIN_HOST": "127.0.0.1",
               "SIM_ADMIN_PORT": str(ports["admin"])}
        saved = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        try:
            return await fn(sim, ctl)
        finally:
            for k, v in saved.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
            server.close()
            await ctl.stop()
            await sim.stop()
    return asyncio.run(main())


def test_live_driver_maps_outcomes_to_orca_errors():
    async def go(sim, ctl):
        drv = VialWorkcellDriver(StationConfig.from_env())
        await drv.initialize()
        try:
            assert drv.is_initialized
            assert (await drv.pick(bay=8, well="A1"))["picked"] is False          # empty rack: data
            with pytest.raises(DriverRejectedError):
                await drv.pick(bay=9, well="A1")                                   # no such bay
            with pytest.raises(DriverRejectedError):
                await drv.heat(60)                                                 # nothing docked
            assert (await drv.pick("3 A1"))["picked"]
            sim.admin({"cmd": "inject", "fault": "arm_collision"})
            with pytest.raises(DriverError):
                await drv.place("scanner")                                         # cell needs a person
            with pytest.raises(DriverRefusedError):
                await drv.home()                                                   # fault latched
        finally:
            await drv.disconnect()
    with_stack(go)


def test_sim_driver_gives_the_same_shape_of_results():
    async def go():
        drv = SimVialWorkcellDriver()
        await drv.initialize()
        assert (await drv.pick(bay=3, well="A1"))["picked"]
        assert (await drv.place("scanner"))["holding"] is False
        assert (await drv.scan())["code"] == "RACK003-A1"
        with pytest.raises(DriverRejectedError):
            await drv.heat(60)                                                     # not in the heater
        await drv.pick("scanner")
        await drv.place("heater")
        assert (await drv.heat(50))["target"] == 50.0 and (await drv.temperature())["stable"]
    asyncio.run(go())


def test_one_vial_workflow_in_pure_simulation():
    from orca_vialstation.deployment.run import run
    from orca_vialstation.deployment.workflows import one_vial
    one_vial.results.clear()
    asyncio.run(run("one_vial", live=False))
    assert one_vial.results == {"code": "RACK003-A1", "grams": 1.8, "heated_c": 45.0}


def test_one_vial_workflow_live_against_the_simulator():
    from orca_vialstation.deployment.run import run
    from orca_vialstation.deployment.workflows import one_vial

    async def go(sim, ctl):
        one_vial.results.clear()
        await asyncio.wait_for(run("one_vial", live=True), 60)
        vial = sim.admin({"cmd": "vial", "slot": 3, "well": "A1"})["vial"]     # back in its well
        assert one_vial.results["code"] == "RACK003-A1" == vial["barcode"]
        assert abs(one_vial.results["grams"] - vial["gross_g"]) < 1e-3
        assert vial["peak_c"] >= 44.5 and abs(one_vial.results["heated_c"] - 45) <= 0.5
        ops = [e["msg"] for e in ctl.events(50)]
        assert ops[-3:] == ["heater_off", "pick", "place"] and "scan" in ops and "weigh" in ops
    with_stack(go)


def test_driver_check_workflow_passes_in_pure_simulation():
    from orca_vialstation.deployment.run import results_of, run
    asyncio.run(run("driver_check", live=False))
    res = results_of("driver_check")
    assert res["passed"], [c for c in res["checks"] if not c["ok"]]
    assert len(res["checks"]) == 18 and res["code"] == "RACK003-B1"


def test_driver_check_workflow_live_retries_a_noread_and_handles_a_heater_timeout():
    from orca_vialstation.deployment.run import results_of, run

    async def go(sim, ctl):
        sim.admin({"cmd": "inject", "fault": "scanner_noread", "count": 2})
        sim.admin({"cmd": "inject", "fault": "heater_timeout"})
        await asyncio.wait_for(run("driver_check", live=True, variables={"heat_deadline_s": 1.5}), 60)
        res = results_of("driver_check")
        assert res["passed"], [c for c in res["checks"] if not c["ok"]]
        assert res["scan_attempts"] == 3 and res["code"] == "RACK003-B1"
        assert res["heater"]["stable"] is False
        assert any(c["check"] == "heater deadline handled" for c in res["checks"])
        assert sim.heater.target is None                                   # switched off
        assert sim.admin({"cmd": "vial", "slot": 3, "well": "B1"})["ok"]  # vial back in its well
    with_stack(go)


def test_two_rows_workflow_moves_scanned_vials_to_the_same_wells_of_bay_8():
    from orca_vialstation.deployment.run import results_of, run
    from orca_vialstation.deployment.workflows.two_rows import WELLS

    async def go(sim, ctl):
        everything_but_a5 = [f"{r}{c}" for r in "ABCDEFGH" for c in range(1, 13) if f"{r}{c}" != "A5"]
        sim.admin({"cmd": "load_rack", "slot": 3, "barcode": "RACK003", "fill": everything_but_a5})
        sim.admin({"cmd": "inject", "fault": "scanner_noread", "count": 3})   # A1 never reads
        await asyncio.wait_for(run("two_rows", live=True), 120)
        res = results_of("two_rows")
        assert res["summary"] == {"empty": 1, "no_read": 1, "ok": 22}
        assert res["wells"]["A1"] == {"status": "no_read", "scan_attempts": 3}
        assert res["wells"]["A5"] == {"status": "empty"}
        moved = sim.admin({"cmd": "rack_map", "slot": 8})["occupied"]
        assert sorted(moved) == sorted(w for w in WELLS if w not in ("A1", "A5"))
        for well in ("A2", "B12"):                       # same well, same vial, weight matches
            vial = sim.admin({"cmd": "vial", "slot": 8, "well": well})["vial"]
            assert vial["barcode"] == f"RACK003-{well}" == res["wells"][well]["code"]
            assert abs(vial["gross_g"] - res["wells"][well]["grams"]) < 1e-3
        left = set(sim.admin({"cmd": "rack_map", "slot": 3})["occupied"])
        assert "A1" in left and not left & (set(WELLS) - {"A1"})      # only the unread vial stayed
    with_stack(go)
