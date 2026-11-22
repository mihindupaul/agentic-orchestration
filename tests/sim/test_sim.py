"""The simulator on its own: device protocols and physics, driven with raw wire lines.

Positions come from the simulator's own world model (ground truth); a client would take them from the
controller's deck file instead (see tests/controller/test_deck.py)."""

import asyncio
import json
import math
import re

from workcell_sim import Simulator, Timing
from workcell_sim.kinematics import fk, ik
from workcell_sim.config import Layout

FREE = {k: 0 for k in ("arm_dashboard", "arm_motion", "arm_feedback", "balance", "scanner", "heater", "admin")}


class Line:
    def __init__(self, reader, writer):
        self.r, self.w = reader, writer

    async def ask(self, text: str) -> str:
        self.w.write((text + "\n").encode())
        await self.w.drain()
        return (await self.r.readline()).decode().strip()


class Raw:
    """One raw line connection per device port: just enough to poke the simulator in tests."""

    async def connect(self, ports: dict) -> "Raw":
        self._writers = []
        for name in ("arm_dashboard", "arm_motion", "balance", "scanner", "heater", "admin"):
            r, w = await asyncio.open_connection("127.0.0.1", ports[name])
            self._writers.append(w)
            setattr(self, name.removeprefix("arm_"), Line(r, w))
        self.dash = self.dashboard
        return self

    async def close(self) -> None:
        for w in self._writers:
            w.close()
            await w.wait_closed()

    async def arm(self, cmd: str) -> tuple:
        port = self.motion if cmd.split("(")[0] in ("MovL", "MovJ", "ToolDO", "Sync") else self.dash
        m = re.match(r"(-?\d+),\{(.*?)\},", await port.ask(cmd))
        return int(m.group(1)), m.group(2)

    async def arm_ok(self, cmd: str) -> str:
        code, vals = await self.arm(cmd)
        assert code == 0, (cmd, code)
        return vals

    async def goto(self, x: float, y: float, z: float) -> None:
        await self.arm_ok(f"MovL({x:.3f},{y:.3f},{z:.3f},0)")

    async def sync(self) -> None:
        await self.arm_ok("Sync()")

    async def adm(self, **req) -> dict:
        return json.loads(await self.admin.ask(json.dumps(req)))


def well(sim, slot, row=0, col=0):
    return sim.world.well_xy(sim.world.racks[slot], row, col)


def run(coro_fn):
    async def main():
        sim = Simulator(timing=Timing(time_scale=2000.0))
        ports = await sim.start(ports=FREE)
        orch = await Raw().connect(ports)
        try:
            await orch.arm_ok("EnableRobot()")
            return await coro_fn(sim, orch)
        finally:
            await orch.close()
            await sim.stop()
    return asyncio.run(main())


def test_layout_is_reachable():
    lay, sim = Layout(), Simulator()
    for name in lay.stations:
        assert ik(lay, *lay.station_xy(name)) is not None, name
    assert ik(lay, *lay.home[:2]) is not None
    for slot, rack in sim.world.racks.items():
        for row in (0, 7):
            for col in (0, 11):
                assert ik(lay, *sim.world.well_xy(rack, row, col)) is not None, (slot, row, col)


def test_ik_fk_roundtrip():
    lay = Layout()
    for x, y in [(0, 215), (-138, 165), (100, 300), (-250, 200)]:
        j = ik(lay, x, y)
        assert j is not None
        fx, fy = fk(lay, *j)
        assert math.hypot(fx - x, fy - y) < 1e-6


def test_motion_refused_when_disabled():
    async def job(sim, orch):
        await orch.arm_ok("DisableRobot()")
        code, _ = await orch.arm("MovL(0,215,100,0)")
        assert code == -1
    run(job)


def test_unreachable_and_unknown_command():
    async def job(sim, orch):
        assert (await orch.arm("MovL(0,700,100,0)"))[0] == -2
        raw = await orch.motion.ask("Mov(1,2,3,4)")
        assert raw.startswith("-10000,")
    run(job)


