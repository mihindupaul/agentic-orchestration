#!/bin/zsh
# Creates the ros2-sim VM in UTM on an Apple Silicon Mac: downloads + verifies the Ubuntu ISO, then creates the VM.
# Afterwards: start it in UTM, run the Ubuntu installer, eject the ISO, and follow ros2/vm/README.md.
set -euo pipefail
ISO_NAME=ubuntu-24.04.5-desktop-arm64.iso
ISO_SHA256=2be09ca883921bff6d8e6b0bfbafd13e32436553b7086f33bce3a4c5bad8bd14
ISO_DIR=${ISO_DIR:-$HOME/VMs/iso}

command -v utmctl >/dev/null || brew install --cask utm
mkdir -p "$ISO_DIR"
if [[ ! -f "$ISO_DIR/$ISO_NAME" ]]; then
  curl -fL -o "$ISO_DIR/$ISO_NAME" "https://cdimage.ubuntu.com/releases/24.04/release/$ISO_NAME"
fi
echo "$ISO_SHA256  $ISO_DIR/$ISO_NAME" | shasum -a 256 -c

open -a UTM
sed -e "s#ISO_PATH#$ISO_DIR/$ISO_NAME#" -e "s#REPO_PATH#${0:A:h:h:h}#" "${0:A:h}/create_vm.applescript" | osascript -
