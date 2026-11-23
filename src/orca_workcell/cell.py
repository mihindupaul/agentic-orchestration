"""The workcell as Orca sees real hardware: an arm that moves vials, and three separate instruments.

    CellArm      Orca transporter: moves one vial between positions. Reserved only while it moves.
    CodeReader   Orca device: reads the code of the vial standing in its well.
    Balance      Orca device: weighs the vial on its pan.
    Heater       Orca device: heats the vial standing in its well.
    rack wells   Orca positions (plate pads), one per well of the bays in use: "bay3_C1" ...

Vials are the labware. Each vial has its own Orca thread, and Orca's scheduler moves vials with the arm
between rack wells and instruments. Because every instrument is its own device, a vial heating in the
heater keeps only the heater busy: the arm and the other instruments carry on with other vials.

Every driver call goes through the Orca-free `CellCore` to the workcell controller. Each device has
its own controller connection, and the controller locks per piece of hardware, so the heater can be read
while the arm moves.
"""

from __future__ import annotations

import abc
import inspect

from cheshire_drivers.command_timings import command_timing
from cheshire_drivers.driver_errors import (DriverError, DriverOutcomeUnknownError, DriverRefusedError,
                                            DriverRejectedError)
from cheshire_drivers.interfaces import BaseDriver, ITransporterDriver
from cheshire_drivers.sims import SimTransporterDriver
from cheshire_drivers.teachpoints import AccessConfig, CartesianCoordinates, Teachpoint
from orca.resource_models.devices import Device
from orca.resource_models.plate_pad import PlatePad
from orca.resource_models.transporter import Transporter
from orca.runtime.device_factory_context import is_factory_bound, use_device_factory
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.teachpoint_service import seeded_teachpoint_service

from workcell_controller.deck import Deck

from .core import CoreFailed, CoreRefused, CoreRejected, CoreUnknown, ControllerConfig, CellCore

STATIONS = {"code_reader": "scanner", "balance": "balance", "heater": "heater"}   # Orca name -> controller


def well_position(bay: int, well: str) -> str:
    return f"bay{bay}_{well.upper()}"


def address(position_id: str) -> str:
    """Orca position -> controller address: 'bay3_C1' -> '3 C1', 'code_reader' -> 'scanner'."""
    name = position_id.split("/", 1)[0]
    if name in STATIONS:
        return STATIONS[name]
    if name.startswith("bay") and "_" in name:
        bay, well = name[3:].split("_", 1)
        return f"{int(bay)} {well}"
    raise DriverRejectedError(f"the arm has no taught position {position_id!r}")


# ------------------------------------------------------------------------------------------- drivers
async def _guard(coro):
    """Run a core call and turn its outcome into the driver error Orca acts on:

        CoreRefused  -> DriverRefusedError         nothing happened, busy or faulted: ask again later
        CoreRejected -> DriverRejectedError        can never work as asked: do not ask again
        CoreFailed   -> DriverError (FAILED)       something went wrong in the cell: someone has to look
        CoreUnknown  -> DriverOutcomeUnknownError  the answer was lost: check before retrying
    """
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


class CellArmDriver(ITransporterDriver):
    """Live arm: an Orca pick or place at a taught position becomes a controller pick or place."""

    def __init__(self, config: ControllerConfig | None = None, name: str = "arm"):
        self.core, self._name, self._ready = CellCore(config), name, False

    @property
    def name(self) -> str:
        return self._name

    @property
    def is_initialized(self) -> bool:
        return self._ready

    async def initialize(self, request=None) -> None:
        await _guard(self.core.connect())
        self._ready = True

    async def connect(self) -> None:
        await _guard(self.core.connect())

    async def disconnect(self) -> None:
        self._ready = False
        await self.core.close()

    async def _shutdown(self) -> None:
        await self.disconnect()

    @command_timing(typical=5.0, max=60.0)
    async def pick_at_coords(self, request) -> None:
        r = await _guard(self.core.execute("pick", {"where": address(request.teachpoint.position_id)}))
        if not r.get("picked", True):
            raise DriverError(f"no vial at {request.teachpoint.position_id}")

    @command_timing(typical=5.0, max=60.0)
    async def place_at_coords(self, request) -> None:
        await _guard(self.core.execute("place", {"where": address(request.teachpoint.position_id)}))

    # Orca keeps a simulated arm's picture of the deck in step with these. On the cell the deck is what it
    # is (the controller and the simulator know where each vial stands), so there is nothing to do.
    async def ensure_seeded(self, request=None) -> None:
        return None

    async def unseed_position(self, request=None) -> None:
        return None

    async def reset_world(self, request=None) -> None:
        return None

    async def home(self, request=None) -> None:
        await _guard(self.core.execute("home"))

    async def move_to_safe(self, request=None) -> None:
        await self.home()


