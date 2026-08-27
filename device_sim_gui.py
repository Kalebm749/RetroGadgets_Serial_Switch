"""
device_sim_gui.py — GUI multi-device simulator for the Retro Gadgets serial switch.

Shows all devices simultaneously in a square grid layout. Each cell has:
  - A header with device ID, port, connection status, and drop counter
  - A scrolling terminal output area (shows sent/received messages)
  - An input field at the bottom (type "DEST message" and hit Enter to send)

Features:
  - Drop detection: sequence numbers in DATA frames, gap detection on receive
  - Auto-test mode: generates realistic random traffic between all connected devices
  - Supports the full switch protocol (HELLO, ARP, DATA frames)

Protocol:
  - DATA frames include a sequence number: DATA:<src>:<dst>:<seq>:<msg>
  - Each device tracks per-sender sequence numbers to detect gaps

Requires: pip install pyserial

Usage:
  python device_sim_gui.py

  On launch a config dialog lets you add devices (id + COM port + baud).
  Or pass them on the command line to skip the dialog:

  python device_sim_gui.py PC1:COM13 PC2:COM14 PC3:COM15 PC4:COM16
  python device_sim_gui.py PC1:COM13:9600 PC2:COM14:9600   (explicit baud)
"""

import sys
import threading
import time
import random
import math
import tkinter as tk
from tkinter import ttk, messagebox
from datetime import datetime

import serial


HEARTBEAT_INTERVAL = 2.0  # seconds between HELLO messages


# ============================================================
# REALISTIC TRAFFIC PATTERNS
# ============================================================

TRAFFIC_PATTERNS = {
    "http_request": [
        "GET /index.html HTTP/1.1",
        "GET /api/users HTTP/1.1",
        "POST /api/login HTTP/1.1",
        "GET /images/logo.png HTTP/1.1",
        "PUT /api/settings HTTP/1.1",
        "GET /api/status HTTP/1.1",
        "DELETE /api/sessions/42 HTTP/1.1",
        "GET /favicon.ico HTTP/1.1",
    ],
    "http_response": [
        "200 OK {users:[alice,bob,charlie]}",
        "201 Created {id:42}",
        "404 Not Found",
        "500 Internal Server Error",
        "301 Moved Permanently -> /new-path",
        "200 OK {status:healthy,uptime:3600}",
        "403 Forbidden",
        "200 OK <html>...</html>",
    ],
    "dns_query": [
        "DNS QUERY A example.com",
        "DNS QUERY AAAA google.com",
        "DNS QUERY MX company.org",
        "DNS QUERY A api.service.local",
        "DNS QUERY PTR 192.168.1.1",
        "DNS QUERY CNAME cdn.example.com",
    ],
    "dns_response": [
        "DNS REPLY A 93.184.216.34",
        "DNS REPLY AAAA 2607:f8b0:4004:800::200e",
        "DNS REPLY MX mail.company.org pri=10",
        "DNS REPLY A 10.0.0.50",
        "DNS REPLY PTR router.local",
        "DNS REPLY CNAME d1234.cloudfront.net",
    ],
    "ping": [
        "ICMP ECHO seq=1 ttl=64",
        "ICMP ECHO seq=2 ttl=64",
        "ICMP ECHO seq=3 ttl=64",
        "ICMP ECHO-REPLY seq=1 time=2ms",
        "ICMP ECHO-REPLY seq=2 time=1ms",
        "ICMP ECHO-REPLY seq=3 time=3ms",
    ],
    "file_transfer": [
        "FTP STOR report_q3.xlsx [2.4MB]",
        "FTP RETR backup_20240801.tar.gz [156MB]",
        "SCP upload config.yaml -> /etc/app/",
        "FTP LIST /shared/documents/",
        "SMB READ \\\\fileserver\\docs\\meeting_notes.docx",
        "FTP STOR database_dump.sql [45MB]",
    ],
    "chat": [
        "MSG hey, are you there?",
        "MSG meeting in 5 minutes",
        "MSG can you review my PR?",
        "MSG server is back up",
        "MSG lunch?",
        "MSG pushed the fix to main",
        "MSG build is green now",
        "MSG deploying to staging",
    ],
    "database": [
        "SQL SELECT * FROM users WHERE active=1",
        "SQL INSERT INTO logs (event,ts) VALUES(...)",
        "SQL UPDATE sessions SET last_seen=NOW()",
        "SQL DELETE FROM cache WHERE expired<NOW()",
        "REDIS GET session:abc123",
        "REDIS SET ratelimit:user42 EX 60",
    ],
    "monitoring": [
        "METRIC cpu_usage=72% host=web01",
        "METRIC mem_free=1.2GB host=db01",
        "ALERT disk_usage>90% host=storage01",
        "METRIC req_per_sec=1420 svc=api",
        "HEARTBEAT service=auth status=healthy",
        "METRIC latency_p99=45ms svc=gateway",
    ],
}

