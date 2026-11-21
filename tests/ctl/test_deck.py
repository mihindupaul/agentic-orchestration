"""The controller's deck file must agree with where the simulated workcell really put things.

On real hardware this is calibration: if the deck file is off, picks miss. Here the simulator's world
model is the physical truth, so the check can be exact."""

import math

import pytest

from vialctl.deck import Deck, parse_well
from workcell_sim import Simulator


def test_deck_matches_the_simulated_workcell():
    deck, sim = Deck.load(), Simulator()
    assert set(deck.bays) == set(sim.world.racks)
    for n, rack in sim.world.racks.items():
        b = deck.bay(n)
        for well in ("A1", "A12", "H1", "H12", "D7"):
            row, col = parse_well(well)
            assert math.dist(b.well_xy(row, col), sim.world.well_xy(rack, row, col)) < 0.01, (n, well)
    for name, xy in deck.stations.items():
        assert math.dist(xy, sim.layout.station_xy(name)) < 0.01, name


def test_addresses_resolve_to_targets():
    deck = Deck.load()
    t = deck.resolve("bay 3 a1")
    assert t.name == "bay 3 A1" and t.kind == "well" and t.work_z == deck.grip_z
    # Every station is a set-down well: the vial is released there and the arm is free meanwhile.
    assert not any(deck.station(name).docks for name in deck.stations)
    assert deck.resolve("scanner").work_z == deck.grip_z
    assert deck.resolve("0 300").xy == (0.0, 300.0)
    assert deck.wells(1)[:3] == ["A1", "B1", "C1"] and len(deck.wells(1)) == 96


@pytest.mark.parametrize("words", ["9 A1", "3 Z1", "3 A13", "nowhere"])
def test_bad_addresses_are_refused(words):
    with pytest.raises(ValueError):
        Deck.load().resolve(words)
