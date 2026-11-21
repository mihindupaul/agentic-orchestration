"""Orca drivers for the workcell: the live one (talks to the vialctl controller) and a simulated one.

Both implement cheshire-drivers' `BaseDriver` plus one method per controller operation. The live driver
is thin on purpose: every call goes through the Orca-free `VialStationCore`, and this module only turns
its outcomes into Orca's driver errors:

    CoreRefused  -> DriverRefusedError         nothing happened, busy or faulted: ask again later
    CoreRejected -> DriverRejectedError        can never work as asked: do not ask again
    CoreFailed   -> DriverError (FAILED)       something went wrong in the cell: someone has to look
    CoreUnknown  -> DriverOutcomeUnknownError  the answer was lost: check before retrying

Results a workflow can branch on (a no-read, a heater not yet stable, an empty well) are returned as
data, not raised.
"""

from __future__ import annotations

from cheshire_drivers.command_timings import command_timing
from cheshire_drivers.driver_errors import (DriverError, DriverOutcomeUnknownError, DriverRefusedError,
                                            DriverRejectedError)
from cheshire_drivers.interfaces import BaseDriver

from .station_core import CoreFailed, CoreRefused, CoreRejected, CoreUnknown, StationConfig, VialStationCore


class IVialWorkcellDriver(BaseDriver):
    """The operations the workcell offers Orca. Places are "3 A1", "scanner", or bay + well."""

    async def pick(self, where: str | None = None, *, bay: int | None = None, well: str | None = None) -> dict:
        raise NotImplementedError

    async def place(self, where: str | None = None, *, bay: int | None = None, well: str | None = None) -> dict:
        raise NotImplementedError

    async def scan(self) -> dict:
        raise NotImplementedError

    async def weigh(self) -> dict:
        raise NotImplementedError

    async def tare(self) -> dict:
        raise NotImplementedError

    async def heat(self, celsius: float) -> dict:
        raise NotImplementedError

    async def temperature(self) -> dict:
        raise NotImplementedError

    async def heater_off(self) -> dict:
        raise NotImplementedError

    async def home(self) -> dict:
        raise NotImplementedError

    async def rack_map(self, bay: int) -> dict:
        raise NotImplementedError

    async def rack_placed(self, site: str | None, name: str, barcode: str | None = None) -> None:
        raise NotImplementedError

    async def rack_picked(self, site: str | None) -> None:
        raise NotImplementedError


def _place_params(where, bay, well) -> dict:
    if where is not None:
        return {"where": where}
    return {"bay": bay, "well": well}


class VialWorkcellDriver(IVialWorkcellDriver):
    """Live driver: the vialctl controller service (and, in simulation, the simulator's admin port)."""

    def __init__(self, config: StationConfig | None = None, core: VialStationCore | None = None):
        self.core = core or VialStationCore(config)
        self._ready = False

    # ---- BaseDriver --------------------------------------------------------------------------
    @property
    def is_initialized(self) -> bool:
        return self._ready

    @command_timing(typical=1.0, max=30.0)
    async def initialize(self) -> None:
        await self.connect()
        self._ready = True

    async def connect(self) -> None:
        await _guard(self.core.connect())

    async def disconnect(self) -> None:
        self._ready = False
        await self.core.close()

    async def open(self) -> None:          # no doors on this cell
        return None

    async def close(self) -> None:
        return None

    async def _shutdown(self) -> None:
        await self.disconnect()

    # ---- operations --------------------------------------------------------------------------
    async def _do(self, action: str, params: dict | None = None) -> dict:
        return await _guard(self.core.execute(action, params or {}))

    @command_timing(typical=8.0, max=60.0)
    async def pick(self, where=None, *, bay=None, well=None) -> dict:
        return await self._do("pick", _place_params(where, bay, well))

    @command_timing(typical=8.0, max=60.0)
    async def place(self, where=None, *, bay=None, well=None) -> dict:
        return await self._do("place", _place_params(where, bay, well))

    @command_timing(typical=1.0, max=15.0)
    async def scan(self) -> dict:
        return await self._do("scan")

    @command_timing(typical=3.0, max=30.0)
    async def weigh(self) -> dict:
        return await self._do("weigh")

    @command_timing(typical=0.5, max=10.0)
    async def tare(self) -> dict:
        return await self._do("tare")

    @command_timing(typical=0.5, max=10.0)
    async def heat(self, celsius: float) -> dict:
        return await self._do("heat", {"celsius": celsius})

    @command_timing(typical=0.2, max=10.0)
    async def temperature(self) -> dict:
        return await self._do("temperature")

    @command_timing(typical=0.2, max=10.0)
    async def heater_off(self) -> dict:
        return await self._do("heater_off")

    @command_timing(typical=3.0, max=30.0)
    async def home(self) -> dict:
        return await self._do("home")

    @command_timing(typical=0.2, max=10.0)
    async def rack_map(self, bay: int) -> dict:
        return await self._do("rack_map", {"bay": bay})

    async def rack_placed(self, site, name, barcode=None) -> None:
        await _guard(self.core.rack_placed(site, barcode, name))

    async def rack_picked(self, site) -> None:
        await _guard(self.core.rack_picked(site))


async def _guard(coro):
    try:
        return await coro
    except CoreRefused as exc:
        raise DriverRefusedError(str(exc)) from exc
    except CoreRejected as exc:
        raise DriverRejectedError(str(exc)) from exc
    except CoreUnknown as exc:
        raise DriverOutcomeUnknownError(str(exc)) from exc
    except CoreFailed as exc:
        raise DriverError(str(exc)) from exc