TRAFFIC_SCENARIOS = [
    {"weight": 30, "sequence": ["dns_query", "dns_response", "http_request", "http_response"]},
    {"weight": 15, "sequence": ["ping", "ping", "ping"]},
    {"weight": 10, "sequence": ["file_transfer"]},
    {"weight": 20, "sequence": ["chat", "chat"]},
    {"weight": 15, "sequence": ["database", "database"]},
    {"weight": 10, "sequence": ["monitoring", "monitoring"]},
]


def pick_scenario():
    total = sum(s["weight"] for s in TRAFFIC_SCENARIOS)
    r = random.randint(1, total)
    cumulative = 0
    for scenario in TRAFFIC_SCENARIOS:
        cumulative += scenario["weight"]
        if r <= cumulative:
            return scenario
    return TRAFFIC_SCENARIOS[0]


def pick_message(pattern_type: str) -> str:
    return random.choice(TRAFFIC_PATTERNS[pattern_type])


# ============================================================
# DEVICE PANEL (one cell in the grid)
# ============================================================

class DevicePanel:
    """One simulated device — owns a serial connection and a grid cell."""

    def __init__(self, parent_frame: ttk.Frame, device_id: str, port: str, baud: int):
        self.device_id = device_id
        self.port = port
        self.baud = baud
        self.ser = None
        self.running = False

        # Sequence number tracking
        self.send_seq = 0  # outgoing sequence number
        self.recv_seqs = {}  # per-sender: last seen seq number
        self.sent_count = 0
        self.recv_count = 0
        self.dropped_count = 0
        self.send_fail_count = 0

        # --- GUI setup ---
        self.frame = ttk.LabelFrame(parent_frame, text=f" {device_id} — {port} ", padding=3)

        # Header with status and stats
        self.header_frame = ttk.Frame(self.frame)
        self.header_frame.pack(fill=tk.X)

        self.status_dot = tk.Label(self.header_frame, text="●", fg="gray",
                                    font=("Segoe UI", 10))
        self.status_dot.pack(side=tk.LEFT, padx=(0, 4))

        self.status_label = ttk.Label(self.header_frame, text="Disconnected",
                                       font=("Segoe UI", 8))
        self.status_label.pack(side=tk.LEFT)

        # Stats frame on the right side of header
        self.stats_label = ttk.Label(self.header_frame, text="TX:0 RX:0 DROP:0 FAIL:0",
                                      font=("Consolas", 8), foreground="gray")
        self.stats_label.pack(side=tk.RIGHT, padx=(8, 0))

        # Terminal output
        self.output = tk.Text(self.frame, wrap=tk.WORD, state=tk.DISABLED,
                              bg="#1e1e1e", fg="#00ff88", font=("Consolas", 9),
                              insertbackground="white", height=10)
        self.output.pack(fill=tk.BOTH, expand=True, pady=(3, 0))

        # Tags for different message types
        self.output.tag_configure("sent", foreground="#88ccff")
        self.output.tag_configure("system", foreground="#888888")
        self.output.tag_configure("error", foreground="#ff4444")
        self.output.tag_configure("arp", foreground="#cc88ff")
        self.output.tag_configure("autotest", foreground="#ffaa00")
        self.output.tag_configure("dropped", foreground="#ff0000", background="#330000")

        # Scrollbar
        scrollbar = ttk.Scrollbar(self.output, command=self.output.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.output.configure(yscrollcommand=scrollbar.set)

        # Input area
        input_frame = ttk.Frame(self.frame)
        input_frame.pack(fill=tk.X, pady=(3, 0))

        self.prompt_label = ttk.Label(input_frame, text=f"{device_id}>",
                                       font=("Consolas", 9))
        self.prompt_label.pack(side=tk.LEFT)

        self.input_entry = ttk.Entry(input_frame, font=("Consolas", 9))
        self.input_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(3, 3))
        self.input_entry.bind("<Return>", self._on_send)

        self.send_btn = ttk.Button(input_frame, text="Send", command=self._on_send,
                                    width=5)
        self.send_btn.pack(side=tk.RIGHT)

    def grid(self, row: int, col: int):
        """Place this panel in the parent grid."""
        self.frame.grid(row=row, column=col, padx=3, pady=3, sticky="nsew")

    def _update_stats_display(self):
        """Update the stats label (thread-safe)."""
        drop_color = "#ff4444" if self.dropped_count > 0 else "gray"
        fail_color = "#ff8800" if self.send_fail_count > 0 else "gray"

        def _set():
            text = f"TX:{self.sent_count} RX:{self.recv_count}"
            self.stats_label.config(text=text, foreground="gray")

            # Show drop/fail counts with color if > 0
            if self.dropped_count > 0 or self.send_fail_count > 0:
                text += f" DROP:{self.dropped_count} FAIL:{self.send_fail_count}"
                self.stats_label.config(
                    text=text,
                    foreground=drop_color if self.dropped_count > 0 else fail_color
                )

        self.stats_label.after(0, _set)

    def log(self, text: str, tag: str = ""):
        """Append a line to the terminal output (thread-safe via after())."""
        timestamp = datetime.now().strftime("%H:%M:%S")

        def _append():
            self.output.configure(state=tk.NORMAL)
            if tag:
                self.output.insert(tk.END, f"[{timestamp}] {text}\n", tag)
            else:
                self.output.insert(tk.END, f"[{timestamp}] {text}\n")
            self.output.configure(state=tk.DISABLED)
            self.output.see(tk.END)

        self.output.after(0, _append)

    def _update_status(self, text: str, color: str):
        def _set():
            self.status_dot.config(fg=color)
            self.status_label.config(text=text)
        self.status_dot.after(0, _set)

    def connect(self):
        """Open the serial port and start the reader + heartbeat threads."""
        try:
            self.ser = serial.Serial(self.port, self.baud, timeout=1)
            self.running = True
            self._update_status("Connected", "#00cc00")
            self.log(f"Connected to {self.port} @ {self.baud}", "system")

            reader = threading.Thread(target=self._reader_loop, daemon=True)
            reader.start()

            heartbeat = threading.Thread(target=self._heartbeat_loop, daemon=True)
            heartbeat.start()

            return True
        except serial.SerialException as e:
            self._update_status("Failed", "#ff0000")
            self.log(f"FAILED: {e}", "error")
            return False

    def _heartbeat_loop(self):
        while self.running:
            try:
                self.ser.write(f"HELLO:{self.device_id}\n".encode("utf-8"))
            except serial.SerialException:
                self._update_status("Lost", "#ff0000")
                self.log("[heartbeat: connection lost]", "error")
                self.running = False
                return
            time.sleep(HEARTBEAT_INTERVAL)

    def _reader_loop(self):
        while self.running:
            try:
                raw = self.ser.readline()
            except serial.SerialException:
                self._update_status("Lost", "#ff0000")
                self.log("[connection lost]", "error")
                self.running = False
                return
            if not raw:
                continue
            text = raw.decode("utf-8", errors="replace").strip()
            if not text:
                continue

            # ARP:WHO-HAS:<id>
            if text.startswith("ARP:WHO-HAS:"):
                target = text[len("ARP:WHO-HAS:"):].strip()
                if target == self.device_id:
                    try:
                        self.ser.write(f"ARP:IS-AT:{self.device_id}\n".encode("utf-8"))
                        self.log(f"ARP: {target}? That's me!", "arp")
                    except serial.SerialException:
                        self.log("[ARP reply failed]", "error")
                continue

            # DATA:<src>:<dst>:<seq>:<msg>  (new format with seq)
            # Also handles legacy DATA:<src>:<dst>:<msg> (no seq)
            if text.startswith("DATA:"):
                payload = text[5:]
                parts = payload.split(":", 3)

                if len(parts) == 4:
                    # New format: src:dst:seq:msg
                    src, dst, seq_str, msg = parts
                    try:
                        seq = int(seq_str)
                        self._check_sequence(src, seq)
                    except ValueError:
                        # seq_str wasn't a number — treat as legacy 3-part format
                        # where parts[2] is actually part of the message
                        src, dst = parts[0], parts[1]
                        msg = parts[2] + ":" + parts[3]
                    self.recv_count += 1
                    self.log(f"<< [{src}] {msg}")
                elif len(parts) == 3:
                    # Legacy format: src:dst:msg
                    src, dst, msg = parts
                    self.recv_count += 1
                    self.log(f"<< [{src}] {msg}")
                else:
                    self.log(f"<< (malformed) {text}", "error")

                self._update_stats_display()
                continue

            self.log(f"<< {text}")

    def _check_sequence(self, sender: str, seq: int):
        """Check for gaps in sequence numbers from a given sender."""
        if sender in self.recv_seqs:
            expected = self.recv_seqs[sender] + 1
            if seq > expected:
                gap = seq - expected
                self.dropped_count += gap
                self.log(f"⚠ DROPPED {gap} msg(s) from {sender} (expected seq {expected}, got {seq})", "dropped")
        self.recv_seqs[sender] = seq

    def send_data(self, dst: str, msg: str, tag: str = "sent"):
        """Send a DATA frame with sequence number."""
        if not self.ser or not self.running:
            return False
        self.send_seq += 1
        frame = f"DATA:{self.device_id}:{dst}:{self.send_seq}:{msg}\n"
        try:
            self.ser.write(frame.encode("utf-8"))
            self.sent_count += 1
            self.log(f">> [{dst}] {msg}", tag)
            self._update_stats_display()
            return True
        except serial.SerialException as e:
            self.send_fail_count += 1
            self.log(f"SEND FAILED: {e}", "error")
            self._update_stats_display()
            return False

    def _on_send(self, event=None):
        raw = self.input_entry.get().strip()
        self.input_entry.delete(0, tk.END)

        if not raw:
            return
        if not self.ser or not self.running:
            self.log("Not connected!", "error")
            return

        parts = raw.split(" ", 1)
        if len(parts) != 2:
            self.log("Format: DEST message", "error")
            return

        dst, msg = parts
        self.send_data(dst, msg)

    def disconnect(self):
        self.running = False
        if self.ser:
            try:
                self.ser.close()
            except Exception:
                pass


