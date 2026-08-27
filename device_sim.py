"""
device_sim.py — simulates a single "PC" plugged into one port of the
Retro Gadgets serial network switch.

Requires: pip install pyserial

You need a virtual null-modem COM pair so this script and the game
can talk to each other without real hardware:

  Windows: install com0com (https://com0com.sourceforge.net/), which
           creates paired ports like COM3 <-> COM4. Point the in-game
           Serial module's `Port` at one end (e.g. 3) and this script
           at the other end (e.g. 4).

  macOS/Linux: use socat to create a linked pair of pseudo-ttys:
           socat -d -d pty,raw,echo=0 pty,raw,echo=0
           This prints two device paths; use one in the game's Serial
           settings (if supported) and the other with this script's
           --port argument.

Run one instance per simulated device, e.g.:
  python device_sim.py --port COM4 --id PC1
  python device_sim.py --port COM6 --id PC2
  python device_sim.py --port COM8 --id SRV

PROTOCOL: plain text lines "SRC:DST:MESSAGE\n" — matches the Lua
switch script's `SerialReceiveMode.Lines` parsing.
"""

import argparse
import sys
import threading
import time

import serial


def reader_thread(ser: serial.Serial, my_id: str):
	"""Continuously prints any line the switch forwards to us."""
	while True:
		try:
			line = ser.readline()
		except serial.SerialException:
			print("[connection closed]")
			return
		if not line:
			continue
		text = line.decode("utf-8", errors="replace").strip()
		if not text:
			continue
		print(f"\n<< {text}")
		print(f"{my_id}> ", end="", flush=True)


def main():
	parser = argparse.ArgumentParser(description="Simulate a device plugged into the in-game serial switch")
	parser.add_argument("--port", required=True, help="COM port / device path for this device, e.g. COM4 or /dev/ttys004")
	parser.add_argument("--baud", type=int, default=9600, help="Baud rate, must match the Serial module's BaudRate")
	parser.add_argument("--id", required=True, help="This device's id, e.g. PC1")
	args = parser.parse_args()

	try:
		ser = serial.Serial(args.port, args.baud, timeout=1)
	except serial.SerialException as e:
		print(f"Could not open {args.port}: {e}")
		sys.exit(1)

	print(f"[{args.id}] connected on {args.port} @ {args.baud} baud")
	print("Type messages as:  DEST MESSAGE   (use ALL to broadcast)")
	print("Ctrl+C to quit.\n")

	t = threading.Thread(target=reader_thread, args=(ser, args.id), daemon=True)
	t.start()

	try:
		while True:
			raw = input(f"{args.id}> ")
			if not raw.strip():
				continue
			parts = raw.split(" ", 1)
			if len(parts) != 2:
				print("format: DEST MESSAGE")
				continue
			dst, msg = parts
			frame = f"{args.id}:{dst}:{msg}\n"
			ser.write(frame.encode("utf-8"))
	except KeyboardInterrupt:
		print("\n[closing]")
	finally:
		ser.close()


if __name__ == "__main__":
	main()
