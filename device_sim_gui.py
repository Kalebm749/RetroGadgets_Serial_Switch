"""
device_sim_gui.py — GUI multi-device simulator for the Retro Gadgets serial switch.

Opens a single window with one tab per simulated device. Each tab has:
  - A scrolling terminal output area (shows sent/received messages)
  - An input field at the bottom (type "DEST message" and hit Enter to send)

Features:
  - Auto-test mode: generates realistic random traffic between all connected devices
  - Supports the full switch protocol (HELLO, ARP, DATA frames)

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
import tkinter as tk
from tkinter import ttk, messagebox
from datetime import datetime

import serial


HEARTBEAT_INTERVAL = 2.0  # seconds between HELLO messages


# ============================================================
# REALISTIC TRAFFIC PATTERNS
# ============================================================

# Simulated traffic types with realistic payloads
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

# Traffic scenarios: weighted sequences of what a "conversation" looks like
TRAFFIC_SCENARIOS = [
    # Web browsing: DNS -> HTTP request -> HTTP response
    {"weight": 30, "sequence": ["dns_query", "dns_response", "http_request", "http_response"]},
    # Ping exchange
    {"weight": 15, "sequence": ["ping", "ping", "ping"]},
    # File transfer
    {"weight": 10, "sequence": ["file_transfer"]},
    # Chat messages
    {"weight": 20, "sequence": ["chat", "chat"]},
    # Database queries
    {"weight": 15, "sequence": ["database", "database"]},
    # Monitoring
    {"weight": 10, "sequence": ["monitoring", "monitoring"]},
]


def pick_scenario():
    """Pick a random traffic scenario based on weights."""
    total = sum(s["weight"] for s in TRAFFIC_SCENARIOS)
    r = random.randint(1, total)
    cumulative = 0
    for scenario in TRAFFIC_SCENARIOS:
        cumulative += scenario["weight"]
        if r <= cumulative:
            return scenario
    return TRAFFIC_SCENARIOS[0]


def pick_message(pattern_type: str) -> str:
    """Pick a random message from a traffic pattern."""
    return random.choice(TRAFFIC_PATTERNS[pattern_type])


# ============================================================
# DEVICE TAB
# ============================================================

class DeviceTab:
    """One simulated device — owns a serial connection and a GUI tab."""

    def __init__(self, notebook: ttk.Notebook, device_id: str, port: str, baud: int):
        self.device_id = device_id
        self.port = port
        self.baud = baud
        self.ser = None
        self.running = False

        # --- GUI setup ---
        self.frame = ttk.Frame(notebook)
        notebook.add(self.frame, text=f" {device_id} ({port}) ")

        # Terminal output
        self.output = tk.Text(self.frame, wrap=tk.WORD, state=tk.DISABLED,
                              bg="#1e1e1e", fg="#00ff88", font=("Consolas", 10),
                              insertbackground="white")
        self.output.pack(fill=tk.BOTH, expand=True, padx=4, pady=(4, 0))

        # Tags for different message types
        self.output.tag_configure("sent", foreground="#88ccff")
        self.output.tag_configure("system", foreground="#888888")
        self.output.tag_configure("error", foreground="#ff4444")
        self.output.tag_configure("arp", foreground="#cc88ff")
        self.output.tag_configure("autotest", foreground="#ffaa00")

        # Scrollbar
        scrollbar = ttk.Scrollbar(self.output, command=self.output.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.output.configure(yscrollcommand=scrollbar.set)

        # Input area
        input_frame = ttk.Frame(self.frame)
        input_frame.pack(fill=tk.X, padx=4, pady=4)

        self.prompt_label = ttk.Label(input_frame, text=f"{device_id}>", font=("Consolas", 10))
        self.prompt_label.pack(side=tk.LEFT)

        self.input_entry = ttk.Entry(input_frame, font=("Consolas", 10))
        self.input_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 4))
        self.input_entry.bind("<Return>", self._on_send)

        self.send_btn = ttk.Button(input_frame, text="Send", command=self._on_send)
        self.send_btn.pack(side=tk.RIGHT)

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

    def connect(self):
        """Open the serial port and start the reader + heartbeat threads."""
        try:
            self.ser = serial.Serial(self.port, self.baud, timeout=1)
            self.running = True
            self.log(f"Connected to {self.port} @ {self.baud} baud", "system")
            self.log("Format: DEST message  (use ALL to broadcast)", "system")
            self.log("", "system")

            # Start reader thread
            reader = threading.Thread(target=self._reader_loop, daemon=True)
            reader.start()

            # Start heartbeat thread
            heartbeat = threading.Thread(target=self._heartbeat_loop, daemon=True)
            heartbeat.start()

            return True
        except serial.SerialException as e:
            self.log(f"FAILED to open {self.port}: {e}", "error")
            return False

    def _heartbeat_loop(self):
        """Background thread: sends HELLO:<id> periodically."""
        while self.running:
            try:
                hello = f"HELLO:{self.device_id}\n"
                self.ser.write(hello.encode("utf-8"))
            except serial.SerialException:
                self.log("[heartbeat: connection lost]", "error")
                self.running = False
                return
            time.sleep(HEARTBEAT_INTERVAL)

    def _reader_loop(self):
        """Background thread: reads lines from the serial port and handles protocol."""
        while self.running:
            try:
                raw = self.ser.readline()
            except serial.SerialException:
                self.log("[connection lost]", "error")
                self.running = False
                return
            if not raw:
                continue
            text = raw.decode("utf-8", errors="replace").strip()
            if not text:
                continue

            # Handle ARP:WHO-HAS:<id> — auto-reply if it's asking for us
            if text.startswith("ARP:WHO-HAS:"):
                target = text[len("ARP:WHO-HAS:"):].strip()
                self.log(f"<< ARP WHO-HAS {target}", "arp")
                if target == self.device_id:
                    reply = f"ARP:IS-AT:{self.device_id}\n"
                    try:
                        self.ser.write(reply.encode("utf-8"))
                        self.log(f">> ARP IS-AT {self.device_id} (that's me!)", "arp")
                    except serial.SerialException:
                        self.log("[ARP reply failed]", "error")
                else:
                    self.log(f"   (not me, ignoring)", "arp")
                continue

            # Handle DATA:<src>:<dst>:<msg> — display the message
            if text.startswith("DATA:"):
                payload = text[5:]  # strip "DATA:" prefix
                parts = payload.split(":", 2)
                if len(parts) == 3:
                    src, dst, msg = parts
                    self.log(f"<< [{src}] {msg}")
                else:
                    self.log(f"<< (malformed) {text}", "error")
                continue

            # Legacy format or unknown — just display
            self.log(f"<< {text}")

    def send_data(self, dst: str, msg: str, tag: str = "sent"):
        """Send a DATA frame (used by both manual input and auto-test)."""
        if not self.ser or not self.running:
            return False
        frame = f"DATA:{self.device_id}:{dst}:{msg}\n"
        try:
            self.ser.write(frame.encode("utf-8"))
            self.log(f">> [{dst}] {msg}", tag)
            return True
        except serial.SerialException as e:
            self.log(f"Send failed: {e}", "error")
            return False

    def _on_send(self, event=None):
        """Handle Enter key or Send button."""
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
        """Clean up the serial port."""
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

    def __init__(self, tabs: list):
        self.tabs = tabs
        self.running = False
        self._thread = None
        self.rate = 2.0  # messages per second (across all devices)
        self.burst_chance = 0.15  # chance of a burst (multiple rapid messages)

    def start(self, rate: float = 2.0):
        """Start generating traffic."""
        if self.running:
            return
        self.rate = rate
        self.running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        """Stop generating traffic."""
        self.running = False

    def _get_alive_tabs(self):
        """Get list of tabs that are connected and running."""
        return [t for t in self.tabs if t.running and t.ser]

    def _run(self):
        """Main auto-test loop."""
        while self.running:
            alive = self._get_alive_tabs()
            if len(alive) < 2:
                time.sleep(0.5)
                continue

            # Pick a scenario
            scenario = pick_scenario()

            # Pick sender and receiver for this scenario
            sender = random.choice(alive)
            others = [t for t in alive if t is not sender]
            receiver = random.choice(others)

            # Decide if this is a broadcast (10% chance)
            is_broadcast = random.random() < 0.10

            # Execute the scenario sequence
            for pattern_type in scenario["sequence"]:
                if not self.running:
                    break

                msg = pick_message(pattern_type)
                dst = "ALL" if is_broadcast else receiver.device_id
                sender.send_data(dst, msg, "autotest")

                # Small delay between messages in a sequence (simulates RTT)
                delay = random.uniform(0.1, 0.4)
                time.sleep(delay)

                # For request-response patterns, swap sender/receiver
                if "response" in pattern_type or "REPLY" in pattern_type:
                    sender, receiver = receiver, sender
                    if receiver not in alive:
                        receiver = random.choice([t for t in alive if t is not sender])

            # Check for burst mode
            if random.random() < self.burst_chance:
                burst_count = random.randint(3, 8)
                for _ in range(burst_count):
                    if not self.running:
                        break
                    burst_sender = random.choice(alive)
                    burst_others = [t for t in alive if t is not burst_sender]
                    burst_receiver = random.choice(burst_others)
                    burst_msg = pick_message(random.choice(list(TRAFFIC_PATTERNS.keys())))
                    burst_sender.send_data(burst_receiver.device_id, burst_msg, "autotest")
                    time.sleep(random.uniform(0.05, 0.15))

            # Wait based on configured rate
            base_delay = 1.0 / self.rate
            jitter = random.uniform(-base_delay * 0.3, base_delay * 0.3)
            time.sleep(max(0.1, base_delay + jitter))


# ============================================================
# MAIN APP
# ============================================================

class App:
    """
    Single-window application. Uses ONE Tk root for the entire lifetime.
    Starts with a config view, then swaps to the tabbed device view.
    """

    def __init__(self, devices: list = None):
        self.root = tk.Tk()
        self.root.title("Retro Gadgets — Serial Switch Device Simulator")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.tabs: list[DeviceTab] = []
        self.devices = devices
        self.auto_test: AutoTestEngine = None

        if self.devices:
            self._build_device_view()
        else:
            self._build_config_view()

    def _build_config_view(self):
        """Build the device configuration UI inside the root window."""
        self.root.geometry("420x350")
        self.root.resizable(False, False)

        self.config_frame = ttk.Frame(self.root)
        self.config_frame.pack(fill=tk.BOTH, expand=True)

        self._config_devices = []

        # Instructions
        ttk.Label(self.config_frame, text="Add simulated devices (one per switch port):",
                  font=("Segoe UI", 10)).pack(pady=(10, 5))

        # Device list
        list_frame = ttk.Frame(self.config_frame)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=10)

        self.device_listbox = tk.Listbox(list_frame, font=("Consolas", 10), height=8)
        self.device_listbox.pack(fill=tk.BOTH, expand=True)

        # Add device controls
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

        # Bottom buttons
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

        # Auto-increment ID suggestion
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

        # Tear down config UI and switch to device view
        self.root.unbind("<Return>")
        self.config_frame.destroy()
        self._build_device_view()

    def _build_device_view(self):
        """Build the tabbed device terminal UI inside the root window."""
        self.root.geometry("750x550")
        self.root.resizable(True, True)
        self.root.minsize(500, 300)

        # Style
        style = ttk.Style()
        style.configure("TNotebook.Tab", padding=[12, 4])

        # Toolbar frame at top
        toolbar = ttk.Frame(self.root)
        toolbar.pack(fill=tk.X, padx=4, pady=(4, 0))

        # Auto-test controls
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

        # Status indicator
        self.status_label = ttk.Label(toolbar, text="● Idle", foreground="gray",
                                       font=("Segoe UI", 9))
        self.status_label.pack(side=tk.RIGHT, padx=8)

        # Notebook with device tabs
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        for device_id, port, baud in self.devices:
            tab = DeviceTab(self.notebook, device_id, port, baud)
            self.tabs.append(tab)

        # Initialize auto-test engine
        self.auto_test = AutoTestEngine(self.tabs)

        # Connect all devices after GUI is drawn
        self.root.after(100, self._connect_all)

    def _connect_all(self):
        for tab in self.tabs:
            tab.connect()

    def _toggle_autotest(self):
        """Toggle auto-test on/off."""
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
        for tab in self.tabs:
            tab.disconnect()
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
