"""Everything the Orca driver does, minus the Orca types.

The station offers Orca one action per controller operation (pick, place, scan, weigh, tare, heat,
temperature, heater_off, home, rack_map). Orca workflows compose them into rack jobs: which tubes, in what order, what to do when a code
will not read. Nothing here sequences tubes.

Outcomes are split the way a workflow needs them:
  - results it can branch on come back as data: a no-read (`code` is None), a heater that has not
    settled yet (`stable` False), a pick that found no vial (`picked` False). `heat` only starts heating:
    waiting for the temperature is the workflow's decision;
  - CoreRefused: nothing happened, the station is busy or faulted;
  - CoreRejected: the request can never work as written (no such bay, nothing to place);
  - CoreFailed: something went wrong in the cell (vial lost, arm fault) and an operator is needed;
  - CoreUnknown: the request went out and the answer was lost.
In simulation, rack handoff hooks also tell the simulator that a rack arrived or left.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass, field

from vialctl.api import ControllerClient
from vialctl.errors import (ArmFault, ControllerBusy, ControllerFault, DeviceError, DeviceTimeout, GripperEmpty,
                            InvalidRequest, OperationError, VialCtlError, VialLost)

ACTIONS = ("pick", "place", "scan", "weigh", "tare", "heat", "temperature", "heater_off", "home", "rack_map")


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
    sim_admin_host: str | None = None        # set to push rack presence into the simulator
    sim_admin_port: int = 9000
    default_fill: float = 1.0                # sim only: how full a newly "placed" rack is
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "StationConfig":
        """Addresses from the environment, so a deployment package needs no code change per site:
        VIALCTL_HOST / VIALCTL_PORT for the controller, SIM_ADMIN_HOST / SIM_ADMIN_PORT for the simulator."""
        sim_host = os.environ.get("SIM_ADMIN_HOST")
        return cls(ctl_host=os.environ.get("VIALCTL_HOST", "127.0.0.1"),
                   ctl_port=int(os.environ.get("VIALCTL_PORT", "8700")),
                   sim_admin_host=sim_host or None,
                   sim_admin_port=int(os.environ.get("SIM_ADMIN_PORT", "9000")))


_SLOT_RE = re.compile(r"(\d+)\s*$")


def parse_slot(site: str | None) -> int:
    """'slot3', 'vial_station/slot3' or '3' -> 3."""
    m = _SLOT_RE.search(site or "")
    if not m:
        raise CoreRejected(f"cannot read a rack slot number from site {site!r}")
    return int(m.group(1))


class VialStationCore:
    def __init__(self, config: StationConfig | None = None):
        self.cfg = config or StationConfig()
        self.ctl = ControllerClient(self.cfg.ctl_host, self.cfg.ctl_port)
        self._admin = None

    @property
    def connected(self) -> bool:
        return self.ctl.is_open

    async def connect(self) -> None:
        if not self.ctl.is_open:
            try:
                await self.ctl.open()
            except OSError as exc:
                raise CoreRefused(f"controller not reachable at {self.cfg.ctl_host}:{self.cfg.ctl_port}: {exc}")
        if self.cfg.sim_admin_host and self._admin is None:
            from vialctl.devices import SimAdmin
            self._admin = SimAdmin(self.cfg.sim_admin_host, self.cfg.sim_admin_port)
            await self._admin.connect()

    async def close(self) -> None:
        await self.ctl.close()
        if self._admin is not None:
            await self._admin.close()
            self._admin = None

    # ------------------------------------------------------------------ actions
    async def execute(self, action: str, params: dict | None = None) -> dict:
        """Run one station action and return its result. A rack well can be given as `where` ("3 A1"),
        as `bay` + `well`, or as an Orca `site` ("vial_station/slot3") + `well`."""
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
            bay = p.get("bay")
            if bay is None and "site" in p:
                bay = parse_slot(p["site"])
            if bay is None or "well" not in p:
                raise CoreRejected(f"{action} needs `where`, `station`, or a `bay`/`site` and a `well`")
            return {"where": {"bay": int(bay), "well": p["well"]}}
        if action == "heat":
            if "celsius" not in p:
                raise CoreRejected("heat needs `celsius`")
            return {"celsius": p["celsius"]}
        if action == "rack_map":
            bay = p.get("bay", parse_slot(p["site"]) if "site" in p else None)
            if bay is None:
                raise CoreRejected("rack_map needs a `bay` or `site`")
            return {"bay": int(bay)}
        return {}

    async def run_protocol(self, protocol_filepath: str, params: dict | None) -> dict:
        """Orca's entry point. A "protocol" is one station action: either its name ("pick") or a small JSON
        file such as {"action": "place", "station": "scanner"}. `params` add to or override the file.
        Orca's run_protocol returns nothing, so the result is also written to `params["results_path"]`."""
        params = dict(params or {})
        results_path = params.pop("results_path", None)
        spec: dict = {}
        if protocol_filepath and os.path.exists(protocol_filepath):
            try:
                with open(protocol_filepath) as fh:
                    spec = json.load(fh) or {}
            except (OSError, ValueError) as exc:
                raise CoreRejected(f"cannot read protocol file {protocol_filepath!r}: {exc}")
        elif protocol_filepath:
            spec = {"action": os.path.splitext(os.path.basename(protocol_filepath))[0]}
        spec.update(params)
        action = spec.pop("action", None)
        if not action:
            raise CoreRejected("no action: name one in the protocol file or in params")
        result = await self.execute(action, spec)
        if results_path:
            os.makedirs(os.path.dirname(os.path.abspath(results_path)), exist_ok=True)
            with open(results_path, "w") as fh:
                json.dump({"action": action, **result}, fh, indent=2)
        return result

    # ------------------------------------------------------------------ labware handoff
    async def rack_placed(self, site: str | None, barcode: str | None, name: str) -> None:
        """A rack arrived at `site`. Real hardware needs nothing; the simulator needs to be told."""
        await self.connect()
        if self._admin is None:
            return
        slot = parse_slot(site)
        await self._admin.call(cmd="load_rack", slot=slot, barcode=barcode or name, fill=self.cfg.default_fill)

    async def rack_picked(self, site: str | None) -> None:
        await self.connect()
        if self._admin is None:
            return
        await self._admin.call(cmd="remove_rack", slot=parse_slot(site))
