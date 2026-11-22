"""Everything the Orca driver does, minus the Orca types.

The cell's Orca drivers (cell.py) offer one action per controller operation (pick, place, home, scan, weigh,
tare, heat, temperature, heater_off). Orca workflows compose them: which vials, in what order, what to do when
a code will not read. Nothing here sequences vials.

Outcomes are split the way a workflow needs them:
  - results it can branch on come back as data: a no-read (`code` is None), a heater that has not
    settled yet (`stable` False), a pick that found no vial (`picked` False). `heat` only starts heating:
    waiting for the temperature is the workflow's decision;
  - CoreRefused: nothing happened, the station is busy or faulted;
  - CoreRejected: the request can never work as written (no such bay, nothing to place);
  - CoreFailed: something went wrong in the cell (vial lost, arm fault) and an operator is needed;
  - CoreUnknown: the request went out and the answer was lost.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass

from vialctl.api import ControllerClient
from vialctl.errors import (ArmFault, ControllerBusy, ControllerFault, DeviceError, DeviceTimeout, GripperEmpty,
                            InvalidRequest, OperationError, VialCtlError, VialLost)

ACTIONS = ("pick", "place", "home", "scan", "weigh", "tare", "heat", "temperature", "heater_off")


class CoreRefused(RuntimeError):
    """Nothing happened; the station is busy or faulted. Asking again later may work."""


class CoreRejected(RuntimeError):
    """The request can never work as written."""


class CoreFailed(RuntimeError):
    """The action started and the cell needs an operator (vial lost, arm fault, device error)."""

    def __init__(self, message: str, results: dict | None = None):
        super().__init__(message)
        self.results = results or {}


class CoreUnknown(RuntimeError):
    """A request went out and the answer was lost. Check the station before retrying."""


@dataclass
class StationConfig:
    ctl_host: str = "127.0.0.1"
    ctl_port: int = 8700

    @classmethod
    def from_env(cls) -> "StationConfig":
        """The controller's address from the environment (VIALCTL_HOST / VIALCTL_PORT), so the deployment
        package needs no code change per site."""
        return cls(ctl_host=os.environ.get("VIALCTL_HOST", "127.0.0.1"),
                   ctl_port=int(os.environ.get("VIALCTL_PORT", "8700")))


class VialStationCore:
    def __init__(self, config: StationConfig | None = None):
        self.cfg = config or StationConfig()
        self.ctl = ControllerClient(self.cfg.ctl_host, self.cfg.ctl_port)

    @property
    def connected(self) -> bool:
        return self.ctl.is_open

    async def connect(self) -> None:
        if not self.ctl.is_open:
            try:
                await self.ctl.open()
            except OSError as exc:
                raise CoreRefused(f"controller not reachable at {self.cfg.ctl_host}:{self.cfg.ctl_port}: {exc}")

    async def close(self) -> None:
        await self.ctl.close()

    # ------------------------------------------------------------------ actions
    async def execute(self, action: str, params: dict | None = None) -> dict:
        """Run one station action and return its result. A place is given as `where`: "3 A1" for a rack
        well, "scanner" for a station."""
        if action not in ACTIONS:
            raise CoreRejected(f"unknown action {action!r} (have {', '.join(ACTIONS)})")
        args = self._arguments(action, dict(params or {}))
        await self.connect()
        try:
            out = await self.ctl.call(action, **args)
        except GripperEmpty as exc:
            return {"picked": False, "reason": str(exc)}
        except (ControllerBusy, ControllerFault) as exc:
            raise CoreRefused(str(exc))
        except (VialLost, ArmFault, DeviceError) as exc:
            raise CoreFailed(f"{action}: {type(exc).__name__}: {exc}")
        except (InvalidRequest, OperationError) as exc:
            raise CoreRejected(f"{action}: {exc}")
        except (DeviceTimeout, ConnectionError, OSError, asyncio.IncompleteReadError) as exc:
            raise CoreUnknown(f"{action} was sent but its outcome is unknown: {exc}")
        except VialCtlError as exc:
            raise CoreFailed(f"{action}: {exc}")
        out.pop("id", None)
        out.pop("ok", None)
        if action == "pick":
            out["picked"] = True
        if action == "scan":
            out["read"] = out["code"] is not None
        return out

    @staticmethod
    def _arguments(action: str, p: dict) -> dict:
        if action in ("pick", "place"):
            if "where" in p:
                return {"where": p["where"]}
            if "station" in p:
                return {"where": {"station": p["station"]}}
            if "bay" in p and "well" in p:
                return {"where": {"bay": int(p["bay"]), "well": p["well"]}}
            raise CoreRejected(f"{action} needs `where`, `station`, or a `bay` and a `well`")
        if action == "heat":
            if "celsius" not in p:
                raise CoreRejected("heat needs `celsius`")
            return {"celsius": p["celsius"]}
        return {}
