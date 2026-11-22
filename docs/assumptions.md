# Assumptions (invented or unverified)

The workcell is an imaginary design made up for this educational project; it does not describe any real
instrument. Everything below is invented for the simulator, or follows a public standard or published API.

- Deck layout (`workcell_sim/config.py`): an invented benchtop arrangement. Square deck
  (840 x 840 mm) with the arm base near the middle of the back edge. Eight landscape bays in a 2 x 4 grid on the
  operator's left, numbered in reading order from the back-left (1 2 / 3 4 / 5 6 / 7 8). Code reader, heater and
  balance wells spread over the operator's right. Dimensions are approximate, sized so a bay fits an SBS rack with
  clearance.
- The controller's deck file (`src/workcell_controller/decks/workcell.json`) repeats the bay, well and station positions as
  the controller's own copy, the way a real cell keeps taught positions. `tests/controller/test_deck.py` checks that it
  matches the simulated workcell. Safe, grip and dock heights live only in the deck file.
- Racks are modelled as SBS 8 x 12 at 9 mm pitch, a common standard footprint.
- Arm links (2 x 330 mm), joint limits (J1 +/-135 deg, J2 +/-155 deg), heights, timings: invented so that every bay and
  well is reachable from the base, along every straight path between them.
- Arm command syntax follows DOBOT's public TCP/IP guide. Only the six-axis V4 version was checked; the guide for the
  4-axis arm (M1 Pro) has not been checked yet, so command details may differ. Error codes are placeholders.
- Balance, scanner and heater protocols are generic placeholders.
- Heater well (`workcell_sim/config.py:HeaterSpec`): every number is invented. Ambient 22 C; set range 25-100 C;
  heats or cools at most 2 C per simulated second, with a first-order lag (time constant 5 s) near the target;
  STABLE after 3 s within 0.5 C of the target; cools back toward ambient when off. The vial is set down in the
  well and released (like the balance), so the arm is free while it heats. Fault `heater_timeout` plateaus
  5 C short of the target, `heater_overshoot` peaks 8 C over and then settles. The vial records the hottest
  temperature it saw (`peak_c`), sampled whenever the heater state is read.
- No doors/shutters yet; vials are lowered into open wells.
- RViz look (column height, link shapes, vial 8 x 45 mm, rack height, finger offsets, colours): cosmetic constants
  at the top of `ros2/workcell_sim_ros/workcell_sim_ros/scene.py`; they never affect the simulation.
- The controller's service rules (one operation at a time, fault latch, which errors latch) are this project's own design.
- Orca integration is verified against Orca 2.3.0 / cheshire-drivers 1.2.0 (see `docs/orca-integration.md`). The
  rack is declared to Orca as a deep-well 96 plate, the closest catalog entry with the same SBS footprint.
