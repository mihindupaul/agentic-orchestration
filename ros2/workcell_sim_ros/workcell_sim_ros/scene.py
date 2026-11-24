"""What RViz shows, as plain data: the arm URDF, a state snapshot and marker descriptions.

No ROS imports here, so it can be tested anywhere. `sim_node.py` turns the dicts into messages.
Every dimension comes from `workcell_sim.config.Layout`; the extra visual sizes below are
cosmetic only (they never affect the simulation) and are listed in docs/assumptions.md.

Frames: `world` is the simulator frame (metres, origin at the arm base, +X right, +Y front, Z up
from the deck). `tool` is the gripper tip, at the commanded (x, y, z).
"""

from __future__ import annotations

import math

from workcell_sim.config import Layout

MM = 0.001

# Cosmetic sizes (mm).
ARM_HEIGHT = 320.0          # top of the column, where link 1 turns
LINK2_DROP = 55.0           # link 2 runs under link 1
QUILL_LEN = 300.0
VIAL_D, VIAL_H, CAP_H = 8.0, 45.0, 6.0
VIAL_GRIP_DEPTH = 20.0      # vial bottom is this far below the tip when gripped at grip height
RACK_H = 24.0
DECK_SKIRT = 140.0          # cabinet below the deck top
DISPLAY_DX = -135.0         # balance display box, beside the balance well (operator's right)
FINGER_OPEN, FINGER_HOLD, FINGER_EMPTY = 7.5, 5.5, 1.5   # finger centre offset from the tip axis

JOINTS = ["joint1", "joint2", "joint3_z", "joint4_r", "finger"]

GREY_LIGHT = (0.86, 0.87, 0.89, 1.0)
GREY_DARK = (0.22, 0.23, 0.26, 1.0)
BLACK = (0.08, 0.08, 0.09, 1.0)
DECK = (0.30, 0.31, 0.34, 1.0)
SKIRT = (0.90, 0.91, 0.92, 1.0)
LABEL = (0.92, 0.93, 0.95, 1.0)
SLOT = (0.58, 0.60, 0.64, 1.0)
RACK = (0.93, 0.93, 0.95, 0.55)
VIAL = (0.92, 0.95, 0.97, 0.9)
VIAL_HEATED = (0.98, 0.85, 0.72, 0.95)     # tint for a vial that has been in the heater
CAP = (0.15, 0.40, 0.85, 1.0)
TEXT = (0.92, 0.93, 0.95, 1.0)
WARN = (0.95, 0.30, 0.25, 1.0)
SCAN_BEAM = (1.0, 0.15, 0.10, 0.45)
HEATER_RIM = (0.98, 0.55, 0.10, 1.0)        # orange
HEAT_GLOW = (1.0, 0.45, 0.05, 0.55)         # heating toward the target
HEAT_STABLE = (1.0, 0.25, 0.05, 0.75)       # at the target

MODE_NAMES = {4: "disabled", 5: "idle", 7: "running", 9: "error", 11: "collision"}


# --------------------------------------------------------------------------- URDF
def _box(sx, sy, sz, xyz, rgba, name):
    return _visual(f'<box size="{sx * MM:.4f} {sy * MM:.4f} {sz * MM:.4f}"/>', xyz, rgba, name)


def _cyl(d, h, xyz, rgba, name):
    return _visual(f'<cylinder radius="{d / 2 * MM:.4f}" length="{h * MM:.4f}"/>', xyz, rgba, name)


def _visual(geometry, xyz, rgba, name):
    x, y, z = (v * MM for v in xyz)
    r, g, b, a = rgba
    return (f'<visual><origin xyz="{x:.4f} {y:.4f} {z:.4f}"/><geometry>{geometry}</geometry>'
            f'<material name="{name}"><color rgba="{r} {g} {b} {a}"/></material></visual>')


