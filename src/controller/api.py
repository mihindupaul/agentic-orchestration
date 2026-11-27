"""Newline-delimited JSON API in front of the controller (asyncio, stdlib only).

Request:  {"id": 1, "cmd": "pick", "where": "3 A1"}      Reply: {"id": 1, "ok": true, "at": "bay 3 A1"}
Error:    {"id": 1, "ok": false, "type": "ControllerBusy", "error": "..."}

Operations (one at a time): home, pick {where}, place {where}, scan, weigh, tare, heat {celsius},
temperature, heater_off, rack_map {bay}. `heat` starts heating and returns at once; poll `temperature`.
`where` is "3 A1", "scanner", or {"bay": 3, "well": "A1"} / {"station": "scanner"}.
Service commands: ping, status, events {n}, clear_fault, emergency_stop.
"""

from __future__ import annotations

import asyncio
import json

from .controller import OPERATIONS, Controller
from .errors import ControllerError


async def handle(ctl: Controller, req: dict) -> dict:
    cmd = req.get("cmd")
    if cmd == "ping":
        return {}
    if cmd == "status":
        return {"status": ctl.status()}
    if cmd in OPERATIONS:
        return await ctl.run(cmd, **{k: v for k, v in req.items() if k not in ("id", "cmd")})
    if cmd == "clear_fault":
        await ctl.clear_fault()
        return {}
    if cmd == "emergency_stop":
        await ctl.emergency_stop()
        return {}
    if cmd == "events":
        return {"events": ctl.events(int(req.get("n", 50)))}
    raise ControllerError(f"unknown command {cmd!r}")


async def serve(ctl: Controller, host: str = "127.0.0.1", port: int = 8700) -> asyncio.AbstractServer:
    async def client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while line := await reader.readline():
                rid = None
                try:
                    req = json.loads(line)
                    rid = req.get("id")
                    out = {"ok": True, **await handle(ctl, req)}
                except asyncio.TimeoutError:
                    out = {"ok": False, "error": "timeout", "type": "Timeout"}
                except (ControllerError, ValueError) as exc:
                    out = {"ok": False, "error": str(exc) or repr(exc), "type": type(exc).__name__}
                out["id"] = rid
                writer.write((json.dumps(out) + "\n").encode())
                await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()

    return await asyncio.start_server(client, host, port)


class ControllerClient:
    """Small client for the API, used by the Orca driver and `python -m controller`."""

    def __init__(self, host: str = "127.0.0.1", port: int = 8700):
        self.host, self.port = host, port
        self._r = self._w = None
        self._n = 0
        self._lock = asyncio.Lock()

    async def open(self) -> None:
        self._r, self._w = await asyncio.open_connection(self.host, self.port)

    async def close(self) -> None:
        if self._w is not None:
            self._w.close()
            self._w = None

    @property
    def is_open(self) -> bool:
        return self._w is not None and not self._w.is_closing()

    async def call(self, cmd: str, **kw) -> dict:
        async with self._lock:
            self._n += 1
            self._w.write((json.dumps({"id": self._n, "cmd": cmd, **kw}) + "\n").encode())
            await self._w.drain()
            line = await self._r.readline()
        if not line:
            raise ConnectionError("controller closed the connection")
        out = json.loads(line)
        if not out.get("ok"):
            from .errors import ERRORS_BY_NAME
            raise ERRORS_BY_NAME.get(out.get("type"), ControllerError)(out.get("error"))
        return out
