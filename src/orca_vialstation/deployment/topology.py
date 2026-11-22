"""Orca topology: an arm that moves vials, three instruments, and rows C-F of the racks in bays 3 and 8."""

from orca.runtime.store_factory import IRuntimeStoreFactory
from orca.sdk.build import Topology

from orca_vialstation.cell import Balance, CellArm, CodeReader, Heater, layout
from orca_vialstation.station_core import StationConfig
from vialctl.deck import Deck

SYSTEM_NAME = "Vial workcell"
SYSTEM_DESCRIPTION = "Arm as transporter; code reader, balance and heater as devices; vials as labware"


def build_topology(stores: IRuntimeStoreFactory) -> Topology:
    del stores
    config = StationConfig.from_env()
    pads, teachpoints = layout(Deck.load(), bays=(3, 8), rows="CDEF")     # the wells the workflows use
    return Topology(
        locations={
            **pads,
            "code_reader": CodeReader("code_reader", config),
            "balance": Balance("balance", config),
            "heater": Heater("heater", config),
        },
        transporters=[CellArm("SCARA", config, teachpoints)],
    )