def urdf(layout: Layout | None = None) -> str:
    """SCARA arm built from primitives. Joint values: j1/j2/r in rad, z in m (tip height), finger in m."""
    lay = layout or Layout()
    l1, l2 = lay.link1, lay.link2
    h, drop = ARM_HEIGHT, LINK2_DROP
    links = {
        "base_link": [_cyl(130, h - 20, (0, 0, (h - 20) / 2), GREY_DARK, "base"),
                      _cyl(170, 12, (0, 0, 6), BLACK, "foot")],
        "link1": [_box(70, l1, 40, (0, l1 / 2, 0), GREY_LIGHT, "arm1"),
                  _cyl(90, 44, (0, 0, 0), GREY_LIGHT, "arm1_hub"),
                  _cyl(80, 44, (0, l1, 0), GREY_LIGHT, "arm1_tip")],
        "link2": [_box(60, l2, 36, (0, l2 / 2, 0), GREY_LIGHT, "arm2"),
                  _cyl(76, 40, (0, 0, 0), GREY_DARK, "elbow"),
                  _cyl(64, 60, (0, l2, 10), GREY_LIGHT, "arm2_tip")],
        "quill": [_cyl(16, QUILL_LEN, (0, 0, 60 + QUILL_LEN / 2), (0.75, 0.77, 0.80, 1.0), "quill")],
        "tool": [_box(34, 22, 26, (0, 0, 45 + 13), BLACK, "gripper_body")],
        "finger_left": [_box(3, 9, 40, (0, 0, 20 + 5), GREY_DARK, "finger")],
        "finger_right": [_box(3, 9, 40, (0, 0, 20 + 5), GREY_DARK, "finger")],
    }
    joints = [
        ("joint1", "revolute", "base_link", "link1", (0, 0, h), "0 0 -1", -lay.j1_limit_deg, lay.j1_limit_deg),
        ("joint2", "revolute", "link1", "link2", (0, l1, -drop), "0 0 -1", -lay.j2_limit_deg, lay.j2_limit_deg),
        ("joint3_z", "prismatic", "link2", "quill", (0, l2, -(h - drop)), "0 0 1", lay.z_min, lay.z_max),
        ("joint4_r", "revolute", "quill", "tool", (0, 0, 0), "0 0 -1", -360.0, 360.0),
        ("finger", "prismatic", "tool", "finger_left", (0, 0, 0), "1 0 0", 0.0, 10.0),
    ]
    out = ['<?xml version="1.0"?>', '<robot name="vial_scara">', '<link name="world"/>',
           '<joint name="world_fixed" type="fixed"><parent link="world"/><child link="base_link"/>'
           '<origin xyz="0 0 0"/></joint>']
    for name, visuals in links.items():
        out.append(f'<link name="{name}">{"".join(visuals)}</link>')
    for name, kind, parent, child, xyz, axis, lo, hi in joints:
        if kind == "revolute":
            lo, hi = math.radians(lo), math.radians(hi)
        else:
            lo, hi = lo * MM, hi * MM
        x, y, z = (v * MM for v in xyz)
        out.append(f'<joint name="{name}" type="{kind}"><parent link="{parent}"/><child link="{child}"/>'
                   f'<origin xyz="{x:.4f} {y:.4f} {z:.4f}"/><axis xyz="{axis}"/>'
                   f'<limit lower="{lo:.4f}" upper="{hi:.4f}" effort="10" velocity="2"/></joint>')
    out.append('<joint name="finger_mirror" type="prismatic"><parent link="tool"/><child link="finger_right"/>'
               '<origin xyz="0 0 0"/><axis xyz="-1 0 0"/><limit lower="0" upper="0.01" effort="10" velocity="2"/>'
               '<mimic joint="finger"/></joint>')
    out.append("</robot>")
    return "\n".join(out)


