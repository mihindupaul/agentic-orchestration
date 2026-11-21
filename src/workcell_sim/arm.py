"""4-axis SCARA arm simulator with a queued motion model and a vacuum-free two-state gripper.

Robot modes follow the DOBOT V4 TCP/IP guide (4 disabled, 5 enabled/idle, 7 running,
9 error, 11 collision). Error codes returned to clients are placeholders.
"""

from __future__ import annotations

import asyncio
import math

from .config import Layout, Timing
from .kinematics import ik

MODE_DISABLED, MODE_IDLE, MODE_RUNNING, MODE_ERROR, MODE_COLLISION = 4, 5, 7, 9, 11

OK = 0
ERR_STATE = -1          # command not allowed in the current state
ERR_UNREACHABLE = -2    # target outside workspace or joint limits
ERR_Z_RANGE = -3
ERR_BAD_ARGS = -4

STEP_S = 0.01           # simulated seconds per pose update (fine enough for smooth 30 fps display)


class Arm:
    def __init__(self, world, clock, layout: Layout, timing: Timing, faults):
        self.world, self.clock, self.layout, self.timing, self.faults = world, clock, layout, timing, faults
        self.pose = list(layout.home)
        self.mode = MODE_DISABLED
        self.error_id = 0
        self.error_msg = ""
        self.speed_factor = 100
        self.gripper_closed = False
        self._enabled = False
        self._queue: asyncio.Queue = asyncio.Queue()
        self._pending = 0
        self._idle = asyncio.Event()
        self._idle.set()
        self._worker = None

    def start(self) -> None:
        self._worker = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._worker:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass

    # -------------------------------------------------------------- dashboard
    async def enable(self) -> int:
        if self.mode in (MODE_ERROR, MODE_COLLISION):
            return ERR_STATE
        await self.clock.sleep(self.timing.enable_s)
        self._enabled = True
        if self.mode == MODE_DISABLED:
            self.mode = MODE_IDLE
        return OK

    def disable(self) -> int:
        self._flush()
        self._enabled = False
        self.mode = MODE_DISABLED
        return OK

    def clear_error(self) -> int:
        self.error_id, self.error_msg = 0, ""
        self.mode = MODE_IDLE if self._enabled else MODE_DISABLED
        return OK

    def emergency_stop(self) -> int:
        self._flush()
        self._fail(MODE_ERROR, -100, "emergency_stop")
        return OK

    def halt(self) -> int:
        """Drop queued commands; the move in progress finishes."""
        self._flush_after_failure()
        return OK

    def set_speed(self, pct: int) -> int:
        if not 1 <= pct <= 100:
            return ERR_BAD_ARGS
        self.speed_factor = pct
        return OK

    async def sync(self) -> int:
        await self._idle.wait()
        return OK if self.mode not in (MODE_ERROR, MODE_COLLISION) else ERR_STATE

    # --------------------------------------------------------------- commands
    def _ready(self) -> bool:
        return self._enabled and self.mode in (MODE_IDLE, MODE_RUNNING)

    def move(self, kind: str, x: float, y: float, z: float, r: float) -> int:
        if not self._ready():
            return ERR_STATE
        if not (self.layout.z_min <= z <= self.layout.z_max):
            return ERR_Z_RANGE
        if ik(self.layout, x, y) is None:
            return ERR_UNREACHABLE
        self._enqueue(("move", kind, (x, y, z, r)))
        return OK

    def tool_do(self, index: int, value: int) -> int:
        if not self._ready():
            return ERR_STATE
        if index != 1 or value not in (0, 1):
            return ERR_BAD_ARGS
        self._enqueue(("tool", value))
        return OK

    def tool_di(self, index: int) -> int:
        return 1 if (index == 1 and self.world.held is not None) else 0

    # --------------------------------------------------------------- internals
    def _enqueue(self, item) -> None:
        self._pending += 1
        self._idle.clear()
        if self.mode == MODE_IDLE:
            self.mode = MODE_RUNNING
        self._queue.put_nowait(item)

    def _done(self) -> None:
        self._pending = max(0, self._pending - 1)
        if self._pending == 0:
            self._idle.set()
            if self.mode == MODE_RUNNING:
                self.mode = MODE_IDLE

    def _flush(self) -> None:
        while not self._queue.empty():
            self._queue.get_nowait()
        self._pending = 0
        self._idle.set()

    def _fail(self, mode: int, code: int, msg: str) -> None:
        self.mode, self.error_id, self.error_msg = mode, code, msg
        self.world.log("arm_error", code=code, msg=msg, pose=[round(v, 1) for v in self.pose])

    @property
    def queue_len(self) -> int:
        return self._pending

    async def _run(self) -> None:
        while True:
            item = await self._queue.get()
            try:
                if self.mode in (MODE_ERROR, MODE_COLLISION, MODE_DISABLED):
                    continue                     # queue was invalidated by a failure
                if item[0] == "move":
                    await self._do_move(item[2])
                else:
                    await self._do_tool(item[1])
            finally:
                self._done()

    def _collision_reason(self, target) -> str:
        lay = self.layout
        sx, sy, sz, _ = self.pose
        tx, ty, tz, _ = target
        if math.hypot(tx - sx, ty - sy) > 2.0 and min(sz, tz) < lay.clearance_z:
            return "low_lateral_move"
        if tz <= lay.tube_top_z:
            hit = self.world.locate(tx, ty)
            if hit and hit[0] == "rack":
                return "rack_strike"
            if hit and hit[0] == "well" and self.world.held is not None:
                _, rack, r, c = hit
                if (r, c) in rack.wells:
                    return "occupied_well"
            if (self.world.station_at(tx, ty) == "balance" and self.world.pan is not None
                    and self.world.held is not None):
                return "occupied_pan"
        return ""

    async def _do_move(self, target) -> None:
        t = self.timing
        sx, sy, sz, sr = self.pose
        tx, ty, tz, tr = target
        sf = self.speed_factor / 100.0
        dur = (math.hypot(tx - sx, ty - sy) / (t.xy_speed * sf)
               + abs(tz - sz) / (t.z_speed * sf) + t.overhead_s)
        reason = self._collision_reason(target)
        if not reason and self.faults.consume("arm_collision"):
            reason = "injected_collision"
        stop_frac = 0.6 if reason else 1.0
        steps = max(1, int(dur * stop_frac / STEP_S))
        for i in range(1, steps + 1):
            if self.mode in (MODE_ERROR, MODE_COLLISION, MODE_DISABLED):
                return
            f = stop_frac * i / steps
            self.pose = [sx + (tx - sx) * f, sy + (ty - sy) * f, sz + (tz - sz) * f, sr + (tr - sr) * f]
            await self.clock.sleep(dur * stop_frac / steps)
        if reason:
            self._flush_after_failure()
            self._fail(MODE_COLLISION, -200, reason)

    def _flush_after_failure(self) -> None:
        while not self._queue.empty():
            self._queue.get_nowait()
            self._pending = max(0, self._pending - 1)

    async def _do_tool(self, value: int) -> None:
        await self.clock.sleep(self.timing.gripper_s)
        if self.mode in (MODE_ERROR, MODE_COLLISION, MODE_DISABLED):
            return
        if value == 1:
            self.gripper_closed = True
            if self.faults.consume("gripper_miss"):
                self.world.log("gripper_miss")
                return
            outcome = self.world.grip(self.pose)
            if outcome == "gripped" and self.faults.consume("gripper_slip"):
                await self.clock.sleep(0.2)
                self.world.lose_held("slip")
        else:
            self.gripper_closed = False
            self.world.release(self.pose)
