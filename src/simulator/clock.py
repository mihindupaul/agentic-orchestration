from __future__ import annotations

import asyncio
import time


class Clock:
    """Simulated time. `scale` > 1 makes the simulation run faster than wall clock."""

    def __init__(self, scale: float = 1.0):
        self.scale = scale
        self._t0 = time.monotonic()

    def now(self) -> float:
        return (time.monotonic() - self._t0) * self.scale

    async def sleep(self, sim_seconds: float) -> None:
        await asyncio.sleep(max(0.0, sim_seconds) / self.scale)