class SimVialWorkcellDriver(IVialWorkcellDriver):
    """In-memory stand-in for Orca's pure-simulation runs: no controller, no simulator, instant answers.

    It keeps just enough state (racks, the gripper, the heater) to give the same shape of results and the
    same refusals as the live driver, so a workflow can be dry-run in Orca before it touches the cell."""

    STATIONS = ("scanner", "balance", "heater")

    def __init__(self, name: str = "vial_workcell"):
        self.name = name
        self._ready = False
        # Same starting deck as the simulator's demo racks: a full rack in bay 3, an empty one in bay 8.
        self.racks: dict = {3: {"barcode": "RACK003", "wells": {f"{r}{c}" for r in "ABCDEFGH" for c in range(1, 13)}},
                            8: {"barcode": "RACK008", "wells": set()}}
        self.held: str | None = None    # code of the vial in the gripper
        self.in_well: dict = {"scanner": None, "balance": None, "heater": None}   # vials set down in stations
        self.at: str | None = None
        self.target: float | None = None

    @property
    def is_initialized(self) -> bool:
        return self._ready

    async def initialize(self) -> None:
        self._ready = True

    async def open(self) -> None:
        return None

    async def close(self) -> None:
        return None

    def _where(self, where, bay, well) -> tuple:
        if where is not None:
            parts = str(where).split()
            if len(parts) == 1 and parts[0] in self.STATIONS:
                return parts[0], None, None
            if len(parts) == 2 and parts[0].isdigit():
                bay, well = int(parts[0]), parts[1]
            else:
                raise DriverRejectedError(f"unknown place {where!r}")
        if bay is None or well is None:
            raise DriverRejectedError("need `where`, or a bay and a well")
        if not 1 <= int(bay) <= 8:
            raise DriverRejectedError(f"no bay {bay}")
        return f"bay {bay} {str(well).upper()}", int(bay), str(well).upper()

    async def pick(self, where=None, *, bay=None, well=None) -> dict:
        name, bay, well = self._where(where, bay, well)
        if bay is None:                                  # take it from a station well
            if self.held is not None:
                raise DriverRejectedError("already holding a vial")
            if self.in_well[name] is None:
                raise DriverError(f"no vial at the {name}")
            self.held, self.in_well[name] = self.in_well[name], None
            self.at = name
            return {"at": name, "picked": True}
        if self.held is not None:
            raise DriverRejectedError("already holding a vial")
        rack = self._rack(bay)
        self.at = name
        if not rack or well not in rack["wells"]:
            return {"picked": False, "reason": f"no vial picked at {name}"}
        rack["wells"].discard(well)
        self.held = f"{rack['barcode']}-{well}"
        return {"at": name, "picked": True}

    async def place(self, where=None, *, bay=None, well=None) -> dict:
        name, bay, well = self._where(where, bay, well)
        if self.held is None:
            raise DriverRejectedError("not holding a vial")
        self.at = name
        if bay is None:                                  # set it down in a station well
            self.in_well[name], self.held = self.held, None
            return {"at": name, "holding": False}
        if bay not in self.racks:
            raise DriverError(f"no rack in bay {bay}: vial dropped")
        self.racks[bay]["wells"].add(well)
        self.held = None
        return {"at": name, "holding": False}

    async def scan(self) -> dict:
        code = self.in_well["scanner"]
        return {"code": code, "read": code is not None}

    async def weigh(self) -> dict:
        return {"grams": 1.8}

    async def tare(self) -> dict:
        return {}

    async def heat(self, celsius: float) -> dict:
        if self.in_well["heater"] is None:
            raise DriverRejectedError("heat: no vial in the heater")
        if not 25 <= float(celsius) <= 100:
            raise DriverRejectedError(f"heat: {celsius} C is outside the heater's range")
        self.target = float(celsius)
        return {"target": self.target, "celsius": 22.0}

    async def temperature(self) -> dict:
        if self.target is None:
            return {"celsius": 22.0, "target": None, "state": "OFF", "stable": False}
        return {"celsius": self.target, "target": self.target, "state": "STABLE", "stable": True}

    async def heater_off(self) -> dict:
        self.target = None
        return {}

    async def home(self) -> dict:
        self.at = "home"
        return {"at": "home"}

    async def rack_map(self, bay: int) -> dict:
        rack = self._rack(int(bay))
        return {"bay": int(bay), "present": not rack.get("absent", False), "barcode": rack["barcode"],
                "occupied": sorted(rack["wells"])}

    def _rack(self, bay: int) -> dict:
        """The rack in a bay; a bay without one reads as an empty, absent rack."""
        return self.racks.get(bay, {"barcode": "", "wells": set(), "absent": True})

    async def rack_placed(self, site, name, barcode=None) -> None:
        from .station_core import parse_slot
        # Orca's pure-simulation runs place racks without calling this; when it is called, the rack is full.
        self.racks[parse_slot(site)] = {"barcode": barcode or name, "wells": {f"{r}{c}" for r in "ABCDEFGH" for c in range(1, 13)}}

    async def rack_picked(self, site) -> None:
        from .station_core import parse_slot
        self.racks.pop(parse_slot(site), None)

    async def _shutdown(self) -> None:
        self._ready = False
