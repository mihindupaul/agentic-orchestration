"""Geometry and timing. Every number here is an ASSUMPTION, not a measured value.

Coordinates are millimetres, origin at the arm base, +Y toward the front of
the deck (where the operator stands), Z up from the deck surface. +X is the
operator's left: seen from the front, bay 1 is at the back-left on the +X side.

The workcell is an imaginary design made up for this educational project. Its deck
layout (square deck, a 2 x 4 grid of rack bays on the left, three station wells
spread over the right) and every dimension are invented; see docs/assumptions.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Timing:
    time_scale: float = 1.0        # >1 runs the simulation faster than real time
    xy_speed: float = 600.0        # mm/s at SpeedFactor 100
    z_speed: float = 250.0         # mm/s
    overhead_s: float = 0.08       # per-move accel/decel allowance
    gripper_s: float = 0.25
    enable_s: float = 0.5
    balance_settle_s: float = 2.0
    scan_s: float = 0.6


@dataclass
class HeaterSpec:
    """Heated well. All numbers invented (docs/assumptions.md)."""
    ambient_c: float = 22.0
    min_c: float = 25.0            # SET range
    max_c: float = 100.0
    ramp_c_per_s: float = 2.0      # fastest the well can heat or cool
    lag_s: float = 5.0             # first-order time constant near the target
    stable_band_c: float = 0.5     # STABLE once within this of the target ...
    stable_hold_s: float = 3.0     # ... for this long
    timeout_short_c: float = 5.0   # fault heater_timeout: plateaus this far below the target
    overshoot_c: float = 8.0       # fault heater_overshoot: peaks this far above the target


@dataclass
class Layout:
    # SCARA model: two equal links, 4 axes (J1, J2, Z, R). Long enough to reach the front bays.
    link1: float = 330.0
    link2: float = 330.0
    j1_limit_deg: float = 135.0
    j2_limit_deg: float = 155.0
    z_min: float = 0.0
    z_max: float = 150.0

    home: tuple = (0.0, 300.0, 100.0, 0.0)

    # Heights (Z above deck surface).
    clearance_z: float = 70.0      # lateral moves below this collide
    tube_top_z: float = 45.0       # tops of tubes standing in racks
    grip_z_max: float = 60.0       # gripper can grab/release at or below this
    dock_z_max: float = 25.0       # vial counts as docked in a well at or below this
    xy_tol: float = 3.0            # positional tolerance for pick/place/dock

    # SBS rack geometry.
    rack_len: float = 127.76
    rack_wid: float = 85.48
    well_pitch: float = 9.0
    n_rows: int = 8
    n_cols: int = 12

    # Eight landscape bays in two columns of four, on the operator's left (+X) of the arm.
    # Numbered in reading order seen from the front: 1 back-left, 2 back-right, ..., 8 front-right.
    # slot -> (centre x, centre y, phi deg). phi = -90 puts the long side across the deck with A1 at
    # the bay's back-left corner (operator view).
    rack_bays: dict = field(default_factory=lambda: {
        1: (315.0, 160.0, -90.0), 2: (150.0, 160.0, -90.0),
        3: (315.0, 270.0, -90.0), 4: (150.0, 270.0, -90.0),
        5: (315.0, 380.0, -90.0), 6: (150.0, 380.0, -90.0),
        7: (315.0, 490.0, -90.0), 8: (150.0, 490.0, -90.0),
    })

    # Round station wells spread over the operator's right (-X) of the arm.
    stations: dict = field(default_factory=lambda: {
        "scanner": (-170.0, 230.0),
        "heater": (-340.0, 330.0),
        "balance": (-170.0, 520.0),
    })
    station_well_d: float = 100.0

    # Square deck centred on the arm in X, arm base near the back edge.
    deck_back_y: float = -110.0
    deck_size: float = 840.0

    @property
    def n_racks(self) -> int:
        return len(self.rack_bays)

    def station_xy(self, name: str) -> tuple:
        return self.stations[name]