# ============================================================
# AUTO-TEST ENGINE
# ============================================================

class AutoTestEngine:
    """Generates realistic random traffic across all connected devices."""

    def __init__(self, panels: list):
        self.panels = panels
        self.running = False
        self._thread = None
        self.rate = 2.0
        self.burst_chance = 0.15

    def start(self, rate: float = 2.0):
        if self.running:
            return
        self.rate = rate
        self.running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self.running = False

    def _get_alive(self):
        return [p for p in self.panels if p.running and p.ser]

    def _run(self):
        while self.running:
            alive = self._get_alive()
            if len(alive) < 2:
                time.sleep(0.5)
                continue

            scenario = pick_scenario()
            sender = random.choice(alive)
            others = [p for p in alive if p is not sender]
            receiver = random.choice(others)
            is_broadcast = random.random() < 0.10

            for pattern_type in scenario["sequence"]:
                if not self.running:
                    break

                msg = pick_message(pattern_type)
                dst = "ALL" if is_broadcast else receiver.device_id
                sender.send_data(dst, msg, "autotest")

                time.sleep(random.uniform(0.1, 0.4))

                if "response" in pattern_type or "REPLY" in pattern_type:
                    sender, receiver = receiver, sender
                    if receiver not in alive:
                        receiver = random.choice([p for p in alive if p is not sender])

            # Burst mode
            if random.random() < self.burst_chance:
                burst_count = random.randint(3, 8)
                for _ in range(burst_count):
                    if not self.running:
                        break
                    bs = random.choice(alive)
                    bo = [p for p in alive if p is not bs]
                    br = random.choice(bo)
                    bm = pick_message(random.choice(list(TRAFFIC_PATTERNS.keys())))
                    bs.send_data(br.device_id, bm, "autotest")
                    time.sleep(random.uniform(0.05, 0.15))

            base_delay = 1.0 / self.rate
            jitter = random.uniform(-base_delay * 0.3, base_delay * 0.3)
            time.sleep(max(0.1, base_delay + jitter))


