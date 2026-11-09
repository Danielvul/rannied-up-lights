# Runbook: connect, change, deploy (no internet needed)

Every command here runs from this folder. Open a terminal and start with:

```bash
cd ~/Documents/Code/OpenCorn/rannied-up-lights/training
```

## What needs internet

| Job | Without internet? |
|---|---|
| Live camera, recording, Review page | Works |
| Change and flash the ESP code | Works |
| Train a dot model at the default size, flash it to the V2 | Works |
| Rebuild the V2 firmware | Works, as long as `v2_firmware/work/` stays in place |
| AI labelling (SayGM) | **Needs internet.** Clips still save; label them later (see Recording) |
| Installing anything new (Python packages, Arduino libraries) | **Needs internet** |
| `git push`, opening a PR | **Needs internet.** `git commit` works |

## Which board is plugged in

```bash
ls /dev/cu.usbmodem*
```

| Port | Board |
|---|---|
| `/dev/cu.usbmodem5B421370831` | V2 (AI camera board), its own USB-C |
| `/dev/cu.usbmodem1101` | ESP, its own USB-C (the number can change with the USB port) |

`no matches found` means nothing is plugged in, or the cable only carries power.

**Power:** use one supply at a time. Unplug the power bank before plugging the ESP's USB-C into the Mac, and the other way round. The V2's USB doesn't power the ESP. The Mac's USB is the weakest supply, so run the real thing from the power bank.

## Connect to the camera

The viewer page is always <http://localhost:8765>. Ctrl+C stops the viewer. Only one program can use the camera at a time.

### Over USB (simplest, needs no network)

Plug in the V2's USB-C. Keep the ESP off.

```bash
.venv/bin/python viewer.py --port /dev/cu.usbmodem5B421370831 --model-name OpenGlass --target "glass of water"
```

### Over WiFi, on a router

The Mac and ESP must be on the same WiFi. A router with no internet is fine.

```bash
.venv/bin/python viewer.py --port socket://boardcam.local:3333 --model-name OpenGlass --target "glass of water"
```

If `boardcam.local` isn't found, use the IP address, last seen as `192.168.1.105`. `tools/esp_log.py` prints the current one.

### Over WiFi, with no router

If the ESP can't find the WiFi named in `xiao_bridge/secrets.h` within 15 seconds, it starts its own network called **BoardCam**. The password is `AP_PASSWORD` in that file. Join BoardCam from the Mac, then:

```bash
.venv/bin/python viewer.py --port socket://192.168.4.1:3333 --model-name OpenGlass --target "glass of water"
```

### If the viewer says "No route to host"

macOS is blocking Python from the local network, even if `ping` works.

1. Go to **System Settings → Privacy & Security → Local Network**. Turn on Python and the app you're running the terminal in. If they're already on, turn them off and on again.
2. Restart the viewer.

If that doesn't help, use the tunnel. It goes through `nc`, which macOS may still allow. Run it in a second terminal:

```bash
python3 tools/esp_tunnel.py 192.168.1.105
```

Then start the viewer through it:

```bash
.venv/bin/python viewer.py --port socket://127.0.0.1:3334 --model-name OpenGlass --target "glass of water"
```

The tunnel didn't work from every terminal when tested. If it connects and drops over and over, try Terminal.app, or use USB.

## Check the boards

Is the ESP on the network?

```bash
ping -c 3 boardcam.local
```

Is the V2 answering through the ESP? Stop the viewer first, because a new connection replaces the old one:

```bash
python3 tools/v2_check.py
```

Read the ESP's own log, with its USB-C in the Mac. This restarts the ESP:

```bash
.venv/bin/python tools/esp_log.py
```

| Log line | Meaning |
|---|---|
| `Wiring check: ... looks right` | The V2 is powered and its output reaches the ESP on D7 |
| `no signal on either pin` | The V2 isn't powered or booted, or the D7 link is broken |
| `bytes to V2` climbing, `from V2: 0` | Commands went out and nothing came back: suspect D6 |
| `from V2` climbing | The link works |
| `V2 found: ring white` / `V2 lost: ring red` | The ring changed colour |

## Change and flash the ESP

The code is `xiao_bridge/xiao_bridge.ino`. WiFi settings are in `xiao_bridge/secrets.h`, which git ignores.

1. Edit the code.
2. Check it compiles:
   ```bash
   arduino-cli compile -b esp32:esp32:XIAO_ESP32C3 xiao_bridge
   ```
