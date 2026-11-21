"""The RViz scene must agree with the simulator: arm tip at the commanded pose, vials in their wells."""

import asyncio
import math
import os
import sys
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "ros2", "workcell_sim_ros"))

from workcell_sim import Simulator, Timing  # noqa: E402
from workcell_sim.config import Layout  # noqa: E402
from workcell_sim.kinematics import ik  # noqa: E402
from workcell_sim_ros import scene  # noqa: E402


def _rotz(v, a):
    c, s = math.cos(a), math.sin(a)
    return (c * v[0] - s * v[1], s * v[0] + c * v[1], v[2])


def tool_position(urdf: str, values: dict) -> tuple:
    """Forward kinematics through the URDF chain world -> tool (z-axis revolute/prismatic joints only)."""
    joints = {j.find("child").get("link"): j for j in ET.fromstring(urdf).iter("joint")}
    chain, link = [], "tool"
    while link in joints:
        chain.append(joints[link])
        link = joints[link].find("parent").get("link")
    pos, yaw = (0.0, 0.0, 0.0), 0.0
    for j in reversed(chain):
        origin = tuple(float(v) for v in j.find("origin").get("xyz").split())
        o = _rotz(origin, yaw)
        pos = (pos[0] + o[0], pos[1] + o[1], pos[2] + o[2])
        if j.get("type") == "fixed":
            continue
        axis = tuple(float(v) for v in j.find("axis").get("xyz").split())
        q = values[j.get("name")]
        if j.get("type") == "revolute":
            yaw += q * axis[2]
        else:
            pos = (pos[0], pos[1], pos[2] + q * axis[2])
    return pos


def test_urdf_tip_matches_simulator_pose():
    sim = Simulator()
    for x, y, z in [(0, 215, 100), (-281.7, 17.7, 20), (138.2, 164.7, 10), (250, 250, 150)]:
        j1, j2 = ik(sim.layout, x, y)
        snap = {"joints": [j1, j2, z, 0.0], "gripper_closed": False, "held": None}
        values = dict(zip(scene.JOINTS, scene.joint_positions(snap)))
        tx, ty, tz = tool_position(scene.urdf(sim.layout), values)
        assert math.isclose(tx * 1000, x, abs_tol=0.05)
        assert math.isclose(ty * 1000, y, abs_tol=0.05)
        assert math.isclose(tz * 1000, z, abs_tol=0.05)


def test_urdf_joint_names_match_published_joints():
    root = ET.fromstring(scene.urdf())
    movable = {j.get("name") for j in root.iter("joint") if j.get("type") != "fixed" and j.find("mimic") is None}
    assert movable == set(scene.JOINTS)


def _snapshot(setup=None) -> dict:
    sim = Simulator(timing=Timing(time_scale=5000.0))
    sim.world.load_rack(1, "RACK001", fill=["A1", "B2", "H12"])
    if setup:
        setup(sim)
    return sim, scene.snapshot(sim)


def test_vials_are_drawn_in_their_wells():
    sim, snap = _snapshot()
    bodies = {m["id"]: m for m in scene.dynamic_markers(snap) if m["ns"] == "vial_body"}
    assert len(bodies) == 3
    rack = sim.world.racks[1]
    for (row, col) in rack.wells:
        x, y = sim.world.well_xy(rack, row, col)
        m = bodies[1000 + row * 12 + col]
        assert math.isclose(m["xyz"][0] * 1000, x, abs_tol=0.01)
        assert math.isclose(m["xyz"][1] * 1000, y, abs_tol=0.01)


def test_held_vial_follows_the_tool_and_heated_vials_are_tinted():
    def pick_heated(sim):
        vial = sim.world.racks[1].wells.pop((0, 0))
        vial.peak_c = 60.0
        sim.world.held = vial
    _, snap = _snapshot(pick_heated)
    markers = scene.dynamic_markers(snap)
    held = [m for m in markers if m["ns"].startswith("held_")]
    assert {m["ns"] for m in held} == {"held_body", "held_cap"}
    assert all(m["frame"] == "tool" for m in held)
    assert next(m for m in held if m["ns"] == "held_body")["rgba"] == scene.VIAL_HEATED
    assert sum(m["ns"] == "vial_body" for m in markers) == 2


def test_heater_label_follows_the_heater_state():
    _, snap = _snapshot()
    label = lambda: next(m for m in scene.dynamic_markers(snap) if m["ns"] == "heater_temp")   # noqa: E731
    glow = lambda: [m for m in scene.dynamic_markers(snap) if m["ns"] == "activity" and m["id"] == 2]   # noqa: E731
    assert label()["text"] == "22.0 C  off" and not glow()
    snap["heater"] = {"celsius": 41.26, "target": 60.0, "state": "HEATING"}
    assert label()["text"] == "41.3 C -> 60 C" and label()["rgba"] == scene.HEATER_RIM
    assert glow()[0]["rgba"] == scene.HEAT_GLOW
    snap["heater"] = {"celsius": 60.0, "target": 60.0, "state": "STABLE"}
    assert glow()[0]["rgba"] == scene.HEAT_STABLE and label()["rgba"] == scene.TEXT


def test_heater_well_is_orange_and_where_the_layout_says():
    rims = [m for m in scene.static_markers() if m["ns"] == "heater" and m["id"] == 0]
    hx, hy = Layout().station_xy("heater")
    assert rims and rims[0]["rgba"] == scene.HEATER_RIM
    assert math.isclose(rims[0]["xyz"][0] * 1000, hx) and math.isclose(rims[0]["xyz"][1] * 1000, hy)


def test_diff_sends_only_changes():
    _, snap = _snapshot()
    first = scene.dynamic_markers(snap)
    changed, deleted, shown = scene.diff({}, first)
    assert len(changed) == len(first) and not deleted
    snap["racks"][0]["vials"] = snap["racks"][0]["vials"][1:]
    changed, deleted, _ = scene.diff(shown, scene.dynamic_markers(snap))
    assert not changed and {ns for ns, _ in deleted} == {"vial_body", "vial_cap"}


def test_snapshot_runs_while_the_simulator_is_live():
    async def main():
        sim = Simulator(timing=Timing(time_scale=5000.0))
        ports = await sim.start(ports={k: 0 for k in ("arm_dashboard", "arm_motion", "arm_feedback", "balance",
                                                       "scanner", "heater", "admin")})
        try:
            snap = scene.snapshot(sim)
            assert snap["mode"] == 4 and len(snap["racks"]) == Layout().n_racks and ports["admin"]
        finally:
            await sim.stop()
    asyncio.run(main())
