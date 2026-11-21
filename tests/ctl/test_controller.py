"""The controller service: one owner of the hardware, one operation at a time, a fault latch."""

import asyncio

import pytest

from vialctl import (ArmFault, Controller, ControllerBusy, ControllerFault, GripperEmpty, InvalidRequest,
                     OperationError)
from vialctl.api import ControllerClient, serve
from workcell_sim import Simulator, Timing

FREE = {k: 0 for k in ("arm_dashboard", "arm_motion", "arm_feedback", "balance", "scanner", "heater", "admin")}


def with_ctl(fn, fill=("A1", "A2", "B1")):
    async def main():
        sim = Simulator(timing=Timing(time_scale=5000.0))
        ports = await sim.start(ports=FREE)
        sim.admin({"cmd": "load_rack", "slot": 1, "barcode": "RACK001", "fill": list(fill)})
        ctl = await Controller("127.0.0.1", ports).start()
        try:
            return await fn(sim, ctl)
        finally:
            await ctl.stop()
            await sim.stop()
    return asyncio.run(main())


def test_operations_by_name():
    async def go(sim, ctl):
        assert await ctl.run("pick", where="1 A1") == {"at": "bay 1 A1"}
        assert await ctl.run("place", where={"station": "scanner"}) == {"at": "scanner", "holding": False}
        assert await ctl.run("scan") == {"code": "RACK001-A1"}
        await ctl.run("pick", where="scanner")
        await ctl.run("place", where="heater")
        assert (await ctl.run("heat", celsius=40))["target"] == 40.0
        assert (await ctl.run("temperature"))["state"] == "HEATING"
        await sim.clock.sleep(60)
        assert await ctl.run("temperature") == {"celsius": 40.0, "target": 40.0, "state": "STABLE", "stable": True}
        assert await ctl.run("heater_off") == {}
        assert (await ctl.run("temperature"))["target"] is None
        await ctl.run("pick", where="heater")
        await ctl.run("place", where={"bay": 1, "well": "H12"})
        assert sim.admin({"cmd": "vial", "slot": 1, "well": "H12"})["vial"]["peak_c"] >= 39.5
        assert (await ctl.run("rack_map", bay=1))["occupied"] == ["A2", "B1", "H12"]
        assert ctl.status()["at"] == "bay 1 H12" and not ctl.status()["fault"]
    with_ctl(go)


def test_one_operation_at_a_time():
    async def go(sim, ctl):
        first = asyncio.create_task(ctl.run("pick", where="1 A1"))
        await asyncio.sleep(0)
        with pytest.raises(ControllerBusy):
            await ctl.run("home")
        await first
    with_ctl(go)


def test_other_hardware_works_while_the_arm_moves():
    async def go(sim, ctl):
        moving = asyncio.create_task(ctl.run("pick", where="1 A1"))
        await asyncio.sleep(0)
        assert ctl.status()["busy_hardware"] == ["arm"]
        assert (await ctl.run("temperature"))["state"] == "OFF"     # heater read during the move
        assert await ctl.run("tare") == {}                          # balance too
        await moving
    with_ctl(go)


def test_an_empty_well_is_an_error_but_not_a_fault():
    async def go(sim, ctl):
        with pytest.raises(GripperEmpty):
            await ctl.run("pick", where="1 C1")
        assert not ctl.fault
        await ctl.run("pick", where="1 A1")
    with_ctl(go)


def test_arm_fault_latches_until_cleared():
    async def go(sim, ctl):
        sim.admin({"cmd": "inject", "fault": "arm_collision"})
        with pytest.raises(ArmFault):
            await ctl.run("pick", where="1 A1")
        with pytest.raises(ControllerFault):
            await ctl.run("home")
        await ctl.clear_fault()
        assert await ctl.run("home") == {"at": "home"}
    with_ctl(go)


@pytest.mark.parametrize("op, args", [("dance", {}), ("pick", {"where": "9 A1"}), ("pick", {"where": "1 Z9"}),
                                      ("pick", {}), ("heat", {"celsius": 60, "colour": "red"}),
                                      ("heat", {"celsius": "warm"})])
def test_invalid_requests(op, args):
    async def go(sim, ctl):
        with pytest.raises(InvalidRequest):
            await ctl.run(op, **args)
        assert not ctl.fault
    with_ctl(go)


def test_api_roundtrip():
    async def go(sim, ctl):
        server = await serve(ctl, "127.0.0.1", 0)
        c = ControllerClient("127.0.0.1", server.sockets[0].getsockname()[1])
        await c.open()
        try:
            assert (await c.call("pick", where="1 A1"))["at"] == "bay 1 A1"
            assert (await c.call("status"))["status"]["at"] == "bay 1 A1"
            with pytest.raises(InvalidRequest):
                await c.call("place", where="7 Q1")
            await c.call("place", where="1 A1")
        finally:
            await c.close()
            server.close()
    with_ctl(go)


def test_heater_errors_are_requests_not_faults():
    async def go(sim, ctl):
        with pytest.raises(OperationError):
            await ctl.run("heat", celsius=60)                 # nothing docked in the heater
        await ctl.run("pick", where="1 A1")
        await ctl.run("place", where="heater")
        with pytest.raises(InvalidRequest):
            await ctl.run("heat", celsius=150)                # outside the heater's range
        assert not ctl.fault
        assert (await ctl.run("heat", celsius=30))["target"] == 30.0
    with_ctl(go)
