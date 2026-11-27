"""Async clients for the four device protocols. Only wire protocols are used here, never simulator
internals, so the same clients can talk to real instruments that speak the same dialect.

Every client can take a `trace(device, line_sent, reply)` callback; the CLI uses it to print exactly
what crosses the wire."""

from __future__ import annotations

import asyncio
import json
import re

from .errors import ArmFault, DeviceError, DeviceTimeout

MODE_DISABLED, MODE_IDLE, MODE_RUNNING, MODE_ERROR, MODE_COLLISION = 4, 5, 7, 9, 11
_ARM_REPLY = re.compile(r"^(-?\d+),\{(.*?)\},")


def _num(v: float) -> str:
    """Short number for a command line: 199.5 rather than 199.500, 100 rather than 100.000."""
    text = f"{v:.3f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


class LineConn:
    """One TCP connection, one request in flight at a time, line-terminated replies."""

    def __init__(self, host: str, port: int, timeout_s: float = 30.0, name: str = "device", trace=None):
        self.host, self.port, self.timeout_s, self.name, self.trace = host, port, timeout_s, name, trace
        self._r = self._w = None
        self._lock = asyncio.Lock()

    async def open(self) -> None:
        try:
            self._r, self._w = await asyncio.wait_for(asyncio.open_connection(self.host, self.port), 10.0)
        except (OSError, asyncio.TimeoutError) as exc:
            raise DeviceError(f"{self.name}: cannot connect to {self.host}:{self.port} ({exc})", "connect")

    @property
    def is_open(self) -> bool:
        return self._w is not None and not self._w.is_closing()

    async def ask(self, text: str, timeout_s: float | None = None) -> str:
        if not self.is_open:
            raise DeviceError(f"{self.name}: not connected", "not_connected")
        async with self._lock:
            try:
                self._w.write((text + "\n").encode())
                await self._w.drain()
                line = await asyncio.wait_for(self._r.readline(), timeout_s or self.timeout_s)
            except asyncio.TimeoutError:
                await self.close()      # a late reply would desynchronise the stream
                raise DeviceTimeout(f"{self.name}: no reply to {text!r}")
            except (ConnectionError, OSError) as exc:
                await self.close()
                raise DeviceTimeout(f"{self.name}: connection lost during {text!r} ({exc})")
        if not line:
            await self.close()
            raise DeviceTimeout(f"{self.name}: connection closed during {text!r}")
        reply = line.decode().strip()
        if self.trace:
            self.trace(self.name, text, reply)
        return reply

    async def close(self) -> None:
        if self._w is not None:
            self._w.close()
            try:
                await self._w.wait_closed()
            except (ConnectionError, OSError):
                pass
            self._w = None


class ArmClient:
    """DOBOT-style command set. Motion is queued by the arm; `sync()` waits for it to finish."""

    def __init__(self, host: str, dashboard_port: int, motion_port: int, timeout_s: float = 60.0, trace=None):
        self.dash = LineConn(host, dashboard_port, timeout_s, "dashboard", trace)
        self.motion = LineConn(host, motion_port, timeout_s, "motion", trace)

    async def connect(self) -> None:
        await self.dash.open()
        await self.motion.open()

    async def close(self) -> None:
        await self.dash.close()
        await self.motion.close()

    async def _cmd(self, conn: LineConn, text: str) -> tuple:
        raw = await conn.ask(text)
        m = _ARM_REPLY.match(raw)
        if not m:
            raise DeviceError(f"arm: unparseable reply {raw!r} to {text!r}", "bad_reply")
        return int(m.group(1)), m.group(2)

    async def _ok(self, conn: LineConn, text: str) -> str:
        code, values = await self._cmd(conn, text)
        if code != 0:
            raise DeviceError(f"arm: {text} returned {code}", str(code))
        return values

    async def enable(self) -> None:
        await self._ok(self.dash, "EnableRobot()")

    async def disable(self) -> None:
        await self._ok(self.dash, "DisableRobot()")

    async def clear_error(self) -> None:
        await self._ok(self.dash, "ClearError()")

    async def emergency_stop(self) -> None:
        await self._ok(self.dash, "EmergencyStop()")

    async def speed(self, percent: int) -> None:
        await self._ok(self.dash, f"SpeedFactor({int(percent)})")

    async def mode(self) -> int:
        return int(await self._ok(self.dash, "RobotMode()"))

    async def pose(self) -> tuple:
        return tuple(float(v) for v in (await self._ok(self.dash, "GetPose()")).split(","))

    async def error_id(self) -> int:
        return int(await self._ok(self.dash, "GetErrorID()"))

    async def move_l(self, x: float, y: float, z: float, r: float = 0.0) -> None:
        await self._ok(self.motion, f"MovL({_num(x)},{_num(y)},{_num(z)},{_num(r)})")

    async def tool(self, close: bool) -> None:
        await self._ok(self.motion, f"ToolDO(1,{1 if close else 0})")

    async def holding(self) -> bool:
        return (await self._ok(self.dash, "ToolDI(1)")) == "1"

    async def sync(self) -> None:
        """Wait for queued motion and raise ArmFault if the arm ended up in an error state."""
        code, _ = await self._cmd(self.motion, "Sync()")
        mode = await self.mode()
        if mode in (MODE_ERROR, MODE_COLLISION):
            raise ArmFault(f"arm in mode {mode} (error id {await self.error_id()})")
        if code != 0:
            raise DeviceError(f"arm: Sync returned {code}", str(code))


