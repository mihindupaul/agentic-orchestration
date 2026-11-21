"""Planar 2-link SCARA kinematics. Forward axis is +Y; positive angle turns toward +X."""

from __future__ import annotations

import math
from typing import Optional

from .config import Layout


def ik(layout: Layout, x: float, y: float) -> Optional[tuple]:
    """Return (j1_deg, j2_deg) or None if the point is unreachable or violates joint limits."""
    l1, l2 = layout.link1, layout.link2
    r2 = x * x + y * y
    c2 = (r2 - l1 * l1 - l2 * l2) / (2 * l1 * l2)
    if abs(c2) > 1.0:
        return None
    for sign in (1, -1):
        j2 = sign * math.acos(c2)
        j1 = math.atan2(x, y) - math.atan2(l2 * math.sin(j2), l1 + l2 * math.cos(j2))
        j1d, j2d = math.degrees(j1), math.degrees(j2)
        if abs(j1d) <= layout.j1_limit_deg and abs(j2d) <= layout.j2_limit_deg:
            return (j1d, j2d)
    return None


def fk(layout: Layout, j1_deg: float, j2_deg: float) -> tuple:
    j1, j12 = math.radians(j1_deg), math.radians(j1_deg + j2_deg)
    x = layout.link1 * math.sin(j1) + layout.link2 * math.sin(j12)
    y = layout.link1 * math.cos(j1) + layout.link2 * math.cos(j12)
    return (x, y)
