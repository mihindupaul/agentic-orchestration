from __future__ import annotations

import asyncio

from .arm import Arm
from .clock import Clock
from .config import HeaterSpec, Layout, Timing
from .devices import Balance, Heater, Scanner
from .faults import KNOWN_FAULTS, Faults
from .kinematics import ik
from .servers import ArmProtocol, serve_admin, serve_arm, serve_feedback, serve_lines
from .world import World

DEFAULT_PORTS = {
    "arm_dashboard": 29999, "arm_motion": 30003, "arm_feedback": 30004,
    "balance": 5001, "scanner": 5002, "heater": 5003, "admin": 9000,
}

# Demo setup: a full rack in bay 3 (outer column, clearly visible from the front), an empty one in bay 8
# (diagonally opposite, so moves between them are easy to follow) and a second full rack in bay 2 whose vials
# have red caps, so they are easy to tell apart from bay 3's blue ones. Bay 2 is loaded last, so bay 3's vials
# keep the weights they had before it was added.
DEMO_SOURCE_SLOT, DEMO_EMPTY_SLOT, DEMO_RED_SLOT = 3, 8, 2


def load_demo_racks(world) -> None:
    world.load_rack(DEMO_SOURCE_SLOT, f"RACK{DEMO_SOURCE_SLOT:03d}", fill=1.0)
    world.load_rack(DEMO_EMPTY_SLOT, f"RACK{DEMO_EMPTY_SLOT:03d}", fill=0.0)
    world.load_rack(DEMO_RED_SLOT, f"RACK{DEMO_RED_SLOT:03d}", fill=1.0, cap="red")


class Simulator:
    def __init__(self, layout: Layout | None = None, timing: Timing | None = None, seed: int = 1,
                 heater: HeaterSpec | None = None):
        self.layout = layout or Layout()
        self.timing = timing or Timing()
        self.heater_spec = heater or HeaterSpec()
        self.clock = Clock(self.timing.time_scale)
        self.faults = Faults()
        self.world = World(self.layout, self.clock, seed)
        self.arm = Arm(self.world, self.clock, self.layout, self.timing, self.faults)
        self.balance = Balance(self.world, self.clock, self.timing, self.faults, seed + 1)
        self.scanner = Scanner(self.world, self.clock, self.timing, self.faults, self.arm)
        self.heater = Heater(self.world, self.clock, self.heater_spec, self.faults, self.arm)
        self._servers: list = []
        self.ports: dict = {}

    async def start(self, host: str = "127.0.0.1", ports: dict | None = None) -> dict:
        """Start all servers. Pass port 0 for any entry to get a free port (used by tests)."""
        self.ports = {**DEFAULT_PORTS, **(ports or {})}
        self.arm.start()
        proto = ArmProtocol(self.arm)
        specs = [
            ("arm_dashboard", serve_arm(proto, host, self.ports["arm_dashboard"])),
            ("arm_motion", serve_arm(proto, host, self.ports["arm_motion"])),
            ("arm_feedback", serve_feedback(self, host, self.ports["arm_feedback"])),
            ("balance", serve_lines(self.balance.handle, host, self.ports["balance"])),
            ("scanner", serve_lines(self.scanner.handle, host, self.ports["scanner"])),
            ("heater", serve_lines(self.heater.handle, host, self.ports["heater"])),
            ("admin", serve_admin(self, host, self.ports["admin"])),
        ]
        for name, coro in specs:
            server = await coro
            self._servers.append(server)
            self.ports[name] = server.sockets[0].getsockname()[1]
        return self.ports

    async def stop(self) -> None:
        for s in self._servers:
            s.close()
        for s in self._servers:
            await s.wait_closed()
        await self.arm.stop()

    # ------------------------------------------------------------- inspection
    def arm_feedback(self) -> dict:
        j = ik(self.layout, self.arm.pose[0], self.arm.pose[1]) or (0.0, 0.0)
        return {
            "t": round(self.clock.now(), 3),
            "mode": self.arm.mode,
            "error_id": self.arm.error_id,
            "error": self.arm.error_msg,
            "pose": [round(v, 3) for v in self.arm.pose],
            "joints": [round(j[0], 3), round(j[1], 3), round(self.arm.pose[2], 3), round(self.arm.pose[3], 3)],
            "gripper_closed": self.arm.gripper_closed,
            "holding": self.world.held is not None,
            "queue": self.arm.queue_len,
        }

    def heater_state(self) -> dict:
        h = self.heater
        state = h.state
        return {"celsius": round(h.celsius, 2), "target": h.target, "state": state}

    def state(self) -> dict:
        return {
            "arm": self.arm_feedback(),
            "world": self.world.snapshot(),
            "balance": {"tare_g": self.balance.tare_g},
            "heater": self.heater_state(),
            "scanner": {"busy": self.scanner.busy},
            "faults": self.faults.pending(),
        }

    def reset(self) -> None:
        """Back to how the simulator starts with demo racks, without restarting it: connections stay open.
        Full racks in bays 3 and 2 (red caps), empty rack in bay 8; nothing in the gripper or an instrument;
        heater off at ambient; arm at home, disabled, no error; no injected faults; balance untared."""
        self.arm.reset()
        self.world.clear()
        load_demo_racks(self.world)
        self.heater.reset()
        self.balance.tare_g = 0.0
        self.faults.clear()
        self.world.log("reset")

    # ------------------------------------------------------------------ admin
    def admin(self, req: dict) -> dict:
        """Simulator-only back door: what an operator does by hand (load racks), fault injection, and
        ground truth for tests. It never tells a client where things are: that is the controller's deck."""
        cmd = req.get("cmd")
        if cmd == "state":
            return {"ok": True, "state": self.state()}
        if cmd == "load_rack":
            self.world.load_rack(int(req["slot"]), str(req["barcode"]),
                                 fill=req.get("fill", 1.0), content_g=req.get("content_g"),
                                 cap=str(req.get("cap", "blue")))
            return {"ok": True}
        if cmd == "remove_rack":
            self.world.remove_rack(int(req["slot"]))
            return {"ok": True}
        if cmd == "inject":
            self.faults.inject(req["fault"], req.get("count", 1))
            return {"ok": True, "pending": self.faults.pending()}
        if cmd == "reset":
            self.reset()
            return {"ok": True}
        if cmd == "clear_faults":
            self.faults.clear()
            return {"ok": True}
        if cmd == "faults":
            return {"ok": True, "faults": KNOWN_FAULTS}
        if cmd == "rack_map":
            from .world import well_name
            rack = self.world.racks[int(req["slot"])]
            return {"ok": True, "present": rack.present, "barcode": rack.barcode,
                    "occupied": sorted(well_name(r, c) for (r, c) in rack.wells)}
        if cmd == "vial":
            from .world import well_name
            wanted = str(req["well"]).upper()
            rack = self.world.racks[int(req["slot"])]
            for (r, c), v in rack.wells.items():
                if well_name(r, c) == wanted:
                    return {"ok": True, "vial": {"barcode": v.barcode, "gross_g": v.gross_g, "peak_c": v.peak_c}}
            return {"ok": False, "error": "no vial in that well"}
        if cmd == "events":
            return {"ok": True, "events": self.world.events[int(req.get("since", 0)):]}
        return {"ok": False, "error": f"unknown admin command {cmd!r}"}


async def run_forever(sim: Simulator, host: str, ports: dict | None = None) -> None:
    ports = await sim.start(host, ports)
    print("workcell_sim listening:", ", ".join(f"{k}={v}" for k, v in ports.items()), flush=True)
    await asyncio.Event().wait()
