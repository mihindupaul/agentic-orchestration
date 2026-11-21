#!/usr/bin/env bash
# In the VM: stop every running copy of the simulator launch, including children a killed launch left behind.
# Stray robot_state_publishers with an old URDF make the arm flicker between models in RViz.
pkill -INT -f '[r]os2 launch workcell_sim_ros' || true
for _ in 1 2 3 4 5; do
  pgrep -f '[r]os2 launch workcell_sim_ros|[l]ib/workcell_sim_ros/sim_node|[r]obot_state_publisher|[r]viz2 -d' >/dev/null || exit 0
  sleep 1
done
pkill -f '[l]ib/workcell_sim_ros/sim_node|[r]obot_state_publisher|[r]viz2 -d' || true
sleep 1
