#!/bin/bash
# Build Seeed's stock Grove Vision AI V2 firmware (SSCMA 2025.01.02) with faster OV5647 camera timing.
# One image per patch, so they can be flashed and compared:
#
#   images/fast-1296.img  ov5647-fast.patch: stock 1296x972 mode, shorter lines and frames, quicker
#                         re-arm -> ~35 fps (was 15)
#   images/fast-640.img   ov5647-640.patch: the same plus the 640x480 mode, which reads half the rows -> 45 fps
#   images/fast-640-spi.img  + grove-spi.patch: also the SPI link to the stacked XIAO. EXPERIMENTAL: the V2
#                         then holds XIAO D9 (an ESP32-C3 boot pin) low, so the XIAO can't boot
#                         -> 45 fps (the 21 ms model's limit); tune live with AT+SETREG
#
#   ./build.sh            downloads what it needs into ./work, builds both, prints the flash command
#
# Needs ~1 GB of disk.
set -euo pipefail
cd "$(dirname "$0")"
HERE=$(pwd)
mkdir -p work images && cd work

# 1. Arm's official GCC 13.2 (the version Himax tested), unpacked here: no installer, no admin password.
TC=arm-gnu-toolchain-13.2.Rel1-darwin-arm64-arm-none-eabi
if [ ! -x "$TC/bin/arm-none-eabi-gcc" ]; then
  curl -fL -o tc.tar.xz "https://developer.arm.com/-/media/Files/downloads/gnu/13.2.rel1/binrel/arm-gnu-toolchain-13.2.rel1-darwin-arm64-arm-none-eabi.tar.xz"
  tar -xf tc.tar.xz && rm tc.tar.xz
  xattr -dr com.apple.quarantine "$TC" 2>/dev/null || true
fi
# The compiler goes on PATH itself: macOS's make 3.81 ignores the makefile's PATH override.
export PATH="$(pwd)/$TC/bin:$PATH"

# 2. Seeed's firmware at the commit that matches the shipped 2025.01.02 build: 4d64ff2 pins SSCMA-Micro
#    ff48a1c (the shipped camera code) and builds it with TFLite 2412, which that code needs.
[ -d sscma-example-we2 ] || git clone https://github.com/Seeed-Studio/sscma-example-we2.git
git -C sscma-example-we2 checkout -q 4d64ff2
git -C sscma-example-we2 submodule update --init EPII_CM55M_APP_S/library/sscma_micro
SSCMA=sscma-example-we2/EPII_CM55M_APP_S/library/sscma_micro
OBJ=sscma-example-we2/EPII_CM55M_APP_S/obj_epii_evb_icv30_bdv10
# TARGET must be the Grove board: the makefiles default to Seeed's SenseCAP A1102 (other pins and memory map).
MAKE_ARGS=(-C sscma-example-we2/EPII_CM55M_APP_S TARGET=GROVE_VISION_AI_V2)
[ -f "$OBJ/.opencorn-4d64ff2" ] || make "${MAKE_ARGS[@]}" clean >/dev/null
mkdir -p "$OBJ" && touch "$OBJ/.opencorn-4d64ff2"

build() {  # build <image name> <patch>...
  local name=$1; shift
  git -C "$SSCMA" checkout -q -- .
  for patch in "$@"; do git -C "$SSCMA" apply --whitespace=nowarn "$HERE/$patch"; done
  # make does not track the .i register tables or the shared headers, so rebuild SSCMA-Micro's objects.
  find "$OBJ"/gnu_epii_evb_WLCSP65/library/sscma_micro -name "*.o" -delete 2>/dev/null || true
  # The link step's object list needs GNU make 4's $(file ...); write it ourselves (see objs-list.mk).
  make "${MAKE_ARGS[@]}" -f "$HERE/objs-list.mk" opencorn-objs >/dev/null 2>&1 || { echo "Couldn't write the link's object list (objs-list.mk)"; exit 1; }
  echo "Compiling $name (full log in work/build.log)..."
  if ! make "${MAKE_ARGS[@]}" -j8 > build.log 2>&1; then
    grep -E "error|Error" build.log | head -20
    echo "Build failed, see $(pwd)/build.log"
    exit 1
  fi
  # 3. Pack the image with Himax's tool.
  (cd sscma-example-we2/we2_image_gen_local &&
   cp ../EPII_CM55M_APP_S/obj_epii_evb_icv30_bdv10/gnu_epii_evb_WLCSP65/EPII_CM55M_gnu_epii_evb_WLCSP65_s.elf input_case1_secboot/ &&
   ./we2_local_image_gen_macOS_arm64 project_case1_blp_wlcsp.json > /dev/null &&
   cp output_case1_sec_wlcsp/output.img "$HERE/images/$name.img")
  ls -la "$HERE/images/$name.img"
}
build fast-1296 ov5647-fast.patch
build fast-640 ov5647-640.patch
build fast-640-spi ov5647-640.patch grove-spi.patch

cat <<EOF

Built. To flash one: plug the V2's own USB-C into the Mac, then run
  $HERE/../.venv/bin/python $HERE/flash.py /dev/cu.usbmodemXXXX $HERE/images/fast-640.img
Undo: flash the stock firmware back with SenseCraft (https://sensecraft.seeed.cc/ai/).
EOF
