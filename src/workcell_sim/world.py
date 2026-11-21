"""Shared physical state: racks, vials, the balance pan, the heater well and whatever the gripper holds."""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Optional

from .config import Layout

ROW_LETTERS = "ABCDEFGH"


def well_name(row: int, col: int) -> str:
    return f"{ROW_LETTERS[row]}{col + 1}"


@dataclass
class Vial:
    vid: str
    barcode: str
    empty_g: float
    content_g: float = 0.0
    peak_c: Optional[float] = None  # hottest the vial has been in the heater well, None if never heated
    code_quality: float = 1.0      # below 0.5 the scanner cannot read it

    @property
    def gross_g(self) -> float:
        return self.empty_g + self.content_g


@dataclass
class Rack:
    slot: int
    phi_deg: float
    cx: float
    cy: float
    barcode: str = ""
    present: bool = False
    wells: dict = field(default_factory=dict)   # (row, col) -> Vial

    def axes(self):
        p = math.radians(self.phi_deg)
        e_long = (math.sin(p), math.cos(p))      # long side points radially
        e_short = (math.cos(p), -math.sin(p))
        return e_long, e_short


class World:
    def __init__(self, layout: Layout, clock, seed: int = 1):
        self.layout = layout
        self.clock = clock
        self.rng = random.Random(seed)
        self.racks = {}
        for slot, (cx, cy, phi) in layout.rack_bays.items():
            self.racks[slot] = Rack(slot, phi, cx, cy)
        self.pan: Optional[Vial] = None
        self.pan_since: float = 0.0     # sim time the pan load last changed
        # Vials set down in a station well (the arm is free meanwhile). The balance pan is `pan`.
        self.in_well: dict = {"scanner": None, "heater": None}
        self.held: Optional[Vial] = None
        self.events: list = []

    # ---------------------------------------------------------------- logging
    def log(self, kind: str, **info) -> None:
        self.events.append({"t": round(self.clock.now(), 3), "event": kind, **info})

    # ------------------------------------------------------------------ racks
    def well_xy(self, rack: Rack, row: int, col: int) -> tuple:
        lay = self.layout
        along = (col - (lay.n_cols - 1) / 2) * lay.well_pitch
        across = (row - (lay.n_rows - 1) / 2) * lay.well_pitch
        e_l, e_s = rack.axes()
        return (rack.cx + along * e_l[0] + across * e_s[0],
                rack.cy + along * e_l[1] + across * e_s[1])

    def load_rack(self, slot: int, barcode: str, fill=1.0, content_g=None) -> None:
        """Operator places a rack. `fill` is a fraction (filled column-major) or a list of well names."""
        rack = self.racks[slot]
        lay = self.layout
        rack.barcode, rack.present, rack.wells = barcode, True, {}
        total = lay.n_rows * lay.n_cols
        if isinstance(fill, (list, tuple)):
            wanted = {str(w).upper() for w in fill}
            coords = [(r, c) for c in range(lay.n_cols) for r in range(lay.n_rows)
                      if well_name(r, c) in wanted]
        else:
            n = round(total * float(fill))
            coords = [(r, c) for c in range(lay.n_cols) for r in range(lay.n_rows)][:n]
        for r, c in coords:
            code = f"{barcode}-{well_name(r, c)}"
            rack.wells[(r, c)] = Vial(
                vid=code, barcode=code,
                empty_g=round(self.rng.uniform(1.70, 1.90), 5),
                content_g=round(content_g if content_g is not None else self.rng.uniform(0.0, 0.5), 5))
        self.log("rack_loaded", slot=slot, barcode=barcode, vials=len(rack.wells))

    def remove_rack(self, slot: int) -> None:
        rack = self.racks[slot]
        rack.present, rack.wells = False, {}
        self.log("rack_removed", slot=slot)

    def locate(self, x: float, y: float):
        """Return ('well', rack, row, col) if (x, y) is within tolerance of a well of a present rack,
        ('rack', rack) if inside a rack footprint but not on a well, else None."""
        tol = self.layout.xy_tol
        for rack in self.racks.values():
            if not rack.present:
                continue
            for r in range(self.layout.n_rows):
                for c in range(self.layout.n_cols):
                    wx, wy = self.well_xy(rack, r, c)
                    if math.hypot(x - wx, y - wy) <= tol:
                        return ("well", rack, r, c)
            e_l, e_s = rack.axes()
            dx, dy = x - rack.cx, y - rack.cy
            along = dx * e_l[0] + dy * e_l[1]
            across = dx * e_s[0] + dy * e_s[1]
            if abs(along) <= self.layout.rack_len / 2 and abs(across) <= self.layout.rack_wid / 2:
                return ("rack", rack)
        return None

    # --------------------------------------------------------------- stations
    def station_at(self, x: float, y: float) -> Optional[str]:
        for name in self.layout.stations:
            sx, sy = self.layout.station_xy(name)
            if math.hypot(x - sx, y - sy) <= self.layout.xy_tol:
                return name
        return None

    def docked(self, pose, station: str) -> bool:
        """True when the gripper holds a vial down inside the named station's well."""
        x, y, z, _ = pose
        if station == "balance":
            return False
        return (self.held is not None and z <= self.layout.dock_z_max
                and self.station_at(x, y) == station)

    # ---------------------------------------------------------- gripper logic
    def grip(self, pose) -> str:
        x, y, z, _ = pose
        if self.held is not None:
            return "already_holding"
        if z > self.layout.grip_z_max:
            return "closed_empty"
        if self.station_at(x, y) == "balance" and self.pan is not None:
            self.held, self.pan = self.pan, None
            self.pan_since = self.clock.now()
            self.log("pick", src="balance", vial=self.held.vid)
            return "gripped"
        station = self.station_at(x, y)
        if station in self.in_well and self.in_well[station] is not None:
            self.held, self.in_well[station] = self.in_well[station], None
            self.log("pick", src=station, vial=self.held.vid)
            return "gripped"
        hit = self.locate(x, y)
        if hit and hit[0] == "well":
            _, rack, r, c = hit
            vial = rack.wells.pop((r, c), None)
            if vial is not None:
                self.held = vial
                self.log("pick", src=f"rack{rack.slot}:{well_name(r, c)}", vial=vial.vid)
                return "gripped"
        return "closed_empty"

    def release(self, pose) -> str:
        x, y, z, _ = pose
        v = self.held
        if v is None:
            return "nothing_held"
        if z <= self.layout.grip_z_max:
            if self.station_at(x, y) == "balance" and self.pan is None:
                self.pan, self.held = v, None
                self.pan_since = self.clock.now()
                self.log("place", dst="balance", vial=v.vid)
                return "placed_balance"
            station = self.station_at(x, y)
            if station in self.in_well and self.in_well[station] is None:
                self.in_well[station], self.held = v, None
                self.log("place", dst=station, vial=v.vid)
                return f"placed_{station}"
            hit = self.locate(x, y)
            if hit and hit[0] == "well":
                _, rack, r, c = hit
                if (r, c) not in rack.wells:
                    rack.wells[(r, c)] = v
                    self.held = None
                    self.log("place", dst=f"rack{rack.slot}:{well_name(r, c)}", vial=v.vid)
                    return "placed_rack"
        self.held = None
        self.log("vial_dropped", vial=v.vid, at=[round(x, 1), round(y, 1), round(z, 1)])
        return "dropped"

    @property
    def in_heater(self) -> Optional[Vial]:
        return self.in_well["heater"]

    def vial_at(self, station: str, pose) -> Optional[Vial]:
        """The vial in a station well: set down in it, or held down in it by the gripper."""
        if self.in_well.get(station) is not None:
            return self.in_well[station]
        return self.held if self.docked(pose, station) else None

    def lose_held(self, reason: str) -> None:
        if self.held is not None:
            self.log("vial_dropped", vial=self.held.vid, reason=reason)
            self.held = None

    # --------------------------------------------------------------- snapshot
    def snapshot(self) -> dict:
        return {
            "racks": {s: {"present": r.present, "barcode": r.barcode, "vials": len(r.wells)}
                      for s, r in self.racks.items()},
            "balance_pan": self.pan.vid if self.pan else None,
            "scanner_well": self.in_well["scanner"].vid if self.in_well["scanner"] else None,
            "heater_well": self.in_well["heater"].vid if self.in_well["heater"] else None,
            "held": self.held.vid if self.held else None,
        }