def test_low_lateral_move_collides_and_needs_clear_error():
    async def job(sim, orch):
        await orch.goto(0, 215, 100)
        await orch.goto(0, 215, 30)          # vertical descent is fine
        await orch.sync()
        code, _ = await orch.arm("MovL(100,250,30,0)")
        assert code == 0                       # accepted, fails while executing
        await asyncio.sleep(0.3)
        assert (await orch.arm("RobotMode()"))[1] == "11"
        assert (await orch.arm("MovL(0,215,100,0)"))[0] == -1
        await orch.arm_ok("ClearError()")
        assert (await orch.arm("RobotMode()"))[1] == "5"
        assert (await orch.arm("MovL(0,215,100,0)"))[0] == 0
    run(job)


def test_scanner_noread_fault_is_one_shot():
    async def job(sim, orch):
        await orch.adm(cmd="load_rack", slot=1, barcode="R1", fill=["A1"])
        scn, src = sim.layout.station_xy("scanner"), well(sim, 1)
        await orch.goto(*src, 100)
        await orch.goto(*src, 20)
        await orch.arm_ok("ToolDO(1,1)")
        await orch.goto(*src, 100)
        await orch.goto(*scn, 100)
        await orch.goto(*scn, 10)
        await orch.sync()
        await orch.adm(cmd="inject", fault="scanner_noread")
        assert await orch.scanner.ask("TRIG") == "NOREAD"
        assert await orch.scanner.ask("TRIG") == "OK,R1-A1"
    run(job)


def test_scan_without_vial_and_gripper_slip():
    async def job(sim, orch):
        assert await orch.scanner.ask("TRIG") == "ERR,NO_VIAL"
        assert (await orch.heater.ask("SET,60")) == "ERR,NO_VIAL"
        await orch.adm(cmd="load_rack", slot=1, barcode="R1", fill=["A1"])
        src = well(sim, 1)
        await orch.adm(cmd="inject", fault="gripper_slip")
        await orch.goto(*src, 100)
        await orch.goto(*src, 20)
        await orch.arm_ok("ToolDO(1,1)")
        await orch.sync()
        await asyncio.sleep(0.05)
        assert await orch.arm_ok("ToolDI(1)") == "0"
        events = (await orch.adm(cmd="events"))["events"]
        assert any(e["event"] == "vial_dropped" for e in events)
    run(job)


# ------------------------------------------------------------------ heater
async def dock_in_heater(sim, orch):
    await orch.adm(cmd="load_rack", slot=1, barcode="R1", fill=["A1"])
    src, (hx, hy) = well(sim, 1), sim.layout.station_xy("heater")
    await orch.goto(*src, 100)
    await orch.goto(*src, 20)
    await orch.arm_ok("ToolDO(1,1)")
    await orch.goto(*src, 100)
    await orch.goto(hx, hy, 100)
    await orch.goto(hx, hy, 10)
    await orch.sync()


async def temp(orch) -> tuple:
    _t, current, target, state = (await orch.heater.ask("TEMP")).split(",")
    return float(current), float(target), state


async def heat_until(sim, orch, seconds, step=0.5) -> list:
    """Poll TEMP every `step` simulated seconds; return (sim time, current, target, state) readings."""
    readings, end = [], sim.clock.now() + seconds
    while sim.clock.now() < end:
        await sim.clock.sleep(step)
        readings.append((sim.clock.now(), *await temp(orch)))
    return readings


def test_heater_refuses_without_vial_and_out_of_range():
    async def job(sim, orch):
        assert await orch.heater.ask("SET,60") == "ERR,NO_VIAL"
        assert await orch.heater.ask("TEMP") == "T,22.0,22.0,OFF"
        await dock_in_heater(sim, orch)
        assert await orch.heater.ask("SET,24") == "ERR,RANGE"
        assert await orch.heater.ask("SET,101") == "ERR,RANGE"
        assert await orch.heater.ask("SET,hot") == "ERR,BAD_VALUE"
        assert await orch.heater.ask("STATUS") == "OK,OFF,VIAL"
    run(job)


