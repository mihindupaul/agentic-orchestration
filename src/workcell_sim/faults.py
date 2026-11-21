from __future__ import annotations

KNOWN_FAULTS = {
    "scanner_noread": "next scan returns NOREAD",
    "heater_timeout": "next SET plateaus 5 C short of the target and never becomes STABLE",
    "heater_overshoot": "next SET overshoots the target by 8 C, then settles",
    "balance_unstable": "next stable-weight request times out",
    "gripper_miss": "next gripper close grabs nothing",
    "gripper_slip": "next successful grip drops the vial a moment later",
    "arm_collision": "next move ends in a collision error",
}


class Faults:
    """Counted one-shot fault injection, consumed by the devices."""

    def __init__(self):
        self._pending: dict = {}

    def inject(self, name: str, count: int = 1) -> None:
        if name not in KNOWN_FAULTS:
            raise ValueError(f"unknown fault {name!r}; known: {sorted(KNOWN_FAULTS)}")
        self._pending[name] = self._pending.get(name, 0) + int(count)

    def consume(self, name: str) -> bool:
        n = self._pending.get(name, 0)
        if n <= 0:
            return False
        if n == 1:
            del self._pending[name]
        else:
            self._pending[name] = n - 1
        return True

    def clear(self) -> None:
        self._pending.clear()

    def pending(self) -> dict:
        return dict(self._pending)
