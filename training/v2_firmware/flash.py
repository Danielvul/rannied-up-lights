"""Flash a firmware image to the Grove Vision AI V2 over its own USB port (XMODEM, CRC mode).

Does what Himax's xmodem_send.py does, using only pyserial:
  1. reset the board (or you press its reset button) and answer the bootloader menu with "1"
  2. send the image with XMODEM-CRC, 128-byte packets
  3. answer "y" to reboot into the new firmware

    .venv/bin/python v2_firmware/flash.py /dev/cu.usbmodemXXXX work/.../output.img
    .venv/bin/python v2_firmware/flash.py /dev/cu.usbmodemXXXX --model train/models/x_vela.tflite

A firmware image only replaces the firmware; --model writes a model to the model area (0x400000,
where SSCMA looks for model 1), which is what the model-only form changes.
"""
import argparse
import sys
import time

import serial

SOH, EOT, ACK, NAK, CAN, CRC = 0x01, 0x04, 0x06, 0x15, 0x18, ord("C")
CHUNK = 0xFF000  # bytes per transfer: under the bootloader's 1 MB limit, a whole number of 4 KB flash sectors


def crc16(data):
    crc = 0
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else crc << 1
            crc &= 0xFFFF
    return crc


def wait_for(ser, text, poke=None, timeout=60):
    """Read lines until one contains text, sending poke after every line/timeout."""
    end = time.time() + timeout
    while time.time() < end:
        line = ser.readline().decode(errors="replace").strip()
        if line:
            print("  V2:", line)
        if text in line:
            return True
        if poke:
            ser.write(poke)
    return False


def xmodem_send(ser, image):
    # The receiver asks for CRC mode by sending "C".
    end = time.time() + 30
    while time.time() < end:
        c = ser.read(1)
        if c and c[0] == CRC:
            break
        if c and c[0] == NAK:
            sys.exit("receiver wants checksum mode; this flasher only does CRC")
    else:
        sys.exit("no XMODEM start from the V2")
    blocks = [image[i:i + 128].ljust(128, b"\x1a") for i in range(0, len(image), 128)]
    for n, block in enumerate(blocks, 1):
        seq = n & 0xFF
        packet = bytes([SOH, seq, 0xFF - seq]) + block + crc16(block).to_bytes(2, "big")
        for attempt in range(16):
            ser.write(packet)
            r = ser.read(1)
            if r and r[0] == ACK:
                break
            if r and r[0] == CAN:
                sys.exit(f"V2 cancelled the transfer at block {n}")
        else:
            sys.exit(f"block {n} failed 16 times")
        if n % 200 == 0 or n == len(blocks):
            print(f"\r  sent {n}/{len(blocks)} blocks ({100 * n // len(blocks)}%)", end="", flush=True)
    print()
    # The bootloader may print a status line ("backup slot header") around its ACK, so scan
    # everything that arrives instead of expecting the ACK as the very next byte.
    seen = b""
    for _ in range(10):
        ser.write(bytes([EOT]))
        end = time.time() + 2
        while time.time() < end:
            seen += ser.read(256)
            if bytes([ACK]) in seen or b"reboot system" in seen:
                return seen
    sys.exit(f"V2 didn't acknowledge the end of transfer; it said: {seen[-200:]!r}")


def model_preamble(address, offset=0):
    """The 128-byte packet that tells the bootloader where the next transfer goes (xmodem_send.py's
    format): C0 5A, flash address and offset (little-endian), 5A C0, padded with FF."""
    return bytes([0xC0, 0x5A]) + address.to_bytes(4, "little") + offset.to_bytes(4, "little") \
        + bytes([0x5A, 0xC0]) + b"\xff" * (128 - 12)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("port")
    ap.add_argument("image", nargs="?", help="firmware image (output.img); leave out to flash only a model")
    ap.add_argument("--model", help="a Vela-compiled .tflite to write to the model area")
    ap.add_argument("--model-address", type=lambda x: int(x, 0), default=0x400000,
                    help="where SSCMA looks for model 1 (default 0x400000)")
    args = ap.parse_args()
    if not args.image and not args.model:
        sys.exit("Give a firmware image, --model, or both")
    image = open(args.image, "rb").read() if args.image else None
    model = open(args.model, "rb").read() if args.model else None
    if image:
        print(f"Image {args.image}: {len(image)} bytes")
    if model:
        if model[4:8] != b"TFL3":
            sys.exit(f"{args.model} isn't a TFLite model")
        print(f"Model {args.model}: {len(model)} bytes, to {args.model_address:#x}")

    ser = serial.Serial(args.port, 921600, timeout=1)
    # Try a reset through the USB adapter's control lines; if the board ignores it, press reset.
    ser.dtr = False; ser.rts = True; time.sleep(0.1); ser.rts = False
    print("Waiting for the bootloader. If nothing happens in a few seconds, press the V2's reset button.")
    if not wait_for(ser, "Send data using the xmodem protocol", poke=b"1", timeout=90):
        sys.exit("bootloader menu never appeared")
    time.sleep(1)
    ser.reset_input_buffer()
    ser.write(b"1")
    tail = b""
    if image:
        print("Sending firmware...")
        tail = xmodem_send(ser, image)
    if model:
        # After a transfer the bootloader asks "end file transmission and reboot? (y)": "n" keeps it
        # listening. A model goes as pairs of transfers: an address preamble, then up to CHUNK bytes
        # (the bootloader takes at most 1 MB per transfer), so a 2.8 MB model is three pairs.
        parts = []
        for off in range(0, len(model), CHUNK):
            parts.append((f"model address {args.model_address + off:#x}", model_preamble(args.model_address + off)))
            parts.append((f"model bytes {off}-{min(off + CHUNK, len(model))}", model[off:off + CHUNK]))
        for n, (part, data) in enumerate(parts):
            if image or n > 0:
                if b"reboot system" not in tail and not wait_for(ser, "reboot system", timeout=20):
                    sys.exit("bootloader didn't ask for the next file")
                time.sleep(1)
                ser.reset_input_buffer()
                ser.write(b"n")
            print(f"Sending {part}...")
            tail = xmodem_send(ser, data)
    print("Sent. Rebooting.")
    if b"reboot system" in tail or wait_for(ser, "reboot system", timeout=20):
        ser.write(b"y")
    wait_for(ser, "\0", timeout=5)  # show the first boot lines
    print("Done.")


if __name__ == "__main__":
    main()
