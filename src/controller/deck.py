"""The deck: where every bay, well and station is, in arm coordinates.

This is where labware addresses ("bay 3, well A1", "scanner") become physical coordinates. The devices
know nothing about bays or wells: the arm only takes x, y, z. The numbers live in a JSON deck file
(`decks/workcell.json` by default); on a real cell they would be taught or calibrated and saved there.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

ROW_LETTERS = "ABCDEFGHIJKLMNOP"
_WELL_RE = re.compile(r"^([A-P])(\d{1,2})$")
DEFAULT_DECK = Path(__file__).with_name("decks") / "workcell.json"


def well_name(row: int, col: int) -> str:
    return f"{ROW_LETTERS[row]}{col + 1}"


def parse_well(name: str, rows: int = 8, cols: int = 12) -> tuple:
    """'A1' -> (row 0, col 0), checked against the rack size."""
    m = _WELL_RE.match(str(name).strip().upper())
    if not m:
        raise ValueError(f"bad well name {name!r}, expected e.g. A1")
    row, col = ROW_LETTERS.index(m.group(1)), int(m.group(2)) - 1
    if not (0 <= row < rows and 0 <= col < cols):
        raise ValueError(f"well {name!r} is outside a {rows} x {cols} rack (A1..{well_name(rows - 1, cols - 1)})")
    return row, col


@dataclass(frozen=True)
class Target:
    """A resolved place on the deck: a name for people, coordinates for the arm."""

    name: str              # "bay 3 A1", "scanner", "home", "point"
    kind: str              # "well", "station", "home" or "point"
    xy: tuple
    work_z: float          # height to go down to here
    docks: bool = False    # True for wells where the vial stays in the gripper while the device works


@dataclass(frozen=True)
class Bay:
    centre: tuple
    rows: int
    cols: int
    pitch: float
    col_dir: tuple         # unit vector from column 1 toward column 12
    row_dir: tuple         # unit vector from row A toward row H

    def well_xy(self, row: int, col: int) -> tuple:
        along = (col - (self.cols - 1) / 2) * self.pitch
        across = (row - (self.rows - 1) / 2) * self.pitch
        return (round(self.centre[0] + along * self.col_dir[0] + across * self.row_dir[0], 3),
                round(self.centre[1] + along * self.col_dir[1] + across * self.row_dir[1], 3))


class Deck:
    def __init__(self, data: dict):
        h = data["heights"]
        self.name = data.get("name", "deck")
        self.safe_z, self.grip_z, self.dock_z = float(h["safe_z"]), float(h["grip_z"]), float(h["dock_z"])
        self.home = tuple(float(v) for v in data["home"])
        types = data["rack_types"]
        self.bays = {}
        for key, b in data["bays"].items():
            t = types[b["rack_type"]]
            self.bays[int(key)] = Bay(tuple(b["centre"]), int(t["rows"]), int(t["cols"]), float(t["pitch"]),
                                      tuple(b["col_dir"]), tuple(b["row_dir"]))
        self.stations = {name: tuple(s["xy"]) for name, s in data["stations"].items()}
        self.docking = {name for name, s in data["stations"].items() if s.get("use") == "dock"}

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Deck":
        with open(path or DEFAULT_DECK) as f:
            return cls(json.load(f))

    # ----------------------------------------------------------- addressing
    def bay(self, number: int) -> Bay:
        if number not in self.bays:
            raise ValueError(f"no bay {number} on this deck (bays {min(self.bays)}-{max(self.bays)})")
        return self.bays[number]

    def well_xy(self, bay: int, well: str) -> tuple:
        b = self.bay(bay)
        return b.well_xy(*parse_well(well, b.rows, b.cols))

    def wells(self, bay: int) -> list:
        """Every well of a bay, column by column (the order a rack is usually worked through)."""
        b = self.bay(bay)
        return [well_name(r, c) for c in range(b.cols) for r in range(b.rows)]

    def check_well(self, bay: int, well: str) -> str:
        b = self.bay(bay)
        parse_well(well, b.rows, b.cols)
        return well.strip().upper()

    def well(self, bay: int, well: str) -> Target:
        well = self.check_well(bay, well)
        return Target(f"bay {bay} {well}", "well", self.well_xy(bay, well), self.grip_z)

    def station(self, name: str) -> Target:
        if name not in self.stations:
            raise ValueError(f"no station {name!r} (have {', '.join(self.stations)})")
        docks = name in self.docking
        return Target(name, "station", self.stations[name], self.dock_z if docks else self.grip_z, docks)

    def resolve(self, words) -> Target:
        """Turn what a person types into a target: 'scanner' | 'home' | '3 A1' | 'bay 3 A1' | 'x y'."""
        words = words.split() if isinstance(words, str) else list(words)
        if words and words[0].lower() == "bay":
            words = words[1:]
        if not words:
            raise ValueError("where to? e.g. scanner, home, 3 A1, or x y")
        first = words[0].lower()
        if first in self.stations:
            return self.station(first)
        if first == "home":
            return Target("home", "home", self.home[:2], self.home[2])
        if len(words) >= 2 and first.isdigit() and words[1][:1].isalpha():
            return self.well(int(first), words[1])
        if len(words) >= 2:
            return Target("point", "point", (float(words[0]), float(words[1])), self.grip_z)
        raise ValueError(f"unknown place {' '.join(words)!r}")
