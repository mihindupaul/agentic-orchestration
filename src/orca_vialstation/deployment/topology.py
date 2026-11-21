"""Orca topology: the workcell as one device whose sites are its eight rack bays."""

from orca.runtime.store_factory import IRuntimeStoreFactory
from orca.sdk.build import Topology

from orca_vialstation.device import VialWorkcell
from orca_vialstation.station_core import StationConfig

SYSTEM_NAME = "Vial workcell"
SYSTEM_DESCRIPTION = "Imaginary sample-handling workcell (arm, code scanner, balance, heater, eight rack bays)"


def build_topology(stores: IRuntimeStoreFactory) -> Topology:
    del stores
    return Topology(locations={"vial_workcell": VialWorkcell("vial_workcell", StationConfig.from_env())},
                    transporters=[])      # racks are placed by hand; the arm inside the cell is the device      # racks are placed by hand; the arm inside the cell is the device
