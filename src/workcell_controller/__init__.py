"""workcell_controller: the controller layer for the imaginary vial scan / weigh / heat workcell.

The devices (arm, balance, scanner well, heater well) are kept simple: coordinates in, readings out.
This package holds everything that knows the cell:
  deck.py        the deck file: bays, wells and stations as arm coordinates
  operations.py  labware operations (pick "3 A1", place "scanner", scan, weigh, heat)
  controller.py  the service: one owner of the hardware, one operation at a time, fault latch
  api.py         the service on the network (JSON lines on :8700)
Rack jobs are not here: a workflow (Orca, from stage 2 on) composes them from these operations.
The devices can be the simulator in `workcell_sim` or real hardware with the same protocols.
"""

from .controller import OPERATIONS, Controller
from .deck import Deck
from .errors import (ArmFault, ControllerBusy, ControllerFault, DeviceError, DeviceTimeout, GripperEmpty,
                     InvalidRequest, OperationError, ControllerError, VialLost)
from .operations import Workcell

__all__ = ["Controller", "OPERATIONS", "Deck", "Workcell", "ControllerError", "DeviceError", "DeviceTimeout",
           "ArmFault", "ControllerBusy", "ControllerFault", "InvalidRequest", "OperationError", "GripperEmpty",
           "VialLost"]