# ------------------------------------------------------------------- snapshot
def snapshot(sim) -> dict:
    """Copy everything the view needs. Call it on the simulator's event loop thread."""
    w, arm = sim.world, sim.arm
    racks = []
    for slot, r in w.racks.items():
        racks.append({"slot": slot, "cx": r.cx, "cy": r.cy, "phi": r.phi_deg, "present": r.present,
                      "barcode": r.barcode,
                      "vials": [(row, col, v.peak_c is not None) for (row, col), v in r.wells.items()]})
    fb = sim.arm_feedback()
    pan = w.pan
    return {
        "joints": fb["joints"],
        "pose": fb["pose"],
        "mode": fb["mode"],
        "error": fb["error"],
        "gripper_closed": arm.gripper_closed,
        "held": None if w.held is None else {"heated": w.held.peak_c is not None, "barcode": w.held.barcode},
        # A vial standing in a station is drawn where the gripper let it go (its release height).
        "pan": None if pan is None else {"heated": pan.peak_c is not None, "z": w.set_down_z["balance"]},
        "in_scanner": _set_down(w, "scanner"),
        "in_heater": _set_down(w, "heater"),
        "balance_g": (pan.gross_g if pan else 0.0) - sim.balance.tare_g,
        "scanner_busy": sim.scanner.busy,
        "heater": sim.heater_state(),
        "racks": racks,
    }


def rack_type(lay: Layout) -> str:
    """What is printed on a rack: its format, from the layout, e.g. 'SBS 96 (8x12)'."""
    return f"SBS {lay.n_rows * lay.n_cols} ({lay.n_rows}x{lay.n_cols})"


def _set_down(w, station: str) -> dict | None:
    vial = w.in_well[station]
    return None if vial is None else {"heated": vial.peak_c is not None, "z": w.set_down_z[station]}


def joint_positions(snap: dict) -> list:
    """Values for JOINTS, in ROS units."""
    j1, j2, z, r = snap["joints"]
    if not snap["gripper_closed"]:
        finger = FINGER_OPEN
    else:
        finger = FINGER_HOLD if snap["held"] else FINGER_EMPTY
    return [math.radians(j1), math.radians(j2), z * MM, math.radians(r), finger * MM]


# -------------------------------------------------------------------- markers
def _m(ns, mid, kind, xyz, scale, rgba, yaw=0.0, text="", frame="world", points=()):
    return {"ns": ns, "id": mid, "type": kind, "frame": frame,
            "xyz": tuple(round(v * MM, 5) for v in xyz), "yaw": round(yaw, 5),
            "scale": tuple(round(v * MM, 5) for v in scale), "rgba": rgba, "text": text,
            "points": tuple(tuple(round(v * MM, 5) for v in pt) for pt in points)}


def deck_outline(lay: Layout) -> list:
    """Square deck edge, anticlockwise from the back-left corner (mm)."""
    hw, back = lay.deck_size / 2, lay.deck_back_y
    return [(hw, back), (hw, back + lay.deck_size), (-hw, back + lay.deck_size), (-hw, back)]


def _deck_markers(lay: Layout) -> list:
    """Square deck: a dark 12 mm top sitting on a lighter cabinet skirt."""
    size, cy = lay.deck_size, lay.deck_back_y + lay.deck_size / 2
    skirt = DECK_SKIRT - 12
    return [_m("deck", 0, "cube", (0, cy, -6.0), (size, size, 12.0), DECK),
            _m("deck", 100, "cube", (0, cy, -12 - skirt / 2), (size - 6, size - 6, skirt), SKIRT)]


def _well(ns, x, y, rim_rgba, inner_rgba, d):
    """A round opening in the deck: coloured rim and a dark mouth the vial disappears into."""
    return [_m(ns, 0, "cylinder", (x, y, 0.3), (d + 8, d + 8, 0.6), rim_rgba),
            _m(ns, 1, "cylinder", (x, y, 0.5), (d, d, 0.6), inner_rgba)]


def _rack_yaw(phi_deg: float) -> float:
    # Rack long axis points radially: direction (sin phi, cos phi) in world XY.
    p = math.radians(phi_deg)
    return math.atan2(math.cos(p), math.sin(p))


