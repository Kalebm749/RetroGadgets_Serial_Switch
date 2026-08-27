"""
device_sim.py — CLI version: simulates a single "PC" plugged into one port
of the Retro Gadgets serial network switch.

Supports the full switch protocol:
  - HELLO:<id>         — heartbeat sent every 2 seconds
  - ARP:WHO-HAS:<id>  — auto-replies if it's asking for us
  - ARP:IS-AT:<id>    — reply sent back to switch
  - DATA:<src>:<dst>:<msg> — payload frames

Requires: pip install pyserial

Usage:
  python device_sim.py --port COM4 --id PC1
  python device_sim.py --port COM6 --id PC2 --baud 9600
"""

import argparse
import sys
import threading
import time

import serial


HEARTBEAT_INTERVAL = 2.0  # seconds


def heartbeat_thread(ser: serial.Serial, my_id: str):
    """Sends HELLO:<id> periodically to announce presence."""
    while True:
        try:
            ser.write(f"HELLO:{my_id}\n".encode("utf-8"))
        except serial.SerialException:
            print("[heartbeat: connection lost]")
            return
        time.sleep(HEARTBEAT_INTERVAL)


def reader_thread(ser: serial.Serial, my_id: str):
    """Continuously reads lines; handles ARP and displays data."""
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

        # Handle ARP:WHO-HAS:<id>
        if text.startswith("ARP:WHO-HAS:"):
            target = text[len("ARP:WHO-HAS:"):].strip()
            if target == my_id:
                reply = f"ARP:IS-AT:{my_id}\n"
                try:
                    ser.write(reply.encode("utf-8"))
                    print(f"\n[ARP] WHO-HAS {target} -> replied IS-AT {my_id}")
                except serial.SerialException:
                    print("[ARP reply failed]")
            else:
                pass  # Not for us, ignore silently
            print(f"{my_id}> ", end="", flush=True)
            continue

        # Handle DATA:<src>:<dst>:<msg>
        if text.startswith("DATA:"):
            payload = text[5:]
            parts = payload.split(":", 2)
            if len(parts) == 3:
                src, dst, msg = parts
                print(f"\n<< [{src}] {msg}")
            else:
                print(f"\n<< (malformed) {text}")
            print(f"{my_id}> ", end="", flush=True)
            continue

        # Legacy/unknown
        print(f"\n<< {text}")
        print(f"{my_id}> ", end="", flush=True)


def main():
    parser = argparse.ArgumentParser(description="Simulate a device plugged into the in-game serial switch")
    parser.add_argument("--port", required=True, help="COM port / device path, e.g. COM4 or /dev/ttys004")
    parser.add_argument("--baud", type=int, default=9600, help="Baud rate (must match Serial module's BaudRate)")
    parser.add_argument("--id", required=True, help="This device's id, e.g. PC1")
    args = parser.parse_args()

    try:
        ser = serial.Serial(args.port, args.baud, timeout=1)
    except serial.SerialException as e:
        print(f"Could not open {args.port}: {e}")
        sys.exit(1)

    print(f"[{args.id}] connected on {args.port} @ {args.baud} baud")
    print(f"[{args.id}] sending HELLO every {HEARTBEAT_INTERVAL}s")
    print("Type messages as:  DEST MESSAGE   (use ALL to broadcast)")
    print("Ctrl+C to quit.\n")

    # Start background threads
    t_reader = threading.Thread(target=reader_thread, args=(ser, args.id), daemon=True)
    t_reader.start()

    t_heartbeat = threading.Thread(target=heartbeat_thread, args=(ser, args.id), daemon=True)
    t_heartbeat.start()

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
            frame = f"DATA:{args.id}:{dst}:{msg}\n"
            ser.write(frame.encode("utf-8"))
    except KeyboardInterrupt:
        print("\n[closing]")
    finally:
        ser.close()


if __name__ == "__main__":
    main()