3. Unplug the power bank and plug the ESP's USB-C into the Mac.
4. Flash it, using the port from `ls /dev/cu.usbmodem*`:
   ```bash
   arduino-cli compile --upload -b esp32:esp32:XIAO_ESP32C3 -p /dev/cu.usbmodem1101 xiao_bridge
   ```
5. Check it started:
   ```bash
   .venv/bin/python tools/esp_log.py
   ```

### Main LED strip

The code is `xiao_bridge/strip.ino`, ported from `leds.ino`. It plays the red comet until a hit, then the full win sequence of about 9.5 seconds. Change `LED_COUNT`, `BRIGHTNESS_ATTRACT` and `BRIGHTNESS_WIN` at the top of that file. Power the strip from the power board, not through the ESP, and share its ground.

### Speaker

The code is `xiao_bridge/sound.ino`, ported from `leds.ino`. A small synth plays slot machine bells and coins through the ESP's I2S hardware, in its own task. Each win scene plays its part of the jackpot once, and the ESP beeps once when it finishes starting up. The sounds are the `JACKPOT_` lists at the top of that file: each line is `{note, milliseconds until the next step}`, `COIN` is a clink, and every list ends with `{0, 0}`. Raise `VOLUME` for more loudness, or `BELL_RING` for longer ringing. The speaker is a Jaycar XC3744: S goes to D1, plus power and ground. Turn its trimpot to set the volume. It's rated for 5 V. If the ESP restarts when a win starts on 3.3 V, move its power to 5 V.

### LED ring settings

These are at the top of `xiao_bridge.ino`:

| Setting | Now | Does |
|---|---|---|
| `RING_PIN` | `D0` | Ring data wire |
| `RING_LEDS` | `24` | Number of LEDs |
| `RING_BRIGHTNESS` | `76` | Steady white, out of 255 (about 30%) |
| `V2_ASK_MS` | `1500` | Quiet for this long (ms): the ESP asks if the V2 is there |
| `V2_LOST_MS` | `5000` | No reply for this long (ms): the ring turns red |
| `HIT_SCORE` | `70` | How sure the V2 must be of a dot, out of 100. Also set in `viewer.py` |
| `HIT_FRAMES` | `1` | Frames in a row with a dot before it counts as a hit. Also set in `viewer.py`. Raise this if it fires with no bag |
| `HIT_COOLDOWN_MS` | `3000` | After a hit, ignore dots for this long (ms) |

**Hits:** with no laptop connected, the ESP starts the model on the V2 at full speed and watches its results. A hit plays the strip's win sequence, and the ring matches each scene with whole-ring colours and flashes (`ringScene` in `strip.ino`), then goes back to its status colour. The ESP's log shows `HIT` and `Game mode: V2 running the model`. With the viewer connected, press **Game mode** above the feed: the model runs at full speed and the page counts hits, but there's no live picture and Record is off. The viewer starts in Game mode: press the button to turn it off for the live picture and recording. The boards go straight into game mode whenever no laptop is connected.

**The ring is the status light:**

| Ring | Meaning |
|---|---|
| Red | The ESP can't reach the V2. It's red from start-up until the V2 first answers |
| White | The V2 is answering. This is also the night light |

The ring draws up to about 1.4 A at full white, so power it from the power bank or terminal block, not through the ESP.

## Rebuild and flash the V2 firmware

You only need this to change the camera firmware. Models are separate (next section). The ready-made image is `v2_firmware/images/fast-640.img`.

To rebuild after changing a patch:

```bash
v2_firmware/build.sh
```

Don't delete `v2_firmware/work/`: the build would then need internet to download it all again.

To flash, stop the viewer, unplug the ESP's power, and plug in the V2's USB-C. The board resets itself:

```bash
.venv/bin/python v2_firmware/flash.py /dev/cu.usbmodem5B421370831 v2_firmware/images/fast-640.img
```

Use `fast-640.img`. `fast-640-spi.img` is experimental and can stop the ESP booting. If flashing goes badly, wait a few seconds and run the same command again.

## Recording and labels

1. Start the viewer, with `--model-name` and `--target` set for the run:
   ```bash
   .venv/bin/python viewer.py --port socket://boardcam.local:3333 --model-name opencorn-night --target "cornhole bean bag"
   ```
   Use `opencorn-day` for daylight. The labellers' camera description is `CAMERA_NOTES` in `autobox.py`.

   **The white paper matters.** On `opencorn` runs, the AIs are told only bags in the paper count. That rule is in the prompt, so it's only as good as they follow it. Check clips in Review.
