"""
device_sim_gui.py — GUI multi-device simulator for the Retro Gadgets serial switch.

Opens a single window with one tab per simulated device. Each tab has:
  - A scrolling terminal output area (shows sent/received messages)
  - An input field at the bottom (type "DEST message" and hit Enter to send)

Requires: pip install pyserial

Usage:
  python device_sim_gui.py

  On launch a small config dialog lets you add devices (id + COM port + baud).
  Or pass them on the command line to skip the dialog:

  python device_sim_gui.py PC1:COM13 PC2:COM14 PC3:COM15 PC4:COM16
  python device_sim_gui.py PC1:COM13:9600 PC2:COM14:9600   (explicit baud)
"""

import argparse
import sys
import threading
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog
from datetime import datetime

import serial


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

        # Tag for sent messages (different color)
        self.output.tag_configure("sent", foreground="#88ccff")
        self.output.tag_configure("system", foreground="#888888")
        self.output.tag_configure("error", foreground="#ff4444")

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
        """Open the serial port and start the reader thread."""
        try:
            self.ser = serial.Serial(self.port, self.baud, timeout=1)
            self.running = True
            self.log(f"Connected to {self.port} @ {self.baud} baud", "system")
            self.log("Format: DEST message  (use ALL to broadcast)", "system")
            self.log("", "system")

            reader = threading.Thread(target=self._reader_loop, daemon=True)
            reader.start()
            return True
        except serial.SerialException as e:
            self.log(f"FAILED to open {self.port}: {e}", "error")
            return False

    def _reader_loop(self):
        """Background thread: reads lines from the serial port."""
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
            if text:
                self.log(f"<< {text}")

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
        frame = f"{self.device_id}:{dst}:{msg}\n"
        try:
            self.ser.write(frame.encode("utf-8"))
            self.log(f">> [{dst}] {msg}", "sent")
        except serial.SerialException as e:
            self.log(f"Send failed: {e}", "error")

    def disconnect(self):
        """Clean up the serial port."""
        self.running = False
        if self.ser:
            try:
                self.ser.close()
            except Exception:
                pass


class AddDeviceDialog(tk.Toplevel):
    """Simple dialog to configure devices before starting."""

    def __init__(self, parent):
        super().__init__(parent)
        self.title("Configure Devices")
        self.geometry("420x350")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        self.devices = []  # list of (id, port, baud) tuples
        self.result = None

        # Instructions
        ttk.Label(self, text="Add simulated devices (one per switch port):",
                  font=("Segoe UI", 10)).pack(pady=(10, 5))

        # Device list
        list_frame = ttk.Frame(self)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=10)

        self.device_listbox = tk.Listbox(list_frame, font=("Consolas", 10), height=8)
        self.device_listbox.pack(fill=tk.BOTH, expand=True)

        # Add device controls
        add_frame = ttk.Frame(self)
        add_frame.pack(fill=tk.X, padx=10, pady=5)

        ttk.Label(add_frame, text="ID:").grid(row=0, column=0, padx=2)
        self.id_entry = ttk.Entry(add_frame, width=8)
        self.id_entry.grid(row=0, column=1, padx=2)
        self.id_entry.insert(0, f"PC{len(self.devices) + 1}")

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
        btn_frame = ttk.Frame(self)
        btn_frame.pack(fill=tk.X, padx=10, pady=(0, 10))

        ttk.Button(btn_frame, text="Remove Selected", command=self._remove_device).pack(side=tk.LEFT)
        ttk.Button(btn_frame, text="Start", command=self._start).pack(side=tk.RIGHT)

        self.id_entry.focus_set()
        self.bind("<Return>", lambda e: self._add_device())

    def _add_device(self):
        device_id = self.id_entry.get().strip()
        port = self.port_entry.get().strip()
        baud_str = self.baud_entry.get().strip()

        if not device_id or not port:
            messagebox.showwarning("Missing info", "ID and Port are required.", parent=self)
            return

        try:
            baud = int(baud_str)
        except ValueError:
            messagebox.showwarning("Invalid baud", "Baud rate must be a number.", parent=self)
            return

        self.devices.append((device_id, port, baud))
        self.device_listbox.insert(tk.END, f"{device_id}  |  {port}  |  {baud} baud")

        # Auto-increment ID suggestion
        self.id_entry.delete(0, tk.END)
        self.id_entry.insert(0, f"PC{len(self.devices) + 1}")
        self.port_entry.delete(0, tk.END)
        self.port_entry.focus_set()

    def _remove_device(self):
        sel = self.device_listbox.curselection()
        if sel:
            idx = sel[0]
            self.device_listbox.delete(idx)
            self.devices.pop(idx)

    def _start(self):
        if not self.devices:
            messagebox.showwarning("No devices", "Add at least one device.", parent=self)
            return
        self.result = self.devices
        self.destroy()


class App:
    def __init__(self, devices: list):
        self.root = tk.Tk()
        self.root.title("Retro Gadgets — Serial Switch Device Simulator")
        self.root.geometry("700x500")
        self.root.minsize(500, 300)

        # Style
        style = ttk.Style()
        style.configure("TNotebook.Tab", padding=[12, 4])

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        self.tabs: list[DeviceTab] = []
        for device_id, port, baud in devices:
            tab = DeviceTab(self.notebook, device_id, port, baud)
            self.tabs.append(tab)

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # Connect all devices after GUI is set up
        self.root.after(100, self._connect_all)

    def _connect_all(self):
        for tab in self.tabs:
            tab.connect()

    def _on_close(self):
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
    # If CLI args provided, skip the dialog
    if len(sys.argv) > 1:
        devices = parse_cli_devices(sys.argv[1:])
    else:
        # Show config dialog
        root = tk.Tk()
        root.withdraw()
        dialog = AddDeviceDialog(root)
        root.wait_window(dialog)
        devices = dialog.result
        root.destroy()

        if not devices:
            print("No devices configured, exiting.")
            sys.exit(0)

    app = App(devices)
    app.run()


if __name__ == "__main__":
    main()
