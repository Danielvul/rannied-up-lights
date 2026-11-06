# Training-data capture

Records labelled throw clips from the Grove Vision AI V2 camera so we can train the hit/miss model.

**Connecting, flashing and fixing things without internet:** see [RUNBOOK.md](RUNBOOK.md).

## Before you start (one-off)

1. Connect the OV5647 camera to the Vision AI V2 with the 15-pin ribbon (metal contacts facing the socket's contacts, latch closed).
2. Plug the V2 into the laptop with a **data** USB-C cable (some cables only carry power).
3. Check the camera in Chrome at <https://sensecraft.seeed.cc/ai/>: connect the device and you should see a live preview.
   If SenseCraft offers to flash firmware or a model, accept. That also installs the firmware this tool talks to.
4. Close the SenseCraft tab. Only one program can use the USB port at a time.

## Check the connection

```bash
cd ~/Documents/Code/OpenCorn/rannied-up-lights/training
.venv/bin/python capture.py --check
```

This prints the device info and saves one frame to `dataset/check.jpg`. Open it to see what the camera sees.

## Live viewer (setup and light check)

```bash
.venv/bin/python viewer.py
```

Then open <http://localhost:8765>. It shows the live feed with frame rate, brightness, blown-out and pure-black areas, contrast, focus and motion, plus a plain-English light check.
Overlays highlight blown areas (pink), pure black (blue) and what the motion filter sees (green). "Save snapshot" puts a frame in `dataset/snapshots/`.
Quit the viewer (Ctrl+C) before running `capture.py`: only one program can use the USB port at a time.

**Ask the AI** sends the current frame to a private vision model on SayGM and draws what it finds; **Live** keeps doing that.
**Record for training** (button or `R`) records the next second and shows the frames. Press **S** to send them to two private models (Qwen and GLM) or **D** to discard them.
Each recording is saved as training data in `dataset/<model>-cycle-NN/<timestamp>/` (frames, `meta.json`, and `boxes.json` in the same format as `autobox.py`).
Set the model name (e.g. OpenVolleyball) and cycle on the page; **New cycle** starts the next one.

## Streaming over WiFi (XIAO ESP32-C3 bridge)

The XIAO passes the V2's serial data over WiFi, so the laptop doesn't need a cable under the board. Everything else works the same.

Hardware: **stack the XIAO on the V2's XIAO socket**. The socket joins the XIAO to both of the V2's ports:

- **SPI** (default in `xiao_bridge.ino`, `LINK_SPI 1`): SCK/MISO/MOSI on D8/D9/D10, select D1, data-ready D0. Needs the OpenCorn V2 firmware (`fast-640.img`, see below), which switches the V2's SPI port on. Fast enough for 45 photos a second.
- **Serial** (`LINK_SPI 0`): D7 = V2 TX, D6 = V2 RX at 921600 baud. Works with Seeed's stock firmware, but tops out near 21 photos a second.

With SPI, D6/D7 are free for the LEDs and speaker. Power only through the XIAO's USB-C; leave the V2's USB-C unplugged.

1. Copy `xiao_bridge/secrets.example.h` to `xiao_bridge/secrets.h` (already done once) and put your 2.4 GHz WiFi name and password in it.
   Leave the name empty to use the XIAO's own "BoardCam" network instead (its password is in the same file).
2. Plug the XIAO into the laptop and flash it:
   ```bash
   arduino-cli compile --upload -b esp32:esp32:XIAO_ESP32C3 -p /dev/cu.usbmodemXXXX xiao_bridge
   ```
3. Run the viewer or capture over WiFi:
   ```bash
   .venv/bin/python viewer.py --port socket://boardcam.local:3333
   .venv/bin/python capture.py --session garage-night --port socket://boardcam.local:3333
   ```
   On the XIAO's own network, use `socket://192.168.4.1:3333`.

## Faster camera firmware (45 fps)

Seeed's stock V2 firmware runs the camera at 15 fps, too slow to catch a fast bag falling through the hole. `v2_firmware/` rebuilds Seeed's own firmware (SSCMA 2025.01.02, the version the V2 ships with) with the camera timing fixed:

| Image | Camera mode | On-board detection |
|---|---|---|
| stock | 1296×972, frame height left at the sensor default | 15.4 fps |
| `fast-1296.img` | 1296×972, shorter lines and frames | ~35 fps |
| `fast-640.img` | 640×480 (same field of view, half the rows to read) | 45 fps |

At 45 fps the 21 ms model is the limit, so a smaller model is the next step. Photos need more bandwidth than detection results: 45 photos a second is about 176 KB/s, while the V2's serial ports (USB included) carry about 90 KB/s, roughly 21 photos a second. `fast-640.img` therefore also switches on the V2's SPI port for the stacked XIAO (`grove-spi.patch`). The viewer's **Test on-board speed** button measures detection, which sends only results.

1. Build both images (downloads Arm's compiler and Seeed's source into `v2_firmware/work`, about 1 GB):
   ```bash
   v2_firmware/build.sh
   ```
2. Plug the V2's own USB-C into the Mac and flash (the board resets itself into the bootloader):
   ```bash
   .venv/bin/python v2_firmware/flash.py /dev/cu.usbmodemXXXX v2_firmware/images/fast-640.img
   ```
   Only the firmware is replaced; the model stored on the V2 stays. To undo, flash the stock firmware with [SenseCraft](https://sensecraft.seeed.cc/ai/).

The camera timing can also be tuned live, without rebuilding: `AT+SETREG=<register>,<value>` takes hex, e.g. `AT+SETREG=380f,bc` sets the low byte of the frame height. A camera restart (`AT+SENSOR`) puts the firmware's values back.

## Record a session

Name each session after where you are and what the light is like, e.g. `garage-night`, `lawn-sunny`, `lounge-lamps`:

```bash
.venv/bin/python capture.py --session garage-night
```

A preview window opens. Throw, then within a second or two press:

| Key | Label | When |
|---|---|---|
| `h` | hit | bag went through the hole |
| `u` | under | bag went under the board |
| `b` | bounce | bag hit the board and stayed out |
| `n` | nothing | no throw; people, hands, feet or dogs in view |
| `z` | undo | remove the last saved clip |
| `q` | quit | |

Each key press saves the **last 3 seconds** (`--seconds` to change). Clips go to `dataset/<session>/<label>/`.

Rough targets for the first training round: **150+ hits, 150+ unders, 100+ bounces, 150+ nothing** clips, spread over 3–4 different places or lighting conditions.

## Review the labels

Open http://localhost:8765/review (or **Review recordings →** on the live page). It lists every recording in every cycle with how many frames you've checked, and shows each frame with the AI's boxes: green is the label used for training, dashed orange and blue are the first AI's (DeepSeek, or Mistral on older clips) and GLM's guesses.

| You see | Do |
|---|---|
| Box is right | **Enter** (marks it checked, next frame) |
| Box wrong or missing | drag a new box, which replaces any boxes there; click a box and press **Delete** to remove it |
| Box but nothing there | **N** (kept as a "nothing here" example) |
| Frame is useless | **X** (left out of training) |
| The models disagreed | **1** for the first AI's boxes (DeepSeek, or Mistral on older clips), **2** for GLM's |

**Tab** jumps to the next frame the AI wasn't sure about; **←/→** step through; **U** drops your label. Your labels go in `review.json` next to the recording's `boxes.json` and always win; the AI's labels are never changed (`labels.py` has the rules). Every frame you check is also known-correct, so the checked frames double as the test set for the trained model.

## Options

- `--res 1`: 480×480 instead of 240×240 (sharper, but fewer frames per second)
- `--port /dev/cu.xxx`: choose the port if more than one USB device is plugged in
- `--baud`: serial speed (default 921600; USB usually ignores it)

## Auto-boxing (after capture)

`autobox.py` draws a box around the bean bag in every frame where something moved, using a **confidential** (TEE-hosted, private) vision model on SayGM. Frames without motion are marked "no bag" without being sent anywhere.

One-off: store your SayGM key in the macOS Keychain. You'll be asked to paste it, and it won't be echoed:

```bash
security add-generic-password -a "$USER" -s saygm-api-key -w
```

Then:

```bash
.venv/bin/python autobox.py --compare      # same 30 frames through 6 private models -> dataset/compare.html
.venv/bin/python autobox.py --limit 50     # test run with the default model (deepseek-v4.1-flash-tee)
.venv/bin/python autobox.py --review       # dataset/review.html: check the boxes
.venv/bin/python autobox.py                # box everything not yet boxed
.venv/bin/python autobox.py --watch        # keep boxing new clips while capture.py runs
```

Use `--model <name>` to switch to the model that wins the comparison. Each clip's boxes are saved as `boxes.json` next to its frames. Delete that file to redo a clip.

## Training (next step)

Training will run locally: YOLOv8 on the laptop, then Arm's Vela compiler, then flashed to the V2 over USB. No accounts and no cloud.
That script comes next, once there's real footage to train on.
