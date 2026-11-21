"""TCP front ends.

Arm      : DOBOT-style `Name(args)` commands, replies `ErrorID,{values},Name(args);`
           on the dashboard (29999) and motion (30003) ports, JSON-lines feedback on 30004.
Balance, scanner, heater : one ASCII line in, one line out.
Admin    : JSON lines, for the test harness (load racks, inject faults, read state).
"""

from __future__ import annotations

import asyncio
import json
import re

from .arm import OK
from .kinematics import ik

CMD_RE = re.compile(r"([A-Za-z_][A-Za-z_0-9]*)\s*\(([^)]*)\)")


def _floats(args: str) -> list:
    out = []
    for tok in args.replace("pose=", "").replace("{", "").replace("}", "").split(","):
        tok = tok.strip()
        if tok:
            out.append(float(tok))
    return out


class ArmProtocol:
    def __init__(self, arm):
        self.arm = arm

    async def execute(self, name: str, args: str) -> str:
        arm, key = self.arm, name.lower()
        echo = f"{name}({args})"

        def reply(code: int, values: str = "") -> str:
            return f"{code},{{{values}}},{echo};"

        try:
            if key == "enablerobot":
                return reply(await arm.enable())
            if key == "disablerobot":
                return reply(arm.disable())
            if key == "clearerror":
                return reply(arm.clear_error())
            if key == "emergencystop":
                return reply(arm.emergency_stop())
            if key == "stop":
                return reply(arm.halt())
            if key == "robotmode":
                return reply(OK, str(arm.mode))
            if key == "geterrorid":
                return reply(OK, str(arm.error_id))
            if key == "getpose":
                return reply(OK, ",".join(f"{v:.3f}" for v in arm.pose))
            if key == "getangle":
                j = ik(arm.layout, arm.pose[0], arm.pose[1]) or (0.0, 0.0)
                return reply(OK, f"{j[0]:.3f},{j[1]:.3f},{arm.pose[2]:.3f},{arm.pose[3]:.3f}")
            if key == "speedfactor":
                return reply(arm.set_speed(int(_floats(args)[0])))
            if key in ("movl", "movj"):
                v = _floats(args)
                if len(v) < 4:
                    return reply(-4)
                return reply(arm.move(key, v[0], v[1], v[2], v[3]))
            if key == "tooldo":
                v = _floats(args)
                return reply(arm.tool_do(int(v[0]), int(v[1])))
            if key == "tooldi":
                return reply(OK, str(arm.tool_di(int(_floats(args)[0]))))
            if key == "sync":
                return reply(await arm.sync())
        except (ValueError, IndexError):
            return reply(-4)
        return f"-10000,{{}},{echo};"


async def serve_arm(protocol: ArmProtocol, host: str, port: int):
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        buf = ""
        try:
            while data := await reader.read(4096):
                buf += data.decode(errors="ignore")
                while (m := CMD_RE.search(buf)):
                    buf = buf[m.end():]
                    writer.write((await protocol.execute(m.group(1), m.group(2)) + "\n").encode())
                    await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
    return await asyncio.start_server(handler, host, port)


async def serve_feedback(sim, host: str, port: int, period_s: float = 0.1):
    async def handler(reader, writer):
        try:
            while True:
                writer.write((json.dumps(sim.arm_feedback()) + "\n").encode())
                await writer.drain()
                await asyncio.sleep(period_s)
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            writer.close()
    return await asyncio.start_server(handler, host, port)


async def serve_lines(handle, host: str, port: int):
    async def handler(reader, writer):
        try:
            while line := await reader.readline():
                text = line.decode(errors="ignore").strip()
                if not text:
                    continue
                writer.write((await handle(text) + "\n").encode())
                await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
    return await asyncio.start_server(handler, host, port)


async def serve_admin(sim, host: str, port: int):
    async def handler(reader, writer):
        try:
            while line := await reader.readline():
                try:
                    resp = sim.admin(json.loads(line))
                except Exception as exc:  # noqa: BLE001 - report any bad request to the client
                    resp = {"ok": False, "error": str(exc)}
                writer.write((json.dumps(resp) + "\n").encode())
                await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
    return await asyncio.start_server(handler, host, port)