class StationDriver(BaseDriver):
    """Live driver for one instrument; it only offers that instrument's operations."""

    OPERATIONS: tuple = ()

    def __init__(self, config: ControllerConfig | None = None):
        self.core, self._ready = CellCore(config), False

    @property
    def is_initialized(self) -> bool:
        return self._ready

    async def initialize(self) -> None:
        await _guard(self.core.connect())
        self._ready = True

    async def open(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def disconnect(self) -> None:
        self._ready = False
        await self.core.close()

    async def _shutdown(self) -> None:
        await self.disconnect()

    async def run(self, op: str, params: dict | None = None) -> dict:
        if op not in self.OPERATIONS:
            raise DriverRejectedError(f"{type(self).__name__} does not offer {op!r}")
        return await _guard(self.core.execute(op, params or {}))


class CodeReaderDriver(StationDriver):
    OPERATIONS = ("scan",)


class BalanceDriver(StationDriver):
    OPERATIONS = ("weigh", "tare")


class HeaterDriver(StationDriver):
    OPERATIONS = ("heat", "temperature", "heater_off")


class SimStationDriver(BaseDriver):
    """Pure-simulation stand-in: instant, plausible answers, no controller."""

    def __init__(self, name: str = "station"):
        self.name, self._ready, self.target = name, False, None

    @property
    def is_initialized(self) -> bool:
        return self._ready

    async def initialize(self) -> None:
        self._ready = True

    async def open(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def run(self, op: str, params: dict | None = None) -> dict:
        params = params or {}
        if op == "scan":
            return {"code": "SIM-CODE", "read": True}
        if op == "weigh":
            return {"grams": 1.8}
        if op == "heat":
            self.target = float(params["celsius"])
            return {"target": self.target, "celsius": 22.0}
        if op == "temperature":
            if self.target is None:
                return {"celsius": 22.0, "target": None, "state": "OFF", "stable": False}
            return {"celsius": self.target, "target": self.target, "state": "STABLE", "stable": True}
        if op == "heater_off":
            self.target = None
        return {}


def _refuse_unsupported(cls) -> None:
    """ITransporterDriver has many arm controls this cell does not offer (single-axis moves, free mode,
    raw coordinates). Give each a body that refuses, so the class can be instantiated."""
    for name in sorted(cls.__abstractmethods__):
        base = getattr(ITransporterDriver, name, None)

        def make(n, is_async):
            if is_async:
                async def refuse(self, *a, **k):
                    raise DriverRejectedError(f"{n} is not supported by this arm")
            else:
                def refuse(self, *a, **k):
                    raise DriverRejectedError(f"{n} is not supported by this arm")
            refuse.__name__ = n
            return refuse
        setattr(cls, name, make(name, inspect.iscoroutinefunction(base)))
    abc.update_abstractmethods(cls)


_refuse_unsupported(CellArmDriver)


# ------------------------------------------------------------------------------------------- devices
class _Drivers:
    """Driver provider bound while one of our resources is built, when no hosted provider is bound."""

    def __init__(self, live, sim):
        self.live, self.sim = live, sim

    def build_drivers(self, device_type: str, name: str, *, deck_modeling: bool = False):
        return self.live, self.sim


class CellArm(Transporter):

    def __init__(self, name: str, config: ControllerConfig | None = None, teachpoints: list | None = None,
                 sim_override: WorkflowRunMode | None = None) -> None:
        store = seeded_teachpoint_service(teachpoints or [])
        if is_factory_bound():
            super().__init__(name, teachpoint_store=store, sim_override=sim_override)
        else:
            with use_device_factory(_Drivers(CellArmDriver(config, name), SimTransporterDriver(name))):
                super().__init__(name, teachpoint_store=store, sim_override=sim_override)


class _Station(Device[BaseDriver]):
    LIVE_DRIVER: type = StationDriver

    def __init__(self, name: str, config: ControllerConfig | None = None,
                 sim_override: WorkflowRunMode | None = None) -> None:
        super().__init__(name, self.LIVE_DRIVER(config), SimStationDriver(name), sim_override=sim_override)


class CodeReader(_Station):
    KIND = "code_reader"
    LIVE_DRIVER = CodeReaderDriver

    async def scan(self) -> dict:
        return await self.driver.run("scan")


class Balance(_Station):
    KIND = "balance"
    LIVE_DRIVER = BalanceDriver

    async def weigh(self) -> dict:
        return await self.driver.run("weigh")

    async def tare(self) -> dict:
        return await self.driver.run("tare")


class Heater(_Station):
    KIND = "heater"
    LIVE_DRIVER = HeaterDriver

    async def heat(self, celsius: float) -> dict:
        return await self.driver.run("heat", {"celsius": celsius})

    async def temperature(self) -> dict:
        return await self.driver.run("temperature")

    async def heater_off(self) -> dict:
        return await self.driver.run("heater_off")


# ------------------------------------------------------------------------------------------- layout
def layout(deck: Deck, bays=(3, 8), rows: str | None = None, wells: set | None = None) -> tuple:
    """Rack-well positions for `bays` (only `rows`, or only the (bay, well) pairs in `wells`, if given) and the
    arm's teachpoints, from the controller's deck file: returns ({position name: PlatePad}, [Teachpoint]).

    Orca builds a route between every pair of positions an arm can reach, so build time grows with the
    square of the number of positions: teach only the wells the workflows use."""
    pads, points = {}, []
    # From above: the controller lifts to its safe height and lowers to the deck's grip height itself.
    straight_down = AccessConfig(name="vertical", access_type="vertical",
                                 vertical_clearance=deck.safe_z - deck.grip_z, gripper_offset=0.0)

    def teach(position: str, xy: tuple) -> None:
        points.append(Teachpoint(position, CartesianCoordinates(x=xy[0], y=xy[1], z=deck.grip_z,
                                                                yaw=0.0, pitch=90.0, roll=0.0),
                                 orientation="landscape", access=straight_down))

    for bay in bays:
        for well in deck.wells(bay):
            if rows and well[0] not in rows:
                continue
            if wells is not None and (bay, well) not in wells:
                continue
            name = well_position(bay, well)
            pads[name] = PlatePad(name)
            teach(name, deck.well_xy(bay, well))
    for orca_name, controller_name in STATIONS.items():
        teach(orca_name, deck.stations[controller_name])
    return pads, points