def test_heater_ramps_then_becomes_stable_then_cools():
    async def job(sim, orch):
        await dock_in_heater(sim, orch)
        s0 = sim.clock.now()
        assert await orch.heater.ask("SET,60") == "OK,60.0"
        s1 = sim.clock.now()
        await sim.clock.sleep(4)
        t0 = sim.clock.now()
        current, target, state = await temp(orch)
        t1 = sim.clock.now()
        # Far from the target the well heats at the ramp limit, 2 C per simulated second.
        assert 22 + 2 * (t0 - s1) - 0.2 <= current <= 22 + 2 * (t1 - s0) + 0.2
        assert target == 60.0 and state == "HEATING"
        readings = await heat_until(sim, orch, 60)
        t_stable = next(t for t, _c, _t, st in readings if st == "STABLE")
        assert all(abs(c - 60) <= 0.5 for t, c, _t, _s in readings if t_stable - 3 <= t < t_stable)
        assert not any(st == "STABLE" for t, _c, _t, st in readings if t < t_stable - 0.01)
        assert max(c for _t, c, _tt, _s in readings) <= 60.05                                  # no overshoot
        assert sim.state()["heater"]["state"] == "STABLE" and sim.state()["heater"]["target"] == 60.0
        assert sim.world.held.peak_c >= 59.5
        assert await orch.heater.ask("OFF") == "OK,OFF"
        await sim.clock.sleep(10)
        current, target, state = await temp(orch)
        assert 22 < current < 50 and target == 22.0 and state == "OFF"
    run(job)


def test_heater_timeout_fault_plateaus_short_of_target():
    async def job(sim, orch):
        await dock_in_heater(sim, orch)
        await orch.adm(cmd="inject", fault="heater_timeout")
        await orch.heater.ask("SET,60")
        readings = await heat_until(sim, orch, 120, step=2)
        assert all(state == "HEATING" for _t, _c, _tt, state in readings)
        assert abs(readings[-1][1] - 55.0) < 0.3
        await orch.heater.ask("SET,60")                      # one-shot: the next SET heats normally
        assert (await heat_until(sim, orch, 60, step=2))[-1][3] == "STABLE"
    run(job)


def test_heater_overshoot_fault_peaks_then_settles():
    async def job(sim, orch):
        await dock_in_heater(sim, orch)
        await orch.adm(cmd="inject", fault="heater_overshoot")
        await orch.heater.ask("SET,60")
        readings = await heat_until(sim, orch, 150)
        peak = max(c for _t, c, _tt, _s in readings)
        assert 66.5 <= peak <= 68.5                          # about 8 C over, give or take the polling step
        assert readings[-1][3] == "STABLE" and abs(readings[-1][1] - 60) <= 0.5
    run(job)


def _rack_corners(lay, cx, cy, phi):
    p = math.radians(phi)
    e_l, e_s = (math.sin(p), math.cos(p)), (math.cos(p), -math.sin(p))
    return [(cx + a * lay.rack_len / 2 * e_l[0] + b * lay.rack_wid / 2 * e_s[0],
             cy + a * lay.rack_len / 2 * e_l[1] + b * lay.rack_wid / 2 * e_s[1]) for a in (-1, 1) for b in (-1, 1)]


def _on_deck(lay, x, y, margin=0.0):
    half = lay.deck_size / 2
    return abs(x) <= half - margin and lay.deck_back_y + margin <= y <= lay.deck_back_y + lay.deck_size - margin


def test_bays_and_wells_fit_the_deck_without_overlap():
    lay = Layout()
    boxes = {}
    for slot, (cx, cy, phi) in lay.rack_bays.items():
        corners = _rack_corners(lay, cx, cy, phi)
        assert all(_on_deck(lay, x, y, margin=5) for x, y in corners), slot
        xs, ys = [c[0] for c in corners], [c[1] for c in corners]
        boxes[slot] = (min(xs), max(xs), min(ys), max(ys))
    for a in boxes:
        for b in boxes:
            if a < b:
                (ax0, ax1, ay0, ay1), (bx0, bx1, by0, by1) = boxes[a], boxes[b]
                assert ax1 <= bx0 or bx1 <= ax0 or ay1 <= by0 or by1 <= ay0, (a, b)
    r = lay.station_well_d / 2
    for name, (sx, sy) in lay.stations.items():
        assert _on_deck(lay, sx, sy, margin=r + 5), name
        for slot, (x0, x1, y0, y1) in boxes.items():
            nx, ny = min(max(sx, x0), x1), min(max(sy, y0), y1)
            assert math.hypot(sx - nx, sy - ny) >= r, (name, slot)
