#!/usr/bin/env python3
"""Restart the ESP over USB and print its log: wiring check, IP address, bytes to and from the V2.

    .venv/bin/python tools/esp_log.py [seconds]      default 20
"""
import glob
import sys
import time

import serial

seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 20
ports = glob.glob("/dev/cu.usbmodem*")
if not ports:
    sys.exit("Nothing on USB: plug the ESP's USB-C into the Mac")
s = serial.Serial(ports[0], 115200, timeout=0.2)
s.dtr = False
s.rts = True
time.sleep(0.1)
s.rts = False
start = time.time()
line = bytearray()
while time.time() - start < seconds:
    try:
        data = s.read(4096)
    except serial.SerialException:  # the USB port drops for a moment as the ESP restarts
        time.sleep(0.5)
        try:
            s = serial.Serial(ports[0], 115200, timeout=0.2)
        except serial.SerialException:
            pass
        continue
    for byte in data:
        if byte == 10:
            text = line.decode("utf8", "replace").rstrip()
            if "Joining" not in text:  # this line names the WiFi network
                print(text, flush=True)
            line.clear()
        else:
            line.append(byte)
