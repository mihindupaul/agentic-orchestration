"""The workcell as an Orca device.

    workcell = VialWorkcell("vial_workcell")                 # sites bay1 .. bay8
    @orca.action(device=workcell, inputs=[rack], deck_positions={rack: "bay3"})
    async def scan_one(ctx: ActionContext):
        await ctx.device().pick(bay=3, well="A1")
        await ctx.device().place("scanner")
        code = (await ctx.device().scan())["code"]

Each method is one controller operation; composing them into rack jobs is the workflow's job. When Orca
places a rack on one of the device's sites, the driver is told (the simulator then loads that bay).
"""

from __future__ import annotations

from orca.resource_models.devices import Device
from orca.runtime.device_factory_context import is_factory_bound, resolve_drivers
from orca.runtime.run_modes import WorkflowRunMode

from .driver import IVialWorkcellDriver, SimVialWorkcellDriver, VialWorkcellDriver
from .station_core import StationConfig

BAY_SITES = [f"bay{n}" for n in range(1, 9)]


class VialWorkcell(Device[IVialWorkcellDriver]):
    KIND = "vial_workcell"

    def __init__(self, name: str, config: StationConfig | None = None, *, site_names: list[str] | None = None,
                 sim_override: WorkflowRunMode | None = None) -> None:
        if is_factory_bound():          # a hosted deployment supplies the drivers
            live, sim = resolve_drivers(self.KIND, name, SimVialWorkcellDriver)
        else:
            live, sim = VialWorkcellDriver(config), SimVialWorkcellDriver(name)
        super().__init__(name, live, sim, sim_override=sim_override, site_names=site_names or BAY_SITES)

    # ---- operations (one controller operation each) ------------------------------------------
    async def pick(self, where: str | None = None, *, bay: int | None = None, well: str | None = None) -> dict:
        return await self.driver.pick(where, bay=bay, well=well)

    async def place(self, where: str | None = None, *, bay: int | None = None, well: str | None = None) -> dict:
        return await self.driver.place(where, bay=bay, well=well)

    async def scan(self) -> dict:
        return await self.driver.scan()

    async def weigh(self) -> dict:
        return await self.driver.weigh()

    async def tare(self) -> dict:
        return await self.driver.tare()

    async def heat(self, celsius: float) -> dict:
        return await self.driver.heat(celsius)

    async def temperature(self) -> dict:
        return await self.driver.temperature()

    async def heater_off(self) -> dict:
        return await self.driver.heater_off()

    async def home(self) -> dict:
        return await self.driver.home()

    async def rack_map(self, bay: int) -> dict:
        return await self.driver.rack_map(bay)

    # ---- labware handoff: a rack arriving at or leaving a bay --------------------------------
    async def _do_prepare_for_place(self, labware, mover, target=None, site=None) -> None:
        return None

    async def _do_prepare_for_pick(self, labware, mover, target=None, site=None) -> None:
        return None

    async def _do_notify_placed(self, labware, mover, target=None, site=None) -> None:
        await self.driver.rack_placed(target or site, labware.name, getattr(labware, "barcode", None))

    async def _do_notify_picked(self, labware, mover, target=None, site=None) -> None:
        await self.driver.rack_picked(target or site)

