"""Balance, 2D-code scanner well and heated well.

Wire protocols are generic, newline-terminated ASCII invented for this project.
Replace the `handle` methods to match the protocol of whichever instruments you connect.
"""

from __future__ import annotations

import random


class Balance:
    """Weighing pan. `S` waits for a stable value, `SI` answers immediately."""

    def __init__(self, world, clock, timing, faults, seed: int = 2):
        self.world, self.clock, self.timing, self.faults = world, clock, timing, faults
        self.rng = random.Random(seed)
        self.tare_g = 0.0

    def _gross(self) -> float:
        pan = self.world.pan
        return pan.gross_g if pan else 0.0

    def _settle_left(self) -> float:
        return self.timing.balance_settle_s - (self.clock.now() - self.world.pan_since)

    def _stable(self) -> bool:
        return self._settle_left() <= 0

    def _reading(self) -> float:
        return self._gross() - self.tare_g + self.rng.gauss(0.0, 0.00003)

    async def handle(self, line: str) -> str:
        cmd = line.strip().upper()
        if cmd == "SI":
            flag = "S" if self._stable() else "D"
            return f"{flag},{self._reading():.5f},g"
        if cmd == "S":
            if self.faults.consume("balance_unstable"):
                await self.clock.sleep(self.timing.balance_settle_s * 3)
                return "ERR,TIMEOUT"
            wait = self._settle_left()
            if wait > 0:
                await self.clock.sleep(wait)
            return f"S,{self._reading():.5f},g"
        if cmd == "Z":
            self.tare_g = self._gross()
            return "OK,TARED"
        if cmd == "STATUS":
            return f"OK,{'LOADED' if self.world.pan else 'EMPTY'},{'STABLE' if self._stable() else 'SETTLING'}"
        return "ERR,UNKNOWN_COMMAND"


class Scanner:
    """2D-code reader looking up into its well from below. Reads a vial set down in the well, or held down in
    it by the gripper."""

    def __init__(self, world, clock, timing, faults, arm):
        self.world, self.clock, self.timing, self.faults, self.arm = world, clock, timing, faults, arm
        self.busy = False

    async def handle(self, line: str) -> str:
        cmd = line.strip().upper()
        if cmd == "STATUS":
            present = self.world.vial_at("scanner", self.arm.pose) is not None
            return f"OK,{'BUSY' if self.busy else 'IDLE'},{'VIAL' if present else 'EMPTY'}"
        if cmd != "TRIG":
            return "ERR,UNKNOWN_COMMAND"
        if self.busy:
            return "ERR,BUSY"
        vial = self.world.vial_at("scanner", self.arm.pose)
        if vial is None:
            return "ERR,NO_VIAL"
        self.busy = True
        try:
            await self.clock.sleep(self.timing.scan_s)
            if self.world.vial_at("scanner", self.arm.pose) is not vial:
                return "ERR,VIAL_LOST"
            if self.faults.consume("scanner_noread") or vial.code_quality < 0.5:
                self.world.log("scan", vial=vial.vid, result="noread")
                return "NOREAD"
            self.world.log("scan", vial=vial.vid, result="ok", code=vial.barcode)
            return f"OK,{vial.barcode}"
        finally:
            self.busy = False


class Heater:
    """Heated well. A vial set down in the well (or held down in it by the gripper) sits in a block that
    is driven toward a set temperature. Setting it down frees the arm while the vial heats.

    STATUS            -> OK,<OFF|HEATING|STABLE>,<VIAL|EMPTY>
    SET,<celsius>     -> OK,<target>        (25-100 C, needs a vial in the well; ERR,RANGE / ERR,NO_VIAL)
    OFF               -> OK,OFF             (cools back toward ambient)
    TEMP              -> T,<current>,<target>,<state>   (target is ambient when off)

    The temperature is worked out from the simulated clock whenever it is asked for: it follows the
    setpoint with a first-order lag, never faster than the ramp rate, and counts as STABLE after staying
    within the band for the hold time. Faults are consumed by the next SET.
    """

    STEP_S = 0.05

    def __init__(self, world, clock, spec, faults, arm):
        self.world, self.clock, self.spec, self.faults, self.arm = world, clock, spec, faults, arm
        self.celsius = spec.ambient_c
        self.target: float | None = None
        self._setpoint = spec.ambient_c      # what the block is actually driven toward
        self._overshoot = False              # fault: aim above the target until the peak is reached
        self._plateau = False                # fault: never get closer than timeout_short_c
        self._in_band_s = 0.0
        self._t = clock.now()

    @property
    def state(self) -> str:
        self.update()
        if self.target is None:
            return "OFF"
        return "STABLE" if self._in_band_s >= self.spec.stable_hold_s else "HEATING"

    def update(self) -> None:
        """Advance the temperature to the current simulated time."""
        now, sp = self.clock.now(), self.spec
        elapsed, self._t = now - self._t, now
        if elapsed > 1800:                    # long idle: everything has settled, skip the integration
            self.celsius = self._setpoint
            elapsed = sp.stable_hold_s
        steps = max(0, int(elapsed / self.STEP_S + 0.5))
        dt = elapsed / steps if steps else 0.0
        for _ in range(steps):
            rate = (self._setpoint - self.celsius) / sp.lag_s
            rate = max(-sp.ramp_c_per_s, min(sp.ramp_c_per_s, rate))
            self.celsius += rate * dt
            if self._overshoot and self.celsius >= self.target + sp.overshoot_c - 0.05:
                self._overshoot, self._setpoint = False, self.target
            if self.target is not None and abs(self.celsius - self.target) <= sp.stable_band_c:
                self._in_band_s += dt
            else:
                self._in_band_s = 0.0
        vial = self.vial()
        if vial is not None:
            vial.peak_c = round(max(vial.peak_c or self.celsius, self.celsius), 2)

    def vial(self):
        return self.world.vial_at("heater", self.arm.pose)

    def _set(self, target: float) -> None:
        sp = self.spec
        self.target, self._in_band_s = target, 0.0
        self._plateau = self.faults.consume("heater_timeout")
        self._overshoot = not self._plateau and self.faults.consume("heater_overshoot")
        if self._plateau:
            self._setpoint = target - sp.timeout_short_c
        elif self._overshoot:
            self._setpoint = target + sp.overshoot_c + 0.5
        else:
            self._setpoint = target

    async def handle(self, line: str) -> str:
        cmd, _, arg = line.strip().partition(",")
        cmd = cmd.upper()
        self.update()
        if cmd == "STATUS":
            return f"OK,{self.state},{'VIAL' if self.vial() is not None else 'EMPTY'}"
        if cmd == "TEMP":
            target = self.spec.ambient_c if self.target is None else self.target
            return f"T,{self.celsius:.1f},{target:.1f},{self.state}"
        if cmd == "OFF":
            self.target, self._setpoint, self._overshoot, self._in_band_s = None, self.spec.ambient_c, False, 0.0
            self.world.log("heater", state="off")
            return "OK,OFF"
        if cmd != "SET":
            return "ERR,UNKNOWN_COMMAND"
        try:
            target = float(arg)
        except ValueError:
            return "ERR,BAD_VALUE"
        if not self.spec.min_c <= target <= self.spec.max_c:
            return "ERR,RANGE"
        if self.vial() is None:
            return "ERR,NO_VIAL"
        self._set(target)
        self.world.log("heater", state="set", target=target)
        return f"OK,{target:.1f}"
