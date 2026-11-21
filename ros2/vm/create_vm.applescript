-- Creates the ROS2 simulator VM in UTM (QEMU backend, GPU-accelerated display, shared network).
set isoFile to POSIX file "ISO_PATH"
tell application "UTM"
	set vm to make new virtual machine with properties {backend:qemu, configuration:{name:"ros2-sim", architecture:"aarch64", machine:"virt", memory:8192, cpu cores:4, hypervisor:true, uefi:true, drives:{{removable:true, source:isoFile}, {guest size:32768}}, network interfaces:{{hardware:"virtio-net-pci", mode:shared}}, displays:{{hardware:"virtio-gpu-gl-pci", dynamic resolution:true}}, directory share mode:VirtFS}}
	update registry vm with {POSIX file "REPO_PATH"}
	return id of vm
end tell
