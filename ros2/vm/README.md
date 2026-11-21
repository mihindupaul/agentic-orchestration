# ROS2 simulator VM (UTM on Apple Silicon)

The simulator and RViz run in this VM; everything else runs on the Mac and reaches it over the network
(see `docs/simulator-vm.md`). The easier way is the container in `ros2/docker` (`make sim-up`); use this VM when
you want a full Ubuntu desktop.

| | |
|---|---|
| Guest | Ubuntu 24.04.5 desktop arm64, ROS2 Jazzy desktop |
| UTM backend | QEMU + Apple hypervisor, `virtio-gpu-gl-pci` display (GPU-accelerated OpenGL for RViz) |
| Resources | 4 cores, 8 GB RAM, 32 GB disk (grows on use, ~15 GB after setup) |
| Network | UTM "shared": the VM gets a `192.168.64.x` address the Mac can reach directly |
| Repo | shared from the Mac over VirtFS (tag `share`), mounted read-only-for-the-VM-user at `~/agentic-orchestration` |

## 1. Create the VM (Mac)
```
ros2/vm/create_vm.sh
```
Start `ros2-sim` in UTM and run the Ubuntu installer: erase disk (the empty virtual disk), default apps, then
create the user. When it asks to remove the installation medium, stop the VM, remove the ISO drive in the VM
settings and start it again.

## 2. Allow SSH from the Mac (in the VM, once)
Open a terminal in the VM and install the SSH server and the UTM guest agent:
```
sudo apt-get install -y openssh-server qemu-guest-agent && ip -4 -brief addr
```
On the Mac, copy a key over (`ssh-copy-id`) and add a host entry, for example:
```
Host ros2-sim
    HostName 192.168.64.4
    User ros
```

## 3. Install ROS2 Jazzy (from the Mac)
```
scp ros2/vm/setup_jazzy.sh ros2-sim:/tmp/ && ssh -t ros2-sim bash /tmp/setup_jazzy.sh
```
Installs `ros-jazzy-desktop` (RViz2 included) and `ros-dev-tools`, and sources ROS from `~/.bashrc`.

## 4. Mount the repo (in the VM, once)
The repo stays on the Mac; `create_vm.sh` shares it with the VM (UTM: Sharing -> VirtFS, directory = repo).
Mount it at the same path on every boot:
```
mkdir -p ~/agentic-orchestration
echo "share $HOME/agentic-orchestration 9p trans=virtio,version=9p2000.L,rw,_netdev,nofail,msize=262144 0 0" | sudo tee -a /etc/fstab
sudo systemctl daemon-reload && sudo mount ~/agentic-orchestration
```
Files show up owned by the Mac user, so the VM user can read but not write them; edit on the Mac. Builds go to
`~/ws` in the VM (`colcon build --base-paths ~/agentic-orchestration/ros2 --symlink-install`), and the symlinks point back into
the share, so a Python change on the Mac only needs a restart.

## 5. Run
From the Mac, `ros2/vm/deploy.sh` rebuilds in the VM and (re)starts simulator + RViz; nothing is copied. In the VM itself use
`ros2/vm/run_sim.sh` and `ros2/vm/stop_sim.sh`. Always stop through `stop_sim.sh`: a leftover
`robot_state_publisher` from an old launch keeps publishing its old arm model and the arm flickers between the two.
