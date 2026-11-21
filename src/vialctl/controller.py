"""The controller service: the one owner of the hardware, running one labware operation at a time.

It adds what a shared instrument needs on top of `operations.py`:
  - one operation at a time: a request that arrives while another runs is refused, not queued;
  - a fault latch: after an arm fault, a lost reply or a lost vial, every operation is refused until an
    operator has made the cell safe and called `clear_fault`;
  - a log of what was done.
It makes no decisions about samples. Sequencing operations into a rack job, retrying, and deciding what
to do with a tube that would not scan belong to the workflow that calls it (Orca, from stage 2 on).
"""

from __future__ import annotations

import asyncio
import inspect
import time

from .deck import Deck
from .devices import Devices
from .errors import (ArmFault, ControllerBusy, ControllerFault, DeviceError, DeviceTimeout, InvalidRequest,
                     OperationError, VialLost)
from .operations import Workcell

OPERATIONS = ("home", "pick", "place", "scan", "weigh", "tare", "heat", "temperature", "heater_off", "rack_map")


def where_words(where) -> list:
    """Accept '3 A1', ['3', 'A1'], {'bay': 3, 'well': 'A1'} or {'station': 'scanner'}."""
    if isinstance(where, dict):
        if "station" in where:
            return [str(where["station"])]
        if "bay" in where and "well" in where:
            return [str(where["bay"]), str(where["well"])]
        raise InvalidRequest(f"cannot read a place from {where!r}")
    if isinstance(where, (list, tuple)):
        return [str(w) for w in where]
    if isinstance(where, str) and where.strip():
        return where.split()
    raise InvalidRequest("missing 'where', e.g. \"3 A1\" or \"scanner\"")


class Controller:
    def __init__(self, host: str, ports: dict | None = None, deck: Deck | None = None):
        self.deck = deck or Deck.load()
        self.dev = Devices(host, ports)
        self.cell = Workcell(self.dev, self.deck)
        self.arm, self.admin = self.dev.arm, self.dev.admin
        self.fault: str | None = None
        self._lock = asyncio.Lock()
        self._events: list = []

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> "Controller":
        await self.dev.connect()
        await self.cell.enable()
        await self.cell.go("home")
        self._log("started")
        return self

    async def stop(self) -> None:
        await self.dev.close()

    def _log(self, msg: str, **kw) -> None:
        self._events.append({"t": time.time(), "msg": msg, **kw})
        del self._events[:-500]

    # ------------------------------------------------------------------ status
    def status(self) -> dict:
        return {"fault": self.fault, "busy": self._lock.locked(),
                "at": self.cell.at.name if self.cell.at else None,
                "bays": sorted(self.deck.bays), "stations": sorted(self.deck.stations),
                "operations": list(OPERATIONS)}

    def events(self, n: int = 50) -> list:
        return self._events[-n:]

    # ------------------------------------------------------------------ operations
    async def run(self, op: str, **args) -> dict:
        """Run one operation and return its result. Raises ControllerBusy / ControllerFault (nothing
        happened), InvalidRequest (can never work as asked) or the operation's own error."""
        if op not in OPERATIONS:
            raise InvalidRequest(f"unknown operation {op!r} (have {', '.join(OPERATIONS)})")
        if self.fault:
            raise ControllerFault(f"faulted: {self.fault}; clear_fault first")
        if self._lock.locked():
            raise ControllerBusy("another operation is running")
        fn = getattr(self, f"_op_{op}")
        try:
            inspect.signature(fn).bind(**args)
        except TypeError as exc:
            raise InvalidRequest(f"{op}: {exc}")
        async with self._lock:
            try:
                result = await fn(**args)
            except ValueError as exc:                    # bad address, bad well name
                raise InvalidRequest(str(exc))
            except (ArmFault, DeviceTimeout, VialLost) as exc:
                self.cell.forget_position()
                self.fault = f"{type(exc).__name__}: {exc}"
                self._log("fault", op=op, error=self.fault)
                raise
            except (OperationError, DeviceError) as exc:
                self.cell.forget_position()
                self._log("failed", op=op, args=args, error=str(exc))
                raise
            self._log(op, args=args, result=result)
            return result

    async def _op_home(self) -> dict:
        await self.cell.go("home")
        return {"at": "home"}

    async def _op_pick(self, where) -> dict:
        return {"at": (await self.cell.pick(where_words(where))).name}

    async def _op_place(self, where) -> dict:
        t = await self.cell.place(where_words(where))
        return {"at": t.name, "holding": t.docks}

    async def _op_scan(self) -> dict:
        return {"code": await self.cell.scan()}

    async def _op_weigh(self) -> dict:
        return {"grams": await self.cell.weigh()}

    async def _op_tare(self) -> dict:
        await self.cell.tare()
        return {}

    async def _op_heat(self, celsius: float) -> dict:
        """Start heating; does not wait. Whether and how long to wait is up to the workflow."""
        try:
            celsius = float(celsius)
        except (TypeError, ValueError):
            raise InvalidRequest(f"heat: {celsius!r} is not a temperature")
        try:
            return await self.cell.heat(celsius)
        except DeviceError as exc:
            if exc.code == "RANGE":
                raise InvalidRequest(f"heat: {celsius:g} C is outside the heater's range")
            if exc.code == "NO_VIAL":
                raise OperationError("heat: no vial docked in the heater")
            raise

    async def _op_temperature(self) -> dict:
        return await self.cell.temperature()

    async def _op_heater_off(self) -> dict:
        await self.cell.heater_off()
        return {}

    async def _op_rack_map(self, bay: int) -> dict:
        """Which wells of a bay hold tubes. Only the simulator can answer; real hardware would need a
        rack scan, so callers must cope with this being unavailable."""
        if self.admin is None:
            raise OperationError("no tube map available on this hardware")
        self.deck.bay(int(bay))
        r = await self.admin.call(cmd="rack_map", slot=int(bay))
        return {"bay": int(bay), "present": r["present"], "barcode": r["barcode"], "occupied": r["occupied"]}

    # ------------------------------------------------------------------ operator actions
    async def emergency_stop(self) -> None:
        await self.arm.emergency_stop()
        self.fault = "emergency stop"
        self._log("emergency stop")

    async def clear_fault(self) -> None:
        """Operator action: the arm is clear, the gripper is empty and the workcell is safe."""
        await self.arm.clear_error()
        await self.cell.enable()
        await self.cell.go("home")
        self.fault = None
        self._log("fault cleared")