class BalanceClient:
    def __init__(self, host: str, port: int, timeout_s: float = 60.0, trace=None):
        self.conn = LineConn(host, port, timeout_s, "balance", trace)

    async def connect(self) -> None:
        await self.conn.open()

    async def close(self) -> None:
        await self.conn.close()

    async def tare(self) -> None:
        r = await self.conn.ask("Z")
        if not r.startswith("OK"):
            raise DeviceError(f"balance: tare answered {r!r}", "tare")

    async def status(self) -> str:
        return await self.conn.ask("STATUS")

    async def weigh_stable(self) -> float:
        r = await self.conn.ask("S")
        parts = r.split(",")
        if parts[0] != "S" or len(parts) < 3:
            raise DeviceError(f"balance: {r}", parts[-1] if parts else "")
        return float(parts[1])


class ScannerClient:
    def __init__(self, host: str, port: int, timeout_s: float = 30.0, trace=None):
        self.conn = LineConn(host, port, timeout_s, "scanner", trace)

    async def connect(self) -> None:
        await self.conn.open()

    async def close(self) -> None:
        await self.conn.close()

    async def status(self) -> str:
        return await self.conn.ask("STATUS")

    async def trigger(self) -> str | None:
        """Return the decoded string, or None for a no-read."""
        r = await self.conn.ask("TRIG")
        if r == "NOREAD":
            return None
        if r.startswith("OK,"):
            return r[3:]
        raise DeviceError(f"scanner: {r}", r.split(",")[-1])


class HeaterClient:
    """Heated well: SET,<celsius> / OFF / TEMP / STATUS."""

    def __init__(self, host: str, port: int, timeout_s: float = 30.0, trace=None):
        self.conn = LineConn(host, port, timeout_s, "heater", trace)

    async def connect(self) -> None:
        await self.conn.open()

    async def close(self) -> None:
        await self.conn.close()

    async def status(self) -> str:
        return await self.conn.ask("STATUS")

    async def set(self, celsius: float) -> float:
        """Start heating toward `celsius`; returns the accepted target. Does not wait for it."""
        r = await self.conn.ask(f"SET,{_num(celsius)}")
        if r.startswith("OK,"):
            return float(r[3:])
        raise DeviceError(f"heater: {r}", r.split(",")[-1])

    async def off(self) -> None:
        r = await self.conn.ask("OFF")
        if r != "OK,OFF":
            raise DeviceError(f"heater: {r}", r.split(",")[-1])

    async def read(self) -> tuple:
        """(current celsius, target celsius or None when off, state OFF | HEATING | STABLE)."""
        r = await self.conn.ask("TEMP")
        parts = r.split(",")
        if parts[0] != "T" or len(parts) != 4:
            raise DeviceError(f"heater: unexpected reply {r!r}", "bad_reply")
        state = parts[3]
        return float(parts[1]), None if state == "OFF" else float(parts[2]), state


class SimAdmin:
    """Simulator-only control channel (rack loading, fault injection, inspection)."""

    def __init__(self, host: str, port: int, trace=None):
        self.conn = LineConn(host, port, 10.0, "admin", trace)

    async def connect(self) -> None:
        await self.conn.open()

    async def close(self) -> None:
        await self.conn.close()

    async def call(self, **req) -> dict:
        resp = json.loads(await self.conn.ask(json.dumps(req)))
        if not resp.get("ok"):
            raise DeviceError(f"sim-admin: {resp.get('error')}", "admin")
        return resp


DEFAULT_PORTS = {"arm_dashboard": 29999, "arm_motion": 30003, "balance": 5001, "scanner": 5002, "heater": 5003,
                 "admin": 9000}


class Devices:
    """One client per device. `admin` exists only when a simulator admin port is given."""

    def __init__(self, host: str, ports: dict | None = None, trace=None):
        p = {**DEFAULT_PORTS, **(ports or {})}
        self.arm = ArmClient(host, p["arm_dashboard"], p["arm_motion"], trace=trace)
        self.balance = BalanceClient(host, p["balance"], trace=trace)
        self.scanner = ScannerClient(host, p["scanner"], trace=trace)
        self.heater = HeaterClient(host, p["heater"], trace=trace)
        self.admin = SimAdmin(host, p["admin"]) if p.get("admin") is not None else None   # never traced

    def lines(self) -> dict:
        """Raw line connections by name, for sending a command exactly as typed."""
        out = {"dashboard": self.arm.dash, "motion": self.arm.motion, "balance": self.balance.conn,
               "scanner": self.scanner.conn, "heater": self.heater.conn}
        if self.admin is not None:
            out["admin"] = self.admin.conn
        return out

    async def connect(self) -> "Devices":
        for dev in (self.arm, self.balance, self.scanner, self.heater):
            await dev.connect()
        if self.admin is not None:
            await self.admin.connect()
        return self

    async def close(self) -> None:
        for dev in (self.arm, self.balance, self.scanner, self.heater):
            await dev.close()
        if self.admin is not None:
            await self.admin.close()
