"""Labware operations: what a person means ("pick the vial in bay 3, A1") turned into device commands.

The devices are deliberately simple: the arm moves to x, y, z and opens or closes its gripper, the
instruments read or heat whatever is in front of them. Everything that needs to know the deck lives
here, on the controller side: where bays and stations are (`Deck`), which height to work at, and the
sequence of moves that makes a pick or a place safe.

    cell = await Workcell.connect("192.168.64.4")
    await cell.enable()
    await cell.pick("3 A1")
    await cell.place("scanner")       # docks the vial and keeps holding it
    code = await cell.scan()
"""

from __future__ import annotations

from .deck import Deck, Target
from .devices import Devices
from .errors import GripperEmpty, OperationError, VialLost


class Workcell:
    def __init__(self, devices: Devices, deck: Deck):
        self.dev, self.deck = devices, deck
        self.arm = devices.arm
        self.at: Target | None = None      # last place the arm was sent to
        self.xy: tuple | None = None       # last commanded position; None when unknown (after an error)
        self.z: float | None = None

    @classmethod
    async def connect(cls, host: str, ports: dict | None = None, deck: Deck | None = None,
                      trace=None) -> "Workcell":
        return cls(await Devices(host, ports, trace).connect(), deck or Deck.load())

    async def close(self) -> None:
        await self.dev.close()

    def target(self, where) -> Target:
        return where if isinstance(where, Target) else self.deck.resolve(where)

    def forget_position(self) -> None:
        """Call after a failed command: the arm may have stopped anywhere, so lift before the next move."""
        self.xy = self.z = None

    async def _read_pose(self) -> None:
        x, y, z, _r = await self.arm.pose()
        self.xy, self.z = (x, y), z

    # ------------------------------------------------------------------ arm
    async def enable(self) -> None:
        await self.arm.enable()
        await self._read_pose()

    async def go(self, where) -> Target:
        """Lift where we are, travel at safe height, wait until there."""
        t = self.target(where)
        safe = self.deck.safe_z
        if self.xy is None:
            await self._read_pose()
        if t.xy != self.xy or self.z != safe:
            if self.z != safe:
                await self.arm.move_l(*self.xy, safe)
            await self.arm.move_l(*t.xy, safe)
            await self.arm.sync()
        self.at, self.xy, self.z = t, t.xy, safe
        return t

    async def _to_z(self, z: float) -> None:
        if self.xy is None:
            await self._read_pose()
        if z != self.z:
            await self.arm.move_l(*self.xy, z)
            await self.arm.sync()
            self.z = z

    async def down(self) -> None:
        """Lower to the working height of the current place (dock height in a docking well)."""
        if self.at is None:
            raise OperationError("move somewhere first")
        await self._to_z(self.at.work_z)

    async def up(self) -> None:
        await self._to_z(self.deck.safe_z)

    async def grip(self) -> bool:
        """Close the gripper; True when it holds a vial."""
        await self.arm.tool(True)
        await self.arm.sync()
        return await self.arm.holding()

    async def release(self) -> None:
        await self.arm.tool(False)
        await self.arm.sync()

    async def holding(self) -> bool:
        return await self.arm.holding()

    # ---------------------------------------------------------------- vials
    async def pick(self, where) -> Target:
        """Take a vial: from a rack well or the balance pan (open, lower, grip, check, lift), or lift the
        vial that is docked in a scanner or heater well."""
        t = self.target(where)
        if t.docks:
            if self.at != t:
                await self.go(t)
            await self.up()
            if not await self.arm.holding():
                raise VialLost(f"no vial in the gripper after lifting out of the {t.name}")
            return t
        if await self.arm.holding():
            raise OperationError("already holding a vial")
        await self.go(t)
        await self.arm.tool(False)
        await self._to_z(t.work_z)
        held = await self.grip()
        await self.up()
        if not held:
            raise GripperEmpty(f"no vial picked at {t.name}")
        if not await self.arm.holding():
            raise VialLost(f"vial slipped out while lifting from {t.name}")
        return t

    async def place(self, where) -> Target:
        """Put the held vial into a rack well or onto the balance and let go; in a scanner or heater well,
        lower it in and keep holding it so the device can work on it."""
        t = self.target(where)
        if not await self.arm.holding():
            raise OperationError("not holding a vial")
        await self.go(t)
        await self._to_z(t.work_z)
        if t.docks:
            return t
        await self.release()
        await self.up()
        return t

    # ---------------------------------------------------------- instruments
    async def scan(self) -> str | None:
        """Read the code of the vial in the scanner; None for a no-read."""
        return await self.dev.scanner.trigger()

    async def heat(self, celsius: float) -> dict:
        """Start heating the heater well toward `celsius` and return straight away; waiting for the
        temperature to settle is the caller's decision (see `temperature`)."""
        target = await self.dev.heater.set(celsius)
        current, _target, _state = await self.dev.heater.read()
        return {"target": target, "celsius": current}

    async def temperature(self) -> dict:
        current, target, state = await self.dev.heater.read()
        return {"celsius": current, "target": target, "state": state, "stable": state == "STABLE"}

    async def heater_off(self) -> None:
        await self.dev.heater.off()

    async def weigh(self) -> float:
        """Stable reading in grams."""
        return await self.dev.balance.weigh_stable()

    async def tare(self) -> None:
        await self.dev.balance.tare()
