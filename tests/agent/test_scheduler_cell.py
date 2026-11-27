"""The cell's Orca drivers against the real simulator and controller, without building an Orca system.

Orca calls an arm driver with requests that carry a teachpoint, and an instrument driver with an operation name.
These tests make the same calls directly, so they run in a second instead of the ~40 s an Orca build takes.
Skipped unless Orca is installed (.venv-orca)."""

import asyncio
from types import SimpleNamespace

import pytest

pytest.importorskip("orca")
from cheshire_drivers.driver_errors import DriverError, DriverRefusedError, DriverRejectedError  # noqa: E402

from agent.scheduler.cell import (BalanceDriver, CellArmDriver, CodeReaderDriver, HeaterDriver,  # noqa: E402
                                  SimStationDriver, address, layout)
from agent.scheduler.core import ControllerConfig  # noqa: E402
from workcell_controller import Controller  # noqa: E402
from workcell_controller.api import serve  # noqa: E402
from workcell_controller.deck import Deck  # noqa: E402
from workcell_sim import Simulator, Timing  # noqa: E402

FREE = {k: 0 for k in ("arm_dashboard", "arm_motion", "arm_feedback", "balance", "scanner", "heater", "admin")}


def at(position_id: str):
    """What Orca hands an arm driver: a request whose teachpoint names the position."""
    return SimpleNamespace(teachpoint=SimpleNamespace(position_id=position_id))


def with_cell(fn):
    """Simulator + controller service on free ports, vials in bay 3 C1 and C2, an empty rack in bay 8."""
    async def main():
        sim = Simulator(timing=Timing(time_scale=5000.0))
        ports = await sim.start(ports=FREE)
        sim.admin({"cmd": "load_rack", "slot": 3, "barcode": "RK-3", "fill": ["C1", "C2"]})
        sim.admin({"cmd": "load_rack", "slot": 8, "barcode": "RK-8", "fill": []})
        ctl = await Controller("127.0.0.1", ports).start()
        server = await serve(ctl, "127.0.0.1", 0)
        cfg = ControllerConfig(ctl_port=server.sockets[0].getsockname()[1])
        drivers = {"arm": CellArmDriver(cfg), "reader": CodeReaderDriver(cfg), "balance": BalanceDriver(cfg),
                   "heater": HeaterDriver(cfg)}
        for d in drivers.values():
            await d.initialize()
        try:
            return await fn(sim, ctl, drivers)
        finally:
            for d in drivers.values():
                await d.disconnect()
            server.close()
            await ctl.stop()
            await sim.stop()
    return asyncio.run(main())


def test_orca_positions_map_to_controller_addresses():
    assert address("bay3_C1") == "3 C1"
    assert address("code_reader") == "scanner" and address("heater/slot") == "heater"
    with pytest.raises(DriverRejectedError):
        address("fridge")


def test_layout_teaches_the_chosen_wells_and_every_instrument():
    pads, points = layout(Deck.load(), bays=(3, 8), rows="CD")
    assert len(pads) == 2 * 2 * 12 and "bay3_C1" in pads and "bay8_D12" in pads and "bay3_E1" not in pads
    taught = {p.position_id for p in points}
    assert {"code_reader", "balance", "heater"} <= taught and set(pads) <= taught


def test_a_vial_goes_through_every_instrument_and_ends_in_bay_8():
    async def go(sim, ctl, d):
        arm = d["arm"]
        await arm.pick_at_coords(at("bay3_C1"))
        await arm.place_at_coords(at("code_reader"))
        assert (await d["reader"].run("scan"))["code"] == "RK-3-C1"
        await arm.pick_at_coords(at("code_reader"))
        await arm.place_at_coords(at("balance"))
        assert (await d["balance"].run("weigh"))["grams"] > 0
        await arm.pick_at_coords(at("balance"))
        await arm.place_at_coords(at("heater"))
        await d["heater"].run("heat", {"celsius": 40})
        while not (await d["heater"].run("temperature"))["stable"]:
            await asyncio.sleep(0.002)
        await d["heater"].run("heater_off")
        await arm.pick_at_coords(at("heater"))
        await arm.place_at_coords(at("bay8_C1"))
        assert sim.admin({"cmd": "rack_map", "slot": 8})["occupied"] == ["C1"]
        assert sim.admin({"cmd": "vial", "slot": 8, "well": "C1"})["vial"]["peak_c"] >= 39.5
    with_cell(go)


def test_the_heater_can_be_read_while_the_arm_moves():
    """The controller locks per piece of hardware: this is what lets Orca overlap the workflows."""
    async def go(sim, ctl, d):
        move = asyncio.create_task(d["arm"].pick_at_coords(at("bay3_C2")))
        while "arm" not in ctl.status()["busy_hardware"]:
            await asyncio.sleep(0)
        assert (await d["heater"].run("temperature"))["state"] == "OFF"
        await move
    with_cell(go)


def test_outcomes_become_the_driver_errors_orca_acts_on():
    async def go(sim, ctl, d):
        with pytest.raises(DriverRejectedError):
            await d["reader"].run("weigh")                       # the code reader does not weigh
        with pytest.raises(DriverError) as failed:
            await d["arm"].pick_at_coords(at("bay3_H12"))        # no vial there: the cell needs a look
        assert failed.type is DriverError
        with pytest.raises(DriverRejectedError):
            await d["arm"].place_at_coords(at("code_reader"))    # nothing in the gripper
        with pytest.raises(DriverRejectedError):
            await d["arm"].move_to_coords(None)                  # an arm control this cell does not offer
        await d["arm"].pick_at_coords(at("bay3_C1"))
        sim.admin({"cmd": "inject", "fault": "arm_collision"})
        with pytest.raises(DriverError) as failed:
            await d["arm"].place_at_coords(at("bay8_C1"))        # arm fault
        assert failed.type is DriverError
        with pytest.raises(DriverRefusedError):                  # the fault latches until cleared
            await d["arm"].home()
    with_cell(go)


def test_sim_driver_answers_in_the_same_shape():
    async def go():
        sim = SimStationDriver("heater")
        await sim.initialize()
        assert (await sim.run("scan"))["read"] and "grams" in await sim.run("weigh")
        await sim.run("heat", {"celsius": 50})
        assert (await sim.run("temperature")) == {"celsius": 50.0, "target": 50.0, "state": "STABLE", "stable": True}
    asyncio.run(go())