2. Press **R** on the live page. It records the next second and shows you the frames.
3. Press **S** to send them to the AIs, or **D** to discard them. Nothing is saved until you send, and pressing **R** again drops the clip and records a new one. After you send, the AIs label it in the background, so you can record straight away. The clip appears in Review when its labels are done. Keep the viewer running until then.
4. Check labels at <http://localhost:8765/review>.

Clips save to `dataset/<model>-cycle-NN/<timestamp>/`.

**With no internet,** the frames still save, but the labels will fail. When you're back online, label a clip with:

```bash
.venv/bin/python tools/relabel_clip.py dataset/OpenGlass-cycle-01/20261104-104406 --force
```

Swap in your clip's folder name. `--force` replaces the failed `boxes.json`.

## Train a model and flash it to the V2

Train from your reviewed clips. You need at least a few clips, including some with nothing in them. Whole clips are held back for testing.

```bash
.venv-train/bin/python train/train_fomo.py --model-name OpenGlass
```

It holds back the newest clips as its test. Add `--test 001858,002116` (the end of each clip's name) to choose them yourself, and keep a mix of drops, hands and empty clips. It prints the flash command when it finishes, and the model lands in `train/models/`. To train one model on day and night together, use `--model-name opencorn-night,opencorn-day`. Stay on the default `--imgsz 160` offline: other sizes need a download.

To flash it, stop the viewer, unplug the ESP's power, and plug in the V2's USB-C:

```bash
.venv/bin/python v2_firmware/flash.py /dev/cu.usbmodem5B421370831 --model train/models/NAME_vela.tflite
```

Swap `NAME` for the real file name. Firmware and model are separate, so flashing one leaves the other alone.

To see it working, tick **Model boxes (on-board)** on the live page. The viewer always starts with this off. Detection runs at about 45 fps when no photos are being sent, and at the photo rate (about 20 fps) on the live page.

## Saving your work

`git commit` works offline. Push when you're back online.

```bash
git status
```

```bash
git add -A && git commit -m "Say what changed"
```

```bash
git push
```

The branch is `training-camera-pipeline`, and the PR is <https://github.com/Danielvul/rannied-up-lights/pull/1>. `xiao_bridge/secrets.h` is ignored by git; keep it that way.

## Pins (ESP stacked on the V2)

| ESP pin | Job |
|---|---|
| 5V, GND | Power |
| D6 | ESP → V2 RXD (commands) |
| D7 | V2 TXD → ESP (camera data) |
| D3 | Resets the V2 at ESP start |
| D0 | LED ring data. Also joins the V2's PA0, so it can't be used with the SPI link |
| D2 | Main LED strip data (120 LEDs) |
| D1 | Speaker signal (XC3744 S). Also joins the V2's PB11, so it can't be used with the SPI link |
| D4, D5 | Free, but shared with the V2's I2C bus |

The ESP's signal pins are **3.3 V**. A 5 V Arduino needs a level shifter (Jaycar XC4486), plus a shared ground.

## Faults

| You see | Likely cause | Fix |
|---|---|---|
| Ring red, or page says "no reply from the camera" | The V2 isn't answering | Restart the power, then run `tools/v2_check.py` |
| ESP on WiFi, V2 LED on, V2 silent | The D6 link has slipped: it has dropped out several times | Power off and meter ESP D6 to V2 RXD on continuity. No beep: reseat, or solder a thin wire across |
| Picture freezes when you touch the boards | Loose joint | Press and tap to find it, then solder |
| "No route to host" | macOS Local Network block | See "If the viewer says No route to host" above |
| "camera failed to start: check the ribbon" | Camera ribbon | Reseat it, with the latch closed |
| Port "Address already in use" | Another viewer is running | Stop it with Ctrl+C. To find it: `lsof -nP -iTCP:8765 -sTCP:LISTEN` |
| Picture nearly black, about 30 fps | Too dark | Add light, or raise `RING_BRIGHTNESS` |
| Blurry, "lens may be smudged" | Lens dirty, out of focus, or something right up against it | Wipe it, then turn the lens to focus |
| Labels fail on a clip | No internet, or SayGM is down | Run `tools/relabel_clip.py` later |

**Meter (UT61E+):** black lead in COM, red in the VΩ socket. Turn the dial to Ω •))) and press SELECT until •))) shows. Keep the power off. A beep means connected, and `OL` means not.
