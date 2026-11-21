"""Labware operations end to end against a live simulator: bay and station names in, device lines out."""

import asyncio

import pytest

from vialctl.errors import GripperEmpty, OperationError
from vialctl.operations import Workcell
from workcell_sim import Simulator, Timing

FREE = {k: 0 for k in ("arm_dashboard", "arm_motion", "arm_feedback", "balance", "scanner", "heater", "admin")}


def with_cell(fn):
    async def main():
        sim = Simulator(timing=Timing(time_scale=5000.0))
        ports = await sim.start(ports=FREE)
        lines = []
        cell = await Workcell.connect("127.0.0.1", ports, trace=lambda *a: lines.append(a))
        try:
            await cell.enable()
            return await fn(sim, cell, lines)
        finally:
            await cell.close()
            await sim.stop()
    return asyncio.run(main())


def test_one_tube_scan_weigh_heat():
    async def go(sim, cell, lines):
        sim.admin({"cmd": "load_rack", "slot": 1, "barcode": "R1", "fill": ["A1"], "content_g": 0.5})
        sim.admin({"cmd": "load_rack", "slot": 8, "barcode": "R8", "fill": []})
        await cell.pick("1 A1")
        await cell.place("scanner")
        assert await cell.scan() == "R1-A1"
        await cell.pick("scanner")
        await cell.place("balance")
        grams = await cell.weigh()
        await cell.pick("balance")
        await cell.place("heater")
        started = await cell.heat(45)
        assert started["target"] == 45.0 and started["celsius"] < 45      # returns at once, no waiting
        while not (await cell.temperature())["stable"]:
            await sim.clock.sleep(1)
        assert abs((await cell.temperature())["celsius"] - 45) <= 0.5
        await cell.heater_off()
        await cell.pick("heater")
        await cell.place("8 D5")
        vial = sim.world.racks[8].wells[(3, 4)]
        assert abs(grams - vial.gross_g) < 2e-4 and vial.peak_c >= 44.5
        assert not sim.world.racks[1].wells and sim.world.held is None
        # The devices only ever saw coordinates, never a bay or well name.
        assert not any("A1" in sent or "bay" in sent for _dev, sent, _reply in lines)
    with_cell(go)


def test_pick_from_an_empty_well_and_place_with_nothing_held():
    async def go(sim, cell, lines):
        sim.admin({"cmd": "load_rack", "slot": 2, "barcode": "R2", "fill": []})
        with pytest.raises(GripperEmpty):
            await cell.pick("2 A1")
        with pytest.raises(OperationError):
            await cell.place("2 A1")
    with_cell(go)