# ============================================================
# MAIN APP
# ============================================================

class App:
    """
    Single-window application with a square grid of device panels.
    """

    def __init__(self, devices: list = None):
        self.root = tk.Tk()
        self.root.title("Retro Gadgets — Serial Switch Device Simulator")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.panels: list[DevicePanel] = []
        self.devices = devices
        self.auto_test: AutoTestEngine = None

        if self.devices:
            self._build_device_view()
        else:
            self._build_config_view()

    def _build_config_view(self):
        self.root.geometry("420x350")
        self.root.resizable(False, False)

        self.config_frame = ttk.Frame(self.root)
        self.config_frame.pack(fill=tk.BOTH, expand=True)

        self._config_devices = []

        ttk.Label(self.config_frame, text="Add simulated devices (one per switch port):",
                  font=("Segoe UI", 10)).pack(pady=(10, 5))

        list_frame = ttk.Frame(self.config_frame)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=10)

        self.device_listbox = tk.Listbox(list_frame, font=("Consolas", 10), height=8)
        self.device_listbox.pack(fill=tk.BOTH, expand=True)

        add_frame = ttk.Frame(self.config_frame)
        add_frame.pack(fill=tk.X, padx=10, pady=5)

        ttk.Label(add_frame, text="ID:").grid(row=0, column=0, padx=2)
        self.id_entry = ttk.Entry(add_frame, width=8)
        self.id_entry.grid(row=0, column=1, padx=2)
        self.id_entry.insert(0, "PC1")

        ttk.Label(add_frame, text="Port:").grid(row=0, column=2, padx=2)
        self.port_entry = ttk.Entry(add_frame, width=10)
        self.port_entry.grid(row=0, column=3, padx=2)
        self.port_entry.insert(0, "COM13")

        ttk.Label(add_frame, text="Baud:").grid(row=0, column=4, padx=2)
        self.baud_entry = ttk.Entry(add_frame, width=7)
        self.baud_entry.grid(row=0, column=5, padx=2)
        self.baud_entry.insert(0, "9600")

        ttk.Button(add_frame, text="Add", command=self._add_device).grid(row=0, column=6, padx=(8, 0))

        btn_frame = ttk.Frame(self.config_frame)
        btn_frame.pack(fill=tk.X, padx=10, pady=(0, 10))

        ttk.Button(btn_frame, text="Remove Selected", command=self._remove_device).pack(side=tk.LEFT)
        ttk.Button(btn_frame, text="Start", command=self._start_devices).pack(side=tk.RIGHT)

        self.id_entry.focus_set()
        self.root.bind("<Return>", lambda e: self._add_device())

    def _add_device(self):
        device_id = self.id_entry.get().strip()
        port = self.port_entry.get().strip()
        baud_str = self.baud_entry.get().strip()

        if not device_id or not port:
            messagebox.showwarning("Missing info", "ID and Port are required.")
            return
        try:
            baud = int(baud_str)
        except ValueError:
            messagebox.showwarning("Invalid baud", "Baud rate must be a number.")
            return

        self._config_devices.append((device_id, port, baud))
        self.device_listbox.insert(tk.END, f"{device_id}  |  {port}  |  {baud} baud")

        self.id_entry.delete(0, tk.END)
        self.id_entry.insert(0, f"PC{len(self._config_devices) + 1}")
        self.port_entry.delete(0, tk.END)
        self.port_entry.focus_set()

    def _remove_device(self):
        sel = self.device_listbox.curselection()
        if sel:
            idx = sel[0]
            self.device_listbox.delete(idx)
            self._config_devices.pop(idx)

    def _start_devices(self):
        if not self._config_devices:
            messagebox.showwarning("No devices", "Add at least one device.")
            return
        self.devices = self._config_devices
        self.root.unbind("<Return>")
        self.config_frame.destroy()
        self._build_device_view()

    def _build_device_view(self):
        """Build the grid layout with all device panels visible at once."""
        n = len(self.devices)
        cols = math.ceil(math.sqrt(n))
        rows = math.ceil(n / cols)

        win_w = max(750, cols * 400)
        win_h = max(550, rows * 340)
        self.root.geometry(f"{win_w}x{win_h}")
        self.root.resizable(True, True)
        self.root.minsize(500, 350)

        # Toolbar at top
        toolbar = ttk.Frame(self.root)
        toolbar.pack(fill=tk.X, padx=4, pady=(4, 0))

        ttk.Label(toolbar, text="Auto-Test:", font=("Segoe UI", 9, "bold")).pack(side=tk.LEFT, padx=(4, 8))

        self.autotest_btn = ttk.Button(toolbar, text="▶ Start", command=self._toggle_autotest)
        self.autotest_btn.pack(side=tk.LEFT, padx=2)

        ttk.Label(toolbar, text="Rate:").pack(side=tk.LEFT, padx=(12, 4))
        self.rate_var = tk.StringVar(value="2.0")
        self.rate_spinbox = ttk.Spinbox(toolbar, from_=0.5, to=20.0, increment=0.5,
                                         textvariable=self.rate_var, width=5,
                                         font=("Consolas", 9))
        self.rate_spinbox.pack(side=tk.LEFT, padx=2)
        ttk.Label(toolbar, text="msg/s", font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=(0, 8))

        self.status_label = ttk.Label(toolbar, text="● Idle", foreground="gray",
                                       font=("Segoe UI", 9))
        self.status_label.pack(side=tk.RIGHT, padx=8)

        # Grid container
        grid_frame = ttk.Frame(self.root)
        grid_frame.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        for r in range(rows):
            grid_frame.rowconfigure(r, weight=1)
        for c in range(cols):
            grid_frame.columnconfigure(c, weight=1)

        for idx, (device_id, port, baud) in enumerate(self.devices):
            row = idx // cols
            col = idx % cols
            panel = DevicePanel(grid_frame, device_id, port, baud)
            panel.grid(row, col)
            self.panels.append(panel)

        self.auto_test = AutoTestEngine(self.panels)
        self.root.after(100, self._connect_all)

    def _connect_all(self):
        for panel in self.panels:
            panel.connect()

    def _toggle_autotest(self):
        if self.auto_test and self.auto_test.running:
            self.auto_test.stop()
            self.autotest_btn.config(text="▶ Start")
            self.status_label.config(text="● Idle", foreground="gray")
            self.rate_spinbox.config(state="normal")
        else:
            try:
                rate = float(self.rate_var.get())
            except ValueError:
                rate = 2.0
            rate = max(0.5, min(20.0, rate))

            self.auto_test.start(rate)
            self.autotest_btn.config(text="⏹ Stop")
            self.status_label.config(text=f"● Running ({rate:.1f} msg/s)", foreground="#ff8800")
            self.rate_spinbox.config(state="disabled")

    def _on_close(self):
        if self.auto_test:
            self.auto_test.stop()
        for panel in self.panels:
            panel.disconnect()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def parse_cli_devices(args: list[str]) -> list[tuple]:
    """Parse 'ID:PORT' or 'ID:PORT:BAUD' from command line args."""
    devices = []
    for arg in args:
        parts = arg.split(":")
        if len(parts) == 2:
            devices.append((parts[0], parts[1], 9600))
        elif len(parts) == 3:
            devices.append((parts[0], parts[1], int(parts[2])))
        else:
            print(f"Invalid device spec: {arg} (expected ID:PORT or ID:PORT:BAUD)")
            sys.exit(1)
    return devices


def main():
    if len(sys.argv) > 1:
        devices = parse_cli_devices(sys.argv[1:])
    else:
        devices = None

    app = App(devices)
    app.run()


if __name__ == "__main__":
    main()
