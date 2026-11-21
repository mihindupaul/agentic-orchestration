"""Hardware-level simulator for an imaginary sample-handling workcell, designed for learning.

Each device (arm, balance, scanner, heater) is exposed on its own TCP port,
so an external orchestrator talks to it as it would to separate instruments.
"""

from .config import HeaterSpec, Layout, Timing
from .simulator import Simulator

__all__ = ["HeaterSpec", "Layout", "Timing", "Simulator"]
