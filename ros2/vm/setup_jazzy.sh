#!/usr/bin/env bash
# Installs ROS2 Jazzy desktop (incl. RViz2) and dev tools on Ubuntu 24.04. Run as the normal user with sudo.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

sudo apt-get update
sudo apt-get install -y software-properties-common curl locales
sudo locale-gen en_US en_US.UTF-8
sudo update-locale LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8
sudo add-apt-repository -y universe

# ros-apt-source sets up the ROS2 apt repository and its signing key.
VER=$(curl -s https://api.github.com/repos/ros-infrastructure/ros-apt-source/releases/latest | grep -F '"tag_name"' | awk -F'"' '{print $4}')
curl -fsSL -o /tmp/ros2-apt-source.deb \
  "https://github.com/ros-infrastructure/ros-apt-source/releases/download/${VER}/ros2-apt-source_${VER}.$(. /etc/os-release && echo "$VERSION_CODENAME")_all.deb"
sudo apt-get install -y /tmp/ros2-apt-source.deb

sudo apt-get update
sudo apt-get upgrade -y
sudo apt-get install -y ros-jazzy-desktop ros-dev-tools git python3-pip python3-venv mesa-utils

grep -qxF 'source /opt/ros/jazzy/setup.bash' ~/.bashrc || echo 'source /opt/ros/jazzy/setup.bash' >> ~/.bashrc
echo "ROS2 Jazzy installed."
