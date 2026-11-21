#!/bin/zsh
# On the Mac: rebuild the ROS package in the VM and restart simulator + RViz.
# The VM reads this repo live from a UTM shared folder (see README), so nothing is copied; the symlinked
# build means Python edits only need this restart. Build output stays VM-local in ~/ws.
#   ros2/vm/deploy.sh [ssh-host] [launch args...]      (default host: ros2-sim)
set -euo pipefail
VM=${1:-ros2-sim}; shift $(( $# > 0 ? 1 : 0 ))
ssh "$VM" "mountpoint -q \$HOME/agentic-orchestration || { echo 'repo share not mounted at ~/agentic-orchestration'; exit 1; } \
  && source /opt/ros/jazzy/setup.bash && mkdir -p \$HOME/ws && cd \$HOME/ws \
  && colcon build --base-paths \$HOME/agentic-orchestration/ros2 --symlink-install --event-handlers console_cohesion- >/dev/null \
  && \$HOME/agentic-orchestration/ros2/vm/stop_sim.sh \
  && (nohup \$HOME/agentic-orchestration/ros2/vm/run_sim.sh $* > /tmp/workcell_sim.log 2>&1 < /dev/null &) && sleep 8 \
  && grep -E 'device ports|Error|Traceback' /tmp/workcell_sim.log | head -5"
