"""Driver provider for hosted Orca deployments.

Orca device constructors ask a bound provider for their (live, sim) drivers. Bind this one around the
topology build and every `VialWorkcell` gets a live driver for its controller; any other device kind is
handed to the fallback provider (for example Orca's own).

    with use_device_factory(VialWorkcellProvider({"vial_workcell": StationConfig.from_env()}, fallback=...)):
        build = await build_system_from_package("orca_vialstation.deployment", stores)

Without a bound provider, `VialWorkcell` builds its own drivers (see device.py), so standalone runs need
none of this.
"""

from __future__ import annotations

from .device import VialWorkcell
from .driver import SimVialWorkcellDriver, VialWorkcellDriver
from .station_core import StationConfig


class VialWorkcellProvider:
    """Implements Orca's IDeviceDriverProvider: build_drivers(device_type, name) -> (live, sim)."""

    def __init__(self, configs: dict | None = None, default: StationConfig | None = None, fallback=None):
        self.configs = configs or {}           # device name -> StationConfig
        self.default = default or StationConfig.from_env()
        self.fallback = fallback

    def build_drivers(self, device_type: str, name: str, *, deck_modeling: bool = False):
        if device_type == VialWorkcell.KIND:
            return VialWorkcellDriver(self.configs.get(name, self.default)), SimVialWorkcellDriver(name)
        if self.fallback is None:
            raise ValueError(f"no driver for device type {device_type!r} (name {name!r}) and no fallback provider")
        if deck_modeling:
            return self.fallback.build_drivers(device_type, name, deck_modeling=True)
        return self.fallback.build_drivers(device_type, name)