def static_markers(layout: Layout | None = None) -> list:
    """Deck, rack slots and station bodies: published once."""
    lay = layout or Layout()
    out = _deck_markers(lay)
    for slot, (bx, by, phi) in lay.rack_bays.items():
        out.append(_m("slots", slot, "cube", (bx, by, 0.4), (lay.rack_len + 6, lay.rack_wid + 6, 0.8), SLOT,
                      yaw=_rack_yaw(phi)))
        # Bay number just off the bay's left end (operator view, +X).
        out.append(_m("slot_labels", slot, "text", (bx + lay.rack_len / 2 + 17, by, 5), (0, 0, 24),
                      LABEL, text=str(slot)))
    d = lay.station_well_d
    bx, by = lay.station_xy("balance")
    out += _well("balance", bx, by, (0.75, 0.77, 0.80, 1.0), (0.05, 0.05, 0.06, 1.0), d)
    out += [_m("balance", 2, "cylinder", (bx, by, 0.9), (60, 60, 0.8), (0.82, 0.84, 0.86, 1.0)),
            _m("balance", 3, "cylinder", (bx, by, 28), (d - 6, d - 6, 55), (0.80, 0.90, 1.0, 0.10))]
    sx, sy = lay.station_xy("scanner")
    out += _well("scanner", sx, sy, (0.85, 0.15, 0.12, 1.0), (0.02, 0.02, 0.02, 1.0), d)
    hx, hy = lay.station_xy("heater")
    out += _well("heater", hx, hy, HEATER_RIM, (0.12, 0.07, 0.05, 1.0), d)
    labels = {"heater": "Heater", "scanner": "Barcode scanner", "balance": "Balance"}
    for i, (name, text) in enumerate(labels.items()):
        x, y = lay.station_xy(name)
        out.append(_m("station_labels", i, "text", (x, y - d / 2 - 22, 5), (0, 0, 18), LABEL, text=text))
    # Balance display beside the balance well; the live reading is drawn on it.
    out += [_m("display", 0, "cube", (bx + DISPLAY_DX, by, 45), (150, 40, 90), (0.80, 0.81, 0.83, 1.0)),
            _m("display", 1, "cube", (bx + DISPLAY_DX, by + 20.5, 58), (110, 1, 28), BLACK)]
    return out


def _vial(ns_prefix, mid, x, y, z_bottom, heated, frame="world"):
    """Body and cap; a vial that has been heated gets a warm tint."""
    body = VIAL_H - CAP_H
    out = [_m(f"{ns_prefix}body", mid, "cylinder", (x, y, z_bottom + body / 2), (VIAL_D, VIAL_D, body),
              VIAL_HEATED if heated else VIAL, frame=frame),
           _m(f"{ns_prefix}cap", mid, "cylinder", (x, y, z_bottom + body + CAP_H / 2),
              (VIAL_D + 0.6, VIAL_D + 0.6, CAP_H), CAP, frame=frame)]
    return out


