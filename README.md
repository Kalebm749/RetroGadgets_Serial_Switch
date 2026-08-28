# Retro Gadgets — Serial Network Switch

A simulated Layer 2 network switch built inside [Retro Gadgets](https://store.steampowered.com/app/1730260/Retro_Gadgets/), using real serial ports to communicate with external "PC" devices running on your computer.

The switch implements real networking concepts: MAC address learning, ARP-like address resolution, heartbeat-based presence detection, and direct forwarding — all visible on the in-game screen and debug console.

![Steam Workshop Badge](https://img.shields.io/badge/Steam_Workshop-Coming_Soon-blue?logo=steam)

## 🎮 Steam Workshop

> https://steamcommunity.com/sharedfiles/filedetails/?id=3790769850

---

## 📋 Requirements

### Software
- [Retro Gadgets](https://store.steampowered.com/app/1730260/Retro_Gadgets/) (Steam)
- [Python 3.10+](https://www.python.org/downloads/)
- [pyserial](https://pypi.org/project/pyserial/) (`pip install pyserial`)
- A virtual COM port driver:
  - **Windows:** [com0com](https://com0com.sourceforge.net/) (use a signed build for Windows 10/11)
  - **macOS/Linux:** [socat](http://www.dest-unreach.org/socat/)

### In-Game Gadget Components
- 4× Serial modules (`Serial0` – `Serial3`)
- 4× LED modules (`Led0` – `Led3`)
- 1+ Screen modules (wire multiple to the same VideoChip for more display space)
- 1× VideoChip (`VideoChip0`)
- 1× ROM chip (for the built-in `StandardFont`)
- 1× CPU

---

## 🔧 Installation & Setup

### 1. Install Virtual COM Ports

You need paired virtual serial ports so the game and Python can talk to each other. Each pair acts like a null-modem cable connecting two endpoints.

<details>
<summary><strong>Windows (com0com)</strong></summary>

1. Download a **signed** build of com0com (search for "com0com signed driver" — community builds on GitHub are recommended for Windows 10/11).
2. Open the com0com Setup GUI.
3. Click **Add Pair** four times to create 4 pairs. For each pair, set the port numbers — for example:

   | Game Side | Python Side |
   |-----------|-------------|
   | COM21     | COM31       |
   | COM22     | COM32       |
   | COM23     | COM33       |
   | COM24     | COM34       |

4. Click **Apply** after each pair. Confirm all 8 ports appear in Device Manager under "Ports (COM & LPT)."

> **Note:** Avoid low COM numbers (1–10) — they're often reserved by existing hardware or Bluetooth drivers.

</details>

<details>
<summary><strong>macOS / Linux (socat)</strong></summary>

Run one of these per port pair (4 total):

```bash
socat -d -d pty,raw,echo=0 pty,raw,echo=0
```

This prints two pseudo-tty paths (e.g., `/dev/pts/3` and `/dev/pts/4`). Use one as the game's serial port and the other for the Python script.

</details>

### 2. Configure the Lua Script

Open `SerialNetworkSwitch.lua` and edit the `COM_PORTS` table to match the **game side** of your virtual port pairs:

```lua
local COM_PORTS = { 21, 22, 23, 24 }  -- match your com0com "game side" numbers
```

### 3. Wire the Gadget (In-Game Multitool)

This must be done by hand in the Multitool — it cannot be set in code:

1. Wire `Serial0` → CPU event channel **1**
2. Wire `Serial1` → CPU event channel **2**
3. Wire `Serial2` → CPU event channel **3**
4. Wire `Serial3` → CPU event channel **4**

Also ensure each Screen module is wired to `VideoChip0`.

### 4. Install Python Dependencies

```bash
pip install pyserial
```

> ⚠️ Make sure you install `pyserial`, **not** the unrelated `serial` package. If in doubt: `pip uninstall serial && pip install pyserial`

---

## 🚀 Usage

### Start the GUI Simulator (Recommended)

The GUI gives you one window with a tab per device — no need to open multiple terminals.

```bash
# Interactive setup dialog:
python device_sim_gui.py

# Or skip the dialog with CLI args (ID:PORT or ID:PORT:BAUD):
python device_sim_gui.py PC1:COM31 PC2:COM32 PC3:COM33 PC4:COM34
```

### Start the CLI Simulator

If you prefer separate terminal windows:

```bash
python device_sim.py --port COM31 --id PC1
python device_sim.py --port COM32 --id PC2
python device_sim.py --port COM33 --id PC3
python device_sim.py --port COM34 --id PC4
```

### Sending Messages

In the input field (GUI) or terminal prompt (CLI), type:

```
DEST message here
```

Examples:
```
PC2 hello there          → sends to PC2
ALL broadcast message    → sends to all devices
```

---

## 📡 Protocol

All communication uses plain text lines terminated with `\n`.

| Frame | Direction | Purpose |
|-------|-----------|---------|
| `HELLO:<id>` | Device → Switch | Heartbeat (sent every 2s), announces device presence |
| `ARP:WHO-HAS:<id>` | Switch → Device | Address resolution request |
| `ARP:IS-AT:<id>` | Device → Switch | Address resolution reply |
| `DATA:<src>:<dst>:<msg>` | Both | Payload frame |

### How the Switch Works

1. **Learning:** When a device sends any frame, the switch learns its ID → port mapping.
2. **Heartbeat:** Devices send `HELLO` every 2 seconds. If no heartbeat is received for ~5 seconds, the device is marked offline and removed from the MAC table.
3. **Forwarding:** If the destination is in the MAC table, the frame is forwarded directly to that port.
4. **ARP Resolution:** If the destination is unknown, the switch sends `ARP:WHO-HAS:<dst>` to all alive ports. The target device replies, the switch learns the mapping, and delivers the buffered frame.
5. **Timeout:** If no ARP reply arrives within ~1 second, the buffered frame is dropped.
6. **Broadcast:** `ALL` as destination sends to every port except the sender.

---

## 🖥️ In-Game Display

The switch screen shows:

- **Port Status** — per-port indicator:
  - 🟢 `ONLINE` — device heartbeat active
  - 🟡 `LINK` — COM port connected but no heartbeat
  - 🔴 `DOWN` — COM port not active
- **MAC Table** — currently learned device-to-port mappings
- **ARP Pending** — any outstanding address resolution requests
- **Traffic Log** — recent switching decisions (forward, ARP, broadcast, timeout)

### Debug Console

All switch logic is logged to the in-game console (viewable in the Multitool debug panel). Lines are prefixed with `[SWITCH]` and include:
- Every received line and its source port
- ARP requests/replies
- Forwarding decisions
- Heartbeat timeouts

---

## 📁 Files

| File | Description |
|------|-------------|
| `SerialNetworkSwitch.lua` | In-game Lua script — paste into the gadget's Code asset |
| `device_sim_gui.py` | Python GUI simulator — tabbed window, one tab per device |
| `device_sim.py` | Python CLI simulator — one instance per terminal per device |

---

## 🐛 Troubleshooting

| Problem | Solution |
|---------|----------|
| GUI window doesn't appear | Make sure you have `pyserial` installed (not `serial`). Try: `pip uninstall serial && pip install pyserial` |
| "Port already in use" in com0com | Use higher COM numbers (20+). Check Device Manager → View → Show Hidden Devices for phantom ports |
| LINK shows up but not ONLINE | The Python simulator isn't running on the paired port, or the heartbeat isn't reaching the game. Verify your com0com pairs match |
| Always flooding, never forwarding | Check the MAC TABLE section on screen. Both devices need to have sent at least one message. Check debug console for `[SWITCH]` logs |
| Script error on power-on | Verify all 4 Serial modules, 4 LEDs, Screen, VideoChip, and ROM are present. Check component names in Multitool match `Serial0`–`Serial3`, `Led0`–`Led3`, etc. |
| ARP timeouts (frames dropped) | Target device may not be running or not responding to `ARP:WHO-HAS`. Check the Python terminal/GUI for ARP activity |

---

## 📝 License

MIT — do whatever you want with it.
