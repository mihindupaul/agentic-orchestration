"""Where the agent learns which wells hold vials.

The planner never assumes what is on the deck; it asks. Two sources answer the same question:

  ControllerInventory  the live answer, from the workcell controller service (`make ctl`), which in turn asks the
                       simulator. This is what the agent uses for real.
  DemoInventory        a fixed answer (the simulator's demo deck: bay 3 full, bay 8 empty), for trying the agent
                       without the controller running, and for tests.
"""

from __future__ import annotations

import asyncio
from typing import Protocol

from .spec import COLUMNS, ROW_LETTERS

ALL_WELLS = [f"{row}{col}" for row in ROW_LETTERS for col in COLUMNS]


class Inventory(Protocol):
    def rack(self, bay: int) -> dict:
        """{"bay": n, "present": bool, "occupied": ["A1", ...]}"""
        ...


class DemoInventory:
    """The simulator's demo deck, fixed: full racks in bays 2 and 3, an empty rack in bay 8, nothing elsewhere."""

    def __init__(self, racks: dict | None = None):
        self.racks = racks if racks is not None else {2: list(ALL_WELLS), 3: list(ALL_WELLS), 8: []}

    def rack(self, bay: int) -> dict:
        if bay not in self.racks:
            return {"bay": bay, "present": False, "occupied": []}
        return {"bay": bay, "present": True, "occupied": sorted(self.racks[bay])}


class ControllerInventory:
    """Asks the workcell controller service, which asks the simulator (rack maps are a simulator feature)."""

    def __init__(self, host: str = "127.0.0.1", port: int = 8700):
        self.host, self.port = host, port

    def rack(self, bay: int) -> dict:
        return asyncio.run(self._rack(bay))

    async def _rack(self, bay: int) -> dict:
        from controller.api import ControllerClient
        client = ControllerClient(self.host, self.port)
        await client.open()
        try:
            reply = await client.call("rack_map", bay=bay)
        finally:
            await client.close()
        return {"bay": reply["bay"], "present": reply["present"], "occupied": reply["occupied"]}