def dynamic_markers(snap: dict, layout: Layout | None = None) -> list:
    """Racks, vials, device activity and status text for one snapshot."""
    lay = layout or Layout()
    out = []
    for r in snap["racks"]:
        if not r["present"]:
            continue
        slot, yaw = r["slot"], _rack_yaw(r["phi"])
        out.append(_m("racks", slot, "cube", (r["cx"], r["cy"], RACK_H / 2), (lay.rack_len, lay.rack_wid, RACK_H),
                      RACK, yaw=yaw))
        p = math.radians(r["phi"])
        e_l, e_s = (math.sin(p), math.cos(p)), (math.cos(p), -math.sin(p))
        for row, col, heated in r["vials"]:
            along = (col - (lay.n_cols - 1) / 2) * lay.well_pitch
            across = (row - (lay.n_rows - 1) / 2) * lay.well_pitch
            x = r["cx"] + along * e_l[0] + across * e_s[0]
            y = r["cy"] + along * e_l[1] + across * e_s[1]
            out += _vial("vial_", slot * 1000 + row * lay.n_cols + col, x, y, 0.0, heated)
        # The rack type on the rack's front face, like a printed label (its barcode stays machine-readable
        # only: the barcode scanner builds vial codes from it).
        lx, ly = r["cx"] + e_s[0] * (lay.rack_wid / 2 + 2), r["cy"] + e_s[1] * (lay.rack_wid / 2 + 2)
        out.append(_m("rack_labels", slot, "text", (lx, ly, RACK_H / 2), (0, 0, 14), BLACK, text=rack_type(lay)))

    if snap["held"]:
        out += _vial("held_", 0, 0, 0, -VIAL_GRIP_DEPTH, snap["held"]["heated"], frame="tool")
    # Vials standing in a station: drawn at the height the gripper released them, so nothing jumps when the
    # gripper lets go or takes the vial again.
    for key, station, ns in (("pan", "balance", "pan_"), ("in_scanner", "scanner", "scanner_vial_"),
                             ("in_heater", "heater", "heater_vial_")):
        vial = snap.get(key)
        if vial:
            sx, sy = lay.station_xy(station)
            out += _vial(ns, 0, sx, sy, vial.get("z", 0.0) - VIAL_GRIP_DEPTH, vial["heated"])

    bx, by = lay.station_xy("balance")
    out.append(_m("readouts", 0, "text", (bx + DISPLAY_DX, by + 30, 58), (0, 0, 20), (0.4, 1.0, 0.6, 1.0),
                  text=f"{snap['balance_g']:.5f} g"))
    # Activity glows sit on top of the station so they stay visible around the docked vial.
    if snap["scanner_busy"]:
        sx, sy = lay.station_xy("scanner")
        out.append(_m("activity", 0, "cylinder", (sx, sy, 1.2), (lay.station_well_d - 10,) * 2 + (1.2,), SCAN_BEAM))
        out.append(_m("activity", 1, "cylinder", (sx, sy, 20), (16, 16, 40), (1.0, 0.2, 0.15, 0.18)))
    # Heater: glow while on, and a temperature label above the well that follows the device state.
    heater = snap["heater"]
    hx, hy = lay.station_xy("heater")
    if heater["state"] != "OFF":
        rgba = HEAT_STABLE if heater["state"] == "STABLE" else HEAT_GLOW
        out.append(_m("activity", 2, "cylinder", (hx, hy, 1.2), (lay.station_well_d - 10,) * 2 + (1.2,), rgba))
        text = f"{heater['celsius']:.1f} C, target {heater['target']:g} C"   # plain ASCII: RViz's font has no ° or →
    else:
        text = f"Off, {heater['celsius']:.1f} C"
    # On the open deck beside the well (toward the deck centre): neither the docked arm nor the balance
    # display stands between it and the default camera.
    out.append(_m("heater_temp", 0, "text", (hx + lay.station_well_d / 2 + 100, hy + 25, 5), (0, 0, 20),
                  HEATER_RIM if heater["state"] == "HEATING" else TEXT, text=text))

    mode = MODE_NAMES.get(snap["mode"], str(snap["mode"]))
    text = f"Arm: {mode}" if not snap["error"] else f"Arm: {mode} ({snap['error']})"
    out.append(_m("status", 0, "text", (0, lay.deck_back_y + lay.deck_size - 60, 12), (0, 0, 24),
                  WARN if snap["mode"] in (9, 11) else TEXT, text=text))
    return out


def diff(previous: dict, markers: list, full: bool = False) -> tuple:
    """Return (changed, deleted_keys, current) so only differences go over the wire. `full` resends every
    marker (for a late RViz) but still deletes the ones that are gone: forgetting `previous` instead would
    leave a vial that vanished in that same tick (a reset clears whole racks) drawn for good."""
    current = {(m["ns"], m["id"]): m for m in markers}
    changed = list(current.values()) if full else [m for k, m in current.items() if previous.get(k) != m]
    deleted = [k for k in previous if k not in current]
    return changed, deleted, current


