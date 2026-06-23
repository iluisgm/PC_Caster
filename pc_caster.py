#!/usr/bin/env python3
"""
PC Caster
Cast URLs from your Windows PC to Roku or Fire TV.
Discovers devices automatically via SSDP, uses Roku ECP and ADB.
"""

import tkinter as tk
from tkinter import ttk, messagebox
import socket
import threading
import time
import re
import subprocess
import json
import os
import webbrowser
from urllib.parse import quote

from stream_finder import find_streams, find_streams_interactive
from hls_proxy import HlsProxy, ensure_firewall_rule
import roku_deploy
from logger import setup_logging, LOG_FILE

log = setup_logging()

try:
    import requests
    import xml.etree.ElementTree as ET
except ImportError:
    import subprocess as sp
    sp.run(["pip", "install", "requests"], check=True)
    import requests
    import xml.etree.ElementTree as ET


# ── Constants ────────────────────────────────────────────────────────────────
SSDP_ADDR         = "239.255.255.250"
SSDP_PORT         = 1900
SSDP_TIMEOUT      = 4          # seconds to wait for SSDP responses
ROKU_MEDIA_PLAYER = "2285"     # Roku built-in Media Player channel ID
CONFIG_FILE       = os.path.join(os.path.expanduser("~"), ".pc_caster.json")
_OLD_CONFIG_FILE  = os.path.join(os.path.expanduser("~"), ".castify_pc.json")

_APP_DIR   = os.path.dirname(os.path.abspath(__file__))
ICON_ICO   = os.path.join(_APP_DIR, "assets", "app_icon.ico")
ICON_PNG   = os.path.join(_APP_DIR, "assets", "app_icon.png")


# ── Colour palette (GitHub-dark inspired) ────────────────────────────────────
BG       = "#0d1117"
CARD     = "#161b22"
BORDER   = "#30363d"
FG       = "#e6edf3"
MUTED    = "#8b949e"
ACCENT   = "#58a6ff"
GREEN    = "#3fb950"
RED      = "#f85149"
ORANGE   = "#d29922"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _ssdp_scan(search_target: str) -> list[str]:
    """Send SSDP M-SEARCH and return list of IPs that respond."""
    msg = (
        "M-SEARCH * HTTP/1.1\r\n"
        f"HOST: {SSDP_ADDR}:{SSDP_PORT}\r\n"
        'MAN: "ssdp:discover"\r\n'
        "MX: 3\r\n"
        f"ST: {search_target}\r\n"
        "\r\n"
    )
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        s.settimeout(SSDP_TIMEOUT)
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        s.sendto(msg.encode(), (SSDP_ADDR, SSDP_PORT))
        while True:
            try:
                data = s.recv(4096).decode(errors="ignore")
                m = re.search(r"[Ll]ocation:\s*http://([0-9.]+)", data)
                if m:
                    ips.add(m.group(1))
            except socket.timeout:
                break
    except Exception as e:
        print(f"[SSDP] Error scanning {search_target}: {e}")
    finally:
        s.close()
    return list(ips)


def _roku_device_name(ip: str) -> str:
    """Query Roku ECP for a human-readable device name."""
    try:
        r = requests.get(f"http://{ip}:8060/query/device-info", timeout=2)
        root = ET.fromstring(r.text)
        name = root.findtext("user-device-name") or root.findtext("model-name")
        return name if name else f"Roku ({ip})"
    except Exception:
        return f"Roku ({ip})"


def _find_roku_app(ip: str, name_substr: str) -> str | None:
    """Return the channel ID of the first installed app whose name contains
    name_substr (case-insensitive), else None."""
    try:
        r = requests.get(f"http://{ip}:8060/query/apps", timeout=3)
        root = ET.fromstring(r.text)
        for app in root.findall("app"):
            if app.text and name_substr.lower() in app.text.lower():
                return app.get("id")
    except Exception:
        pass
    return None


def _cast_roku(ip: str, url: str) -> tuple[bool, str]:
    """
    Cast a URL to a Roku device using the built-in Roku Media Player, which is
    the reliable way to play an arbitrary HLS/MP4 URL via ECP deep-link.
    Returns (success, message).
    """
    encoded = quote(url, safe="")
    is_hls = ".m3u8" in url.split("?", 1)[0].lower()
    vfmt = "hls" if is_hls else "mp4"

    # Find the Roku Media Player channel id (commonly 2213, sometimes 2285).
    mp = (_find_roku_app(ip, "Roku Media Player")
          or _find_roku_app(ip, "Media Player"))
    candidates = [c for c in (mp, "2213", "2285") if c]

    last_status = None
    for chan in candidates:
        # Roku Media Player deep-link: t=v (video), u=<url>, videoFormat=hls|mp4
        launch = (f"http://{ip}:8060/launch/{chan}"
                  f"?t=v&u={encoded}&videoName=PC%20Caster&videoFormat={vfmt}")
        try:
            r = requests.post(launch, timeout=6)
            last_status = r.status_code
            if r.status_code == 200:
                return True, f"Sent to Roku Media Player (channel {chan})"
        except Exception as e:
            last_status = str(e)
    return False, f"Roku ECP launch failed (last={last_status})"


def _cast_via_castify(ip: str, url: str, fmt: str) -> tuple[bool, str]:
    """
    EXPERIMENTAL — drive the stock Castify channel (651775). Castify normally
    only obeys its own paired apps, so this may not play. Kept as an option to
    iterate on once the custom receiver works.
    """
    enc = quote(url, safe="")
    cid = _find_roku_app(ip, "castify") or "651775"
    try:
        requests.post(f"http://{ip}:8060/launch/{cid}", timeout=6)
        time.sleep(7)
        for path in (f"input?contentId={enc}&mediaType={fmt}",
                     f"input?url={enc}",
                     f"launch/{cid}?contentId={enc}&mediaType={fmt}"):
            requests.post(f"http://{ip}:8060/{path}", timeout=6)
            time.sleep(1)
        return True, f"Sent to Castify (channel {cid}) — experimental, may not play."
    except Exception as e:
        return False, str(e)


def _cast_firetv(ip: str, url: str) -> tuple[bool, str]:
    """
    Cast a URL to Fire TV via ADB.
    Tries Castify first, falls back to opening in the default browser.
    Returns (success, message).
    """
    # 1. Check ADB is in PATH
    try:
        subprocess.run(
            ["adb", "version"], capture_output=True, timeout=3, check=True
        )
    except FileNotFoundError:
        return False, "ADB_MISSING"
    except subprocess.CalledProcessError:
        return False, "ADB_ERROR"

    # 2. Connect to Fire TV
    conn = subprocess.run(
        ["adb", "connect", f"{ip}:5555"],
        capture_output=True, text=True, timeout=8
    )
    time.sleep(1.2)

    # 3. Try Castify via intent
    castify_pkgs = ["com.castify", "castify.roku"]
    for pkg in castify_pkgs:
        result = subprocess.run(
            ["adb", "-s", f"{ip}:5555", "shell",
             "pm", "list", "packages", pkg],
            capture_output=True, text=True, timeout=5
        )
        if pkg in result.stdout:
            r2 = subprocess.run(
                ["adb", "-s", f"{ip}:5555", "shell",
                 "am", "start", "-a", "android.intent.action.VIEW",
                 "-d", url, "-n", f"{pkg}/.MainActivity"],
                capture_output=True, text=True, timeout=5
            )
            if r2.returncode == 0:
                return True, f"Sent to Castify ({pkg})"

    # 4. Fallback — open in default browser (Silk)
    r3 = subprocess.run(
        ["adb", "-s", f"{ip}:5555", "shell",
         "am", "start", "-a", "android.intent.action.VIEW", "-d", url],
        capture_output=True, text=True, timeout=5
    )
    if r3.returncode == 0:
        return True, "Opened in Fire TV browser (Castify not found)"
    return False, r3.stderr.strip() or "ADB command failed"


# ── Config persistence ────────────────────────────────────────────────────────

def _load_config() -> dict:
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except Exception:
        pass
    # Migrate settings (dev password, devices) from the old app name.
    try:
        with open(_OLD_CONFIG_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_config(cfg: dict):
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass


# ── Main Application ──────────────────────────────────────────────────────────

class PCCaster:

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("PC Caster")
        self.root.geometry("700x560")
        self.root.configure(bg=BG)
        self.root.resizable(True, True)
        self.root.minsize(560, 460)
        self._apply_window_icon()

        # Route Tk callback errors into the log instead of the void.
        self.root.report_callback_exception = \
            lambda exc, val, tb: log.error("Tk callback error",
                                           exc_info=(exc, val, tb))

        self.devices: dict[str, dict] = {}   # name -> {ip, type}
        self._scanning = False
        self._cfg = _load_config()
        self._clipboard_last = ""
        self._last_referer = ""
        self._proxy = HlsProxy(port=8011)   # local HLS proxy (lazy-started)

        self._build_styles()
        self._build_ui()
        self._start_scan()

        # Poll clipboard every 800ms for convenience
        self._poll_clipboard()

    # ── Styles ────────────────────────────────────────────────────────────────

    def _build_styles(self):
        s = ttk.Style()
        s.theme_use("clam")

        s.configure(".", background=BG, foreground=FG, relief="flat")
        s.configure("TFrame",    background=BG)
        s.configure("Card.TFrame", background=CARD)
        s.configure("TLabel",   background=BG, foreground=FG,   font=("Segoe UI", 10))
        s.configure("H1.TLabel", background=BG, foreground=FG,  font=("Segoe UI", 15, "bold"))
        s.configure("Small.TLabel", background=BG, foreground=MUTED, font=("Segoe UI", 9))
        s.configure("CardLabel.TLabel", background=CARD, foreground=MUTED, font=("Segoe UI", 9))

        s.configure("TButton",
            background=CARD, foreground=FG, font=("Segoe UI", 10),
            relief="flat", padding=(10, 5), borderwidth=1)
        s.map("TButton",
            background=[("active", "#21262d"), ("pressed", "#30363d")],
            foreground=[("active", FG)])

        s.configure("Primary.TButton",
            background=ACCENT, foreground="#0d1117",
            font=("Segoe UI", 12, "bold"), padding=(16, 10))
        s.map("Primary.TButton",
            background=[("active", "#79c0ff"), ("pressed", "#388bfd"),
                         ("disabled", "#1f3a5c")],
            foreground=[("disabled", "#4a6a8c")])

        s.configure("Treeview",
            background=CARD, foreground=FG, fieldbackground=CARD,
            rowheight=36, font=("Segoe UI", 10), borderwidth=0)
        s.configure("Treeview.Heading",
            background="#21262d", foreground=MUTED,
            font=("Segoe UI", 9, "bold"), relief="flat")
        s.map("Treeview",
            background=[("selected", "#1f6feb")],
            foreground=[("selected", FG)])

        s.configure("TRadiobutton", background=BG, foreground=FG, font=("Segoe UI", 10))
        s.map("TRadiobutton", background=[("active", BG)])

    # ── UI Build ──────────────────────────────────────────────────────────────

    def _apply_window_icon(self):
        """Set the title-bar + taskbar icon (and group under our own AppID)."""
        # Distinct AppUserModelID so Windows shows our icon in the taskbar
        # instead of the generic python.exe icon.
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "PCCaster.App")
        except Exception:
            pass
        try:
            if os.path.exists(ICON_ICO):
                self.root.iconbitmap(default=ICON_ICO)
        except Exception:
            pass
        # iconphoto as a fallback / for the window list
        try:
            if os.path.exists(ICON_PNG):
                self._icon_img = tk.PhotoImage(file=ICON_PNG)
                self.root.iconphoto(True, self._icon_img)
        except Exception:
            pass

    def _build_ui(self):
        # ── Main container
        main = ttk.Frame(self.root, padding="18 16 18 4")
        main.pack(fill="both", expand=True)
        main.columnconfigure(0, weight=1)

        # ── Header
        hdr = ttk.Frame(main)
        hdr.grid(row=0, column=0, sticky="ew", pady=(0, 14))
        # Brand icon (scaled down from the app icon)
        try:
            self._hdr_icon = tk.PhotoImage(file=ICON_PNG).subsample(16, 16)
            tk.Label(hdr, image=self._hdr_icon, bg=BG).pack(side="left", padx=(0, 8))
        except Exception:
            tk.Label(hdr, text="📺", bg=BG, fg=FG,
                     font=("Segoe UI", 15)).pack(side="left", padx=(0, 8))
        tk.Label(hdr, text="PC Caster",
                  bg=BG, fg=FG, font=("Segoe UI", 15, "bold")).pack(side="left")
        tk.Label(hdr, text="Cast links to your TV from Windows",
                  bg=BG, fg=MUTED, font=("Segoe UI", 9)).pack(side="left", padx=(10, 0), pady=(4, 0))

        # ── Divider
        tk.Frame(main, bg=BORDER, height=1).grid(row=1, column=0, sticky="ew", pady=(0, 14))

        # ── Device section label row
        dev_hdr = ttk.Frame(main)
        dev_hdr.grid(row=2, column=0, sticky="ew", pady=(0, 6))
        tk.Label(dev_hdr, text="Devices", bg=BG, fg=MUTED,
                  font=("Segoe UI", 9, "bold")).pack(side="left")
        self.scan_btn = ttk.Button(dev_hdr, text="↺  Scan", command=self._start_scan)
        self.scan_btn.pack(side="right")
        ttk.Button(dev_hdr, text="＋  Add IP", command=self._add_manual).pack(side="right", padx=(0, 6))
        ttk.Button(dev_hdr, text="📺  TV App", command=self._setup_tv_app).pack(side="right", padx=(0, 6))
        ttk.Button(dev_hdr, text="📄  Log", command=self._open_log).pack(side="right", padx=(0, 6))

        # ── Device list
        tree_container = tk.Frame(main, bg=BORDER, bd=1, relief="solid")
        tree_container.grid(row=3, column=0, sticky="nsew", pady=(0, 14))
        main.rowconfigure(3, weight=1)

        inner = tk.Frame(tree_container, bg=CARD)
        inner.pack(fill="both", expand=True, padx=1, pady=1)

        cols = ("name", "type", "ip")
        self.tree = ttk.Treeview(inner, columns=cols, show="headings", height=6)
        self.tree.heading("name", text="Device Name")
        self.tree.heading("type", text="Type")
        self.tree.heading("ip",   text="IP Address")
        self.tree.column("name", width=310, stretch=True)
        self.tree.column("type", width=90,  anchor="center", stretch=False)
        self.tree.column("ip",   width=130, anchor="center", stretch=False)

        vsb = ttk.Scrollbar(inner, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        # ── URL section
        tk.Label(main, text="URL TO CAST", bg=BG, fg=MUTED,
                  font=("Segoe UI", 9, "bold")).grid(row=4, column=0, sticky="w", pady=(0, 6))

        url_row = ttk.Frame(main)
        url_row.grid(row=5, column=0, sticky="ew", pady=(0, 14))
        url_row.columnconfigure(0, weight=1)

        self.url_var = tk.StringVar()
        self.url_entry = tk.Entry(
            url_row, textvariable=self.url_var,
            bg=CARD, fg=FG, insertbackground=FG,
            font=("Segoe UI", 11), relief="flat",
            highlightbackground=BORDER, highlightcolor=ACCENT,
            highlightthickness=1, bd=0
        )
        self.url_entry.grid(row=0, column=0, sticky="ew", ipady=9, padx=(0, 8))
        self.url_entry.bind("<Return>", lambda e: self._cast())

        btn_frame = ttk.Frame(url_row)
        btn_frame.grid(row=0, column=1, sticky="e")
        self.find_btn = ttk.Button(btn_frame, text="🔍  Find .m3u8", command=self._find_streams)
        self.find_btn.pack(side="left", padx=(0, 4))
        ttk.Button(btn_frame, text="🖥  Test on PC", command=self._test_on_pc).pack(side="left", padx=(0, 4))
        ttk.Button(btn_frame, text="Paste", command=self._paste_url).pack(side="left", padx=(0, 4))
        ttk.Button(btn_frame, text="✕",     command=lambda: self.url_var.set("")).pack(side="left")

        # ── Cast button
        self.cast_btn = ttk.Button(
            main, text="▶   Cast to TV",
            style="Primary.TButton", command=self._cast
        )
        self.cast_btn.grid(row=6, column=0, sticky="ew", pady=(0, 6))

        # ── Tip label
        self.tip_var = tk.StringVar(value="")
        tk.Label(main, textvariable=self.tip_var,
                  bg=BG, fg=MUTED, font=("Segoe UI", 9),
                  wraplength=640, justify="left").grid(row=7, column=0, sticky="w")

        # ── Status bar
        bar = tk.Frame(self.root, bg="#0a0e14")
        bar.pack(fill="x", side="bottom")
        self.status_var = tk.StringVar(value="Starting…")
        self._status_dot = tk.Label(bar, text="●", bg="#0a0e14", fg=MUTED,
                                     font=("Segoe UI", 9))
        self._status_dot.pack(side="left", padx=(10, 4), pady=6)
        tk.Label(bar, textvariable=self.status_var, bg="#0a0e14", fg=MUTED,
                  font=("Segoe UI", 9), anchor="w").pack(side="left", pady=6)

        # ── Proxy activity indicator (right side)
        self.proxy_var = tk.StringVar(value="○ Proxy off")
        self._proxy_lbl = tk.Label(bar, textvariable=self.proxy_var, bg="#0a0e14",
                                   fg=MUTED, font=("Segoe UI", 9))
        self._proxy_lbl.pack(side="right", padx=(0, 12), pady=6)
        self._poll_proxy_status()

    # ── Proxy status indicator ──────────────────────────────────────────────────

    def _poll_proxy_status(self):
        try:
            if not self._proxy.running:
                self.proxy_var.set("○ Proxy off")
                self._proxy_lbl.configure(fg=MUTED)
            elif self._proxy.seconds_since_activity() < 6:
                # Roku pulled a segment/playlist very recently → actively serving
                n = self._proxy.requests_served
                self.proxy_var.set(f"⚡ Streaming to TV · {n} reqs")
                self._proxy_lbl.configure(fg=GREEN)
            else:
                self.proxy_var.set("● Proxy ready (idle)")
                self._proxy_lbl.configure(fg=ACCENT)
        except Exception:
            pass
        self.root.after(1000, self._poll_proxy_status)

    # ── Status helpers ────────────────────────────────────────────────────────

    def _set_status(self, msg: str, color: str = MUTED):
        self.root.after(0, lambda: (
            self.status_var.set(msg),
            self._status_dot.configure(fg=color),
        ))

    def _set_tip(self, msg: str):
        self.root.after(0, lambda: self.tip_var.set(msg))

    # ── Clipboard polling ─────────────────────────────────────────────────────

    def _poll_clipboard(self):
        try:
            clip = self.root.clipboard_get().strip()
            if (clip != self._clipboard_last
                    and clip.startswith(("http://", "https://"))
                    and not self.url_var.get()):
                self._clipboard_last = clip
                self.url_var.set(clip)
                self._set_tip("💡 URL auto-pasted from clipboard.")
        except Exception:
            pass
        self.root.after(800, self._poll_clipboard)

    def _paste_url(self):
        try:
            self.url_var.set(self.root.clipboard_get().strip())
            self._set_tip("")
        except Exception:
            pass

    # ── Device discovery ──────────────────────────────────────────────────────

    def _start_scan(self):
        if self._scanning:
            return
        self._scanning = True
        self.scan_btn.configure(state="disabled", text="↺  Scanning…")
        self._set_status("Scanning your network for Roku and Fire TV devices…", ORANGE)
        threading.Thread(target=self._scan_worker, daemon=True).start()

    def _scan_worker(self):
        found: dict[str, dict] = {}

        # Roku — well-known SSDP target
        for ip in _ssdp_scan("roku:ecp"):
            name = _roku_device_name(ip)
            found[name] = {"ip": ip, "type": "Roku"}

        # Fire TV / Amazon devices — use DIAL target
        for ip in _ssdp_scan("urn:dial-multiscreen-org:service:dial:1"):
            if ip not in [v["ip"] for v in found.values()]:
                found[f"Fire TV ({ip})"] = {"ip": ip, "type": "Fire TV"}

        # Merge manually-added devices
        manual = self._cfg.get("manual_devices", {})
        for name, info in manual.items():
            if name not in found:
                found[name] = info

        self.devices = found
        self.root.after(0, self._refresh_list)

    def _refresh_list(self):
        for i in self.tree.get_children():
            self.tree.delete(i)

        for name, info in self.devices.items():
            icon = "📺" if info["type"] == "Roku" else "🔥"
            self.tree.insert("", "end",
                              iid=name,
                              values=(f"{icon}  {name}", info["type"], info["ip"]))

        # Re-select last used device
        last = self._cfg.get("last_device")
        if last and last in self.devices:
            self.tree.selection_set(last)

        if self.devices:
            self._set_status(
                f"Found {len(self.devices)} device(s) — select one and paste a URL.", GREEN
            )
        else:
            self._set_status(
                "No devices found. Make sure they're on the same Wi-Fi, then try Scan or Add IP.", RED
            )
            self._set_tip(
                "Tip: If auto-scan fails, click '＋ Add IP' and enter your device's IP address manually."
            )

        self._scanning = False
        self.scan_btn.configure(state="normal", text="↺  Scan")

    def _add_manual(self):
        """Dialog to add a device by IP."""
        dlg = tk.Toplevel(self.root)
        dlg.title("Add Device Manually")
        dlg.geometry("380x200")
        dlg.configure(bg=BG)
        dlg.transient(self.root)
        dlg.grab_set()
        dlg.resizable(False, False)

        f = ttk.Frame(dlg, padding="20 16")
        f.pack(fill="both", expand=True)

        ttk.Label(f, text="Device IP address:").grid(row=0, column=0, sticky="w", pady=4)
        ip_var = tk.StringVar()
        ip_e = tk.Entry(f, textvariable=ip_var, bg=CARD, fg=FG,
                         insertbackground=FG, font=("Segoe UI", 11),
                         highlightbackground=BORDER, highlightthickness=1, relief="flat", width=22)
        ip_e.grid(row=0, column=1, padx=(10, 0), pady=4)
        ip_e.focus()

        ttk.Label(f, text="Device type:").grid(row=1, column=0, sticky="w", pady=4)
        type_var = tk.StringVar(value="Roku")
        rb_frame = ttk.Frame(f)
        rb_frame.grid(row=1, column=1, sticky="w", padx=(10, 0))
        ttk.Radiobutton(rb_frame, text="Roku",    variable=type_var, value="Roku").pack(side="left", padx=(0, 10))
        ttk.Radiobutton(rb_frame, text="Fire TV", variable=type_var, value="Fire TV").pack(side="left")

        def _ok():
            ip = ip_var.get().strip()
            if not ip:
                return
            t = type_var.get()
            name = _roku_device_name(ip) if t == "Roku" else f"Fire TV ({ip})"
            self.devices[name] = {"ip": ip, "type": t}
            # Persist manual device
            manual = self._cfg.setdefault("manual_devices", {})
            manual[name] = {"ip": ip, "type": t}
            _save_config(self._cfg)
            self._refresh_list()
            dlg.destroy()

        ttk.Button(f, text="Add Device", command=_ok, style="Primary.TButton").grid(
            row=2, column=0, columnspan=2, pady=(20, 0), sticky="ew"
        )
        dlg.bind("<Return>", lambda e: _ok())

    # ── Stream scanning (.m3u8 finder) ─────────────────────────────────────────

    def _find_streams(self):
        page_url = self.url_var.get().strip()
        if not page_url:
            messagebox.showwarning(
                "No URL",
                "Paste the web page URL (the streaming page) first, "
                "then click Find .m3u8."
            )
            return

        # State shared with the worker thread.
        self._scan_stop = threading.Event()
        self._scan_results = []          # list[dict], appended by worker
        self._scan_seen = set()          # urls already shown in the modal
        self._scan_lock = threading.Lock()
        self._scan_token = None          # 'PLAYWRIGHT_MISSING' / 'NO_BROWSER'

        self.find_btn.configure(state="disabled", text="🔍  Scanning…")
        self._open_scanner_modal()

        threading.Thread(
            target=self._stream_worker, args=(page_url,), daemon=True
        ).start()
        self._poll_scan_modal()

    def _stream_worker(self, page_url: str):
        def log(msg: str):
            if msg in ("PLAYWRIGHT_MISSING", "PLAYWRIGHT_NO_BROWSER"):
                self._scan_token = msg
            else:
                self._set_status(msg, ORANGE)

        def on_found(item):
            with self._scan_lock:
                self._scan_results.append(item)

        try:
            find_streams_interactive(
                page_url, on_log=log, on_found=on_found,
                stop_event=self._scan_stop, max_seconds=240,
            )
        except Exception as e:
            self._set_status(f"Scan error: {e}", RED)

        self.root.after(0, self._scan_finished)

    # ── Live scanner modal ──────────────────────────────────────────────────────

    def _open_scanner_modal(self):
        dlg = tk.Toplevel(self.root)
        self._scan_dlg = dlg
        dlg.title("Stream scanner")
        dlg.geometry("640x380")
        dlg.configure(bg=BG)
        dlg.transient(self.root)

        f = ttk.Frame(dlg, padding="16 14")
        f.pack(fill="both", expand=True)
        tk.Label(f, text="📡  Scanning for .m3u8 streams",
                 bg=BG, fg=FG, font=("Segoe UI", 13, "bold")).pack(anchor="w")
        tk.Label(
            f,
            text="A browser window opened. Click the server you want (TSN 4, "
                 "FOX, ITV1…) and press play if needed. Streams appear below "
                 "the moment the player requests them.",
            bg=BG, fg=MUTED, font=("Segoe UI", 9),
            wraplength=600, justify="left",
        ).pack(anchor="w", pady=(2, 10))

        list_wrap = tk.Frame(f, bg=BORDER, bd=1, relief="solid")
        list_wrap.pack(fill="both", expand=True)
        cols = ("label", "host", "ref")
        tv = ttk.Treeview(list_wrap, columns=cols, show="headings", height=7)
        tv.heading("label", text="Stream")
        tv.heading("host",  text="Host")
        tv.heading("ref",   text="Needs Referer?")
        tv.column("label", width=210, stretch=True)
        tv.column("host",  width=260, stretch=True)
        tv.column("ref",   width=110, anchor="center", stretch=False)
        tv.pack(fill="both", expand=True, padx=1, pady=1)
        self._scan_tv = tv

        btns = ttk.Frame(f)
        btns.pack(fill="x", pady=(10, 0))
        self._scan_count = tk.StringVar(value="Found: 0")
        tk.Label(btns, textvariable=self._scan_count, bg=BG, fg=MUTED,
                 font=("Segoe UI", 9)).pack(side="left")
        ttk.Button(btns, text="Use selected", style="Primary.TButton",
                   command=self._scan_use_selected).pack(side="right")
        ttk.Button(btns, text="Stop", command=self._scan_stop_clicked).pack(
            side="right", padx=(0, 8))

        tv.bind("<Double-1>", lambda e: self._scan_use_selected())
        dlg.protocol("WM_DELETE_WINDOW", self._scan_stop_clicked)

    def _poll_scan_modal(self):
        if not getattr(self, "_scan_dlg", None) or not self._scan_dlg.winfo_exists():
            return
        with self._scan_lock:
            items = list(self._scan_results)
        from urllib.parse import urlparse
        for i, r in enumerate(items):
            if r["url"] in self._scan_seen:
                continue
            self._scan_seen.add(r["url"])
            host = urlparse(r["url"]).netloc
            needs = "yes" if r.get("referer") else "—"
            self._scan_tv.insert("", "end", iid=str(i),
                                 values=(r["label"], host, needs))
            if len(self._scan_tv.get_children()) == 1:
                self._scan_tv.selection_set("0")
        self._scan_count.set(f"Found: {len(items)}")
        self.root.after(400, self._poll_scan_modal)

    def _scan_use_selected(self):
        sel = self._scan_tv.selection() if getattr(self, "_scan_tv", None) else None
        if not sel:
            messagebox.showinfo(
                "Pick a stream",
                "No stream selected yet. Click a server in the browser window, "
                "wait for it to appear here, then select it."
            )
            return
        with self._scan_lock:
            chosen = self._scan_results[int(sel[0])]
        self._apply_stream(chosen)
        self._scan_stop.set()
        if getattr(self, "_scan_dlg", None) and self._scan_dlg.winfo_exists():
            self._scan_dlg.destroy()

    def _scan_stop_clicked(self):
        if getattr(self, "_scan_stop", None):
            self._scan_stop.set()
        if getattr(self, "_scan_dlg", None) and self._scan_dlg.winfo_exists():
            self._scan_dlg.destroy()

    def _scan_finished(self):
        self.find_btn.configure(state="normal", text="🔍  Find .m3u8")
        if getattr(self, "_scan_dlg", None) and self._scan_dlg.winfo_exists():
            self._scan_dlg.destroy()

        if self._scan_token in ("PLAYWRIGHT_MISSING", "PLAYWRIGHT_NO_BROWSER"):
            self._set_status("Stream scanner needs a one-time setup.", RED)
            messagebox.showinfo(
                "One-time setup needed",
                "The scanner uses a headless browser engine. Install it once:\n\n"
                "Open a terminal in this folder and run:\n"
                "      pip install playwright\n"
                "      python -m playwright install chromium\n\n"
                "Then click Find .m3u8 again."
            )
            return

        with self._scan_lock:
            n = len(self._scan_results)
            urls = [r["url"][:90] for r in self._scan_results]
        log.info("Scan finished: %d stream(s) captured: %s", n, urls)
        if n == 0:
            self._set_status("No .m3u8 captured. Did you click a server link?", RED)
        else:
            self._set_status(f"Scanner closed — {n} stream(s) captured.", GREEN)

    def _apply_stream(self, stream: dict):
        """Put the chosen .m3u8 in the URL box and remember its Referer."""
        self.url_var.set(stream["url"])
        self._last_referer = stream.get("referer", "")
        self._set_status(f"Selected stream: {stream['label']} — ready to cast.", GREEN)
        if self._last_referer:
            self._set_tip(
                "Note: this stream wants Referer "
                f"'{self._last_referer}'. Roku's built-in player can't send "
                "custom headers, so it may 403 — Fire TV (browser fallback) is "
                "more likely to play it."
            )
        else:
            self._set_tip("")

    def _open_log(self):
        """Open the log file in the default text editor."""
        try:
            if not os.path.exists(LOG_FILE):
                with open(LOG_FILE, "w", encoding="utf-8") as f:
                    f.write("(log is empty so far)\n")
            os.startfile(LOG_FILE)   # Windows: opens in Notepad/default
        except Exception as e:
            messagebox.showinfo("Log location",
                                f"Couldn't open the log automatically ({e}).\n\n"
                                f"It's here:\n{LOG_FILE}")

    # ── TV App (custom Roku receiver) setup ─────────────────────────────────────

    def _setup_tv_app(self):
        ip = ""
        sel = self.tree.selection()
        if sel and self.devices.get(sel[0]):
            ip = self.devices[sel[0]]["ip"]

        dlg = tk.Toplevel(self.root)
        dlg.title("Set up your TV App (one-time)")
        dlg.geometry("560x520")
        dlg.configure(bg=BG)
        dlg.transient(self.root)
        dlg.grab_set()

        f = ttk.Frame(dlg, padding="18 16")
        f.pack(fill="both", expand=True)

        tk.Label(f, text="📺  Install your own TV receiver",
                 bg=BG, fg=FG, font=("Segoe UI", 14, "bold")).pack(anchor="w")
        steps = (
            "This installs a tiny channel on your Roku that THIS app controls "
            "directly — so casting is reliable.\n\n"
            "STEP 1 — Enable Developer Mode on the Roku (one time):\n"
            "  On the Roku remote press, in order:\n"
            "    Home ×3,  Up ×2,  Right, Left, Right, Left, Right\n"
            "  A 'Developer Settings' screen appears. Enable it, set a password,\n"
            "  and accept the agreement. The TV will reboot.\n\n"
            "STEP 2 — Enter that password below and click Install. The app uploads\n"
            "  the channel to your Roku automatically.\n"
        )
        tk.Label(f, text=steps, bg=BG, fg=MUTED, font=("Segoe UI", 9),
                 justify="left", wraplength=520).pack(anchor="w", pady=(8, 6))

        row = ttk.Frame(f); row.pack(fill="x", pady=(4, 2))
        tk.Label(row, text="Roku IP:", bg=BG, fg=FG,
                 font=("Segoe UI", 10)).pack(side="left")
        ip_var = tk.StringVar(value=ip or self._cfg.get("last_roku_ip", ""))
        tk.Entry(row, textvariable=ip_var, bg=CARD, fg=FG, insertbackground=FG,
                 relief="flat", highlightbackground=BORDER, highlightthickness=1,
                 width=18).pack(side="left", padx=(8, 0))

        row2 = ttk.Frame(f); row2.pack(fill="x", pady=(6, 2))
        tk.Label(row2, text="Dev password:", bg=BG, fg=FG,
                 font=("Segoe UI", 10)).pack(side="left")
        pw_var = tk.StringVar(value=self._cfg.get("roku_dev_password", ""))
        tk.Entry(row2, textvariable=pw_var, bg=CARD, fg=FG, insertbackground=FG,
                 relief="flat", highlightbackground=BORDER, highlightthickness=1,
                 width=24, show="•").pack(side="left", padx=(8, 0))

        status = tk.StringVar(value="")
        tk.Label(f, textvariable=status, bg=BG, fg=ACCENT, font=("Segoe UI", 9),
                 wraplength=520, justify="left").pack(anchor="w", pady=(10, 0))

        # Receiver mode selector
        tk.Label(f, text="When casting to Roku, use:", bg=BG, fg=FG,
                 font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(14, 2))
        mode_var = tk.StringVar(value=self._cfg.get("receiver_mode", "mychannel"))
        for val, txt in (("mychannel", "My TV App (recommended)"),
                         ("mediaplayer", "Roku Media Player (won't play these streams)"),
                         ("castify", "Castify channel (experimental)")):
            ttk.Radiobutton(f, text=txt, value=val, variable=mode_var).pack(anchor="w")

        def _save_mode():
            self._cfg["receiver_mode"] = mode_var.get()
            _save_config(self._cfg)

        def _install():
            ipx = ip_var.get().strip()
            pwx = pw_var.get().strip()
            if not ipx or not pwx:
                status.set("Enter both the Roku IP and the dev password.")
                return
            status.set("Building + uploading the channel…")
            dlg.update_idletasks()
            ok, msg = roku_deploy.sideload(ipx, pwx)
            if ok:
                self._cfg["roku_dev_password"] = pwx
                self._cfg["last_roku_ip"] = ipx
                _save_mode()
                _save_config(self._cfg)
                status.set("✓ " + msg + "  You can close this and Cast to TV.")
            else:
                status.set("✗ " + msg)

        btns = ttk.Frame(f); btns.pack(fill="x", pady=(16, 0))
        ttk.Button(btns, text="Install / Update channel", style="Primary.TButton",
                   command=_install).pack(side="right")
        ttk.Button(btns, text="Save choice", command=lambda: (_save_mode(), dlg.destroy())
                   ).pack(side="right", padx=(0, 8))

    # ── Proxy helpers / PC test ─────────────────────────────────────────────────

    def _proxied_url(self, raw_url: str, target_ip: str = "8.8.8.8"):
        """Return (cast_url, is_hls). HLS goes through the local proxy."""
        if not raw_url.startswith(("http://", "https://")):
            raw_url = "https://" + raw_url
        if ".m3u8" in raw_url.split("?", 1)[0].lower() and "/p.m3u8" not in raw_url:
            self._proxy.start(target_ip=target_ip)
            return self._proxy.url_for(raw_url, self._last_referer or ""), True
        return raw_url, False

    @staticmethod
    def _find_vlc() -> str | None:
        for p in (r"C:\Program Files\VideoLAN\VLC\vlc.exe",
                  r"C:\Program Files (x86)\VideoLAN\VLC\vlc.exe"):
            if os.path.exists(p):
                return p
        return None

    def _test_on_pc(self):
        """Play the proxied stream on THIS PC to confirm the proxy works,
        independent of whether the TV can reach it."""
        raw = self.url_var.get().strip()
        if not raw:
            messagebox.showwarning("No URL", "Find or paste a stream URL first.")
            return

        # Use the selected device's subnet if one is picked (so the proxy binds
        # the same LAN IP the TV would use); otherwise default route.
        target = "8.8.8.8"
        sel = self.tree.selection()
        if sel and self.devices.get(sel[0]):
            target = self.devices[sel[0]]["ip"]

        cast_url, is_hls = self._proxied_url(raw, target)
        if not is_hls:
            messagebox.showinfo("Not an HLS stream",
                                "This doesn't look like an .m3u8 stream, so there's "
                                "nothing to proxy. Test it directly instead.")
            return

        if not self._last_referer:
            self._set_tip("⚠ No Referer captured for this stream — use 🔍 Find "
                          ".m3u8 (not a pasted link) so the proxy can authenticate.")

        vlc = self._find_vlc()
        if vlc:
            try:
                subprocess.Popen([vlc, cast_url])
                self._set_status("Opened the proxied stream in VLC on this PC. "
                                 "If it plays here, the stream is good.", GREEN)
                self._set_tip("If it plays on the PC but NOT on the TV, the issue "
                              "is the TV reaching your PC — see the firewall note.")
                return
            except Exception as e:
                self._set_status(f"Couldn't launch VLC: {e}", RED)

        # No VLC — copy the proxied URL so the user can paste it anywhere.
        self.root.clipboard_clear()
        self.root.clipboard_append(cast_url)
        messagebox.showinfo(
            "Proxied URL copied",
            "VLC wasn't found in the usual location.\n\n"
            "The PROXIED url (the one that actually works) is now on your "
            "clipboard. In VLC: Media → Open Network Stream → paste → Play.\n\n"
            "⚠ Do NOT use the raw stream link — it will always fail "
            "(missing Referer + TLS block). Only this proxied link works:\n\n"
            f"{cast_url}"
        )

    # ── Cast ──────────────────────────────────────────────────────────────────

    def _cast(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showwarning("No Device Selected",
                                    "Please select a device from the list first.")
            return

        url = self.url_var.get().strip()
        if not url:
            messagebox.showwarning("No URL", "Please enter or paste a URL to cast.")
            return
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
            self.url_var.set(url)

        device_name = sel[0]   # iid == name
        dev = self.devices.get(device_name)
        if not dev:
            messagebox.showerror("Error", "Device not found. Please scan again.")
            return

        mode = self._cfg.get("receiver_mode", "mychannel")
        log.info("CAST start: device=%s ip=%s type=%s mode=%s referer=%r url=%s",
                 device_name, dev.get("ip"), dev.get("type"), mode,
                 self._last_referer, url[:120])

        # Save last device
        self._cfg["last_device"] = device_name
        _save_config(self._cfg)

        # HLS streams are header- and TLS-locked on most sites. Route them
        # through the local proxy, which re-fetches with a browser TLS
        # fingerprint and injects the captured Referer so the TV can play them.
        cast_url = url
        if ".m3u8" in url.split("?", 1)[0].lower() and "/p.m3u8" not in url:
            try:
                self._proxy.start(target_ip=dev["ip"])
                # Make sure the TV can actually reach us (one-time UAC prompt).
                if not getattr(self, "_fw_checked", False):
                    self._fw_checked = True
                    ensure_firewall_rule(self._proxy.port)
                cast_url = self._proxy.url_for(url, self._last_referer or "")
                log.info("Proxy started at %s -> cast_url=%s",
                         self._proxy.public_origin, cast_url[:90])
                self._set_tip("Routing this stream through the local proxy "
                              "(adds Referer + browser TLS so the TV can play it).")
            except Exception as e:
                log.exception("Proxy failed to start")
                self._set_tip(f"Proxy could not start ({e}); casting the raw URL.")

        self.cast_btn.configure(state="disabled", text="⏳  Casting…")
        self._set_status(f"Casting to {device_name}…", ORANGE)

        threading.Thread(
            target=self._cast_worker,
            args=(dev, cast_url, device_name),
            daemon=True,
        ).start()

    def _cast_worker(self, dev: dict, url: str, name: str):
        try:
            if dev["type"] == "Roku":
                mode = self._cfg.get("receiver_mode", "mychannel")
                fmt = "hls" if ".m3u8" in url.split("?", 1)[0].lower() else "mp4"
                if mode == "mychannel":
                    ok, msg = self._cast_via_channel(dev["ip"], url, fmt)
                elif mode == "mediaplayer":
                    ok, msg = _cast_roku(dev["ip"], url)
                else:
                    ok, msg = _cast_via_castify(dev["ip"], url, fmt)
            else:
                ok, msg = _cast_firetv(dev["ip"], url)
        except Exception as e:
            log.exception("Cast worker crashed")
            ok, msg = False, str(e)

        log.info("CAST result: ok=%s msg=%s", ok, msg)
        self.root.after(0, self._cast_done, ok, msg, name, dev)

    def _cast_via_channel(self, ip: str, url: str, fmt: str) -> tuple[bool, str]:
        """Play through the sideloaded 'PC Caster' dev channel."""
        pw = self._cfg.get("roku_dev_password")
        if not pw:
            log.warning("Channel cast aborted: no dev password saved")
            return False, "NO_DEV_PASSWORD"
        # Install on first use (or if it got removed).
        if not roku_deploy.is_installed(ip, pw):
            log.info("Dev channel not installed on %s — sideloading…", ip)
            ok, msg = roku_deploy.sideload(ip, pw)
            log.info("Sideload: ok=%s msg=%s", ok, msg)
            if not ok:
                return False, msg
            time.sleep(1.0)
        # If it's already running, push the new URL via /input; otherwise launch.
        ok, msg = roku_deploy.launch(ip, url, fmt)
        log.info("Channel launch: ok=%s msg=%s", ok, msg)
        return ok, msg

    def _cast_done(self, ok: bool, msg: str, name: str, dev: dict):
        self.cast_btn.configure(state="normal", text="▶   Cast to TV")

        if ok:
            self._set_status(f"✓  Now playing on {name} — {msg}", GREEN)
            self._set_tip("")
        elif msg == "ADB_MISSING":
            self._set_status("Fire TV requires ADB — see instructions below.", RED)
            self._show_adb_help(dev["ip"])
        elif msg == "ADB_ERROR":
            self._set_status("ADB found but returned an error. Try re-connecting.", RED)
        elif msg == "NO_DEV_PASSWORD":
            self._set_status("Your TV App isn't set up yet.", RED)
            messagebox.showinfo(
                "Set up your TV App first",
                "To cast through your own TV channel, set it up once:\n\n"
                "Click the '📺 TV App' button, follow the Developer-Mode steps, "
                "enter your Roku dev password, and Install.\n\n"
                "(Or open '📺 TV App' and switch the receiver mode.)"
            )
            self._setup_tv_app()
        else:
            self._set_status(f"✗  Cast failed: {msg or 'Unknown error'}", RED)
            tip = ""
            if dev["type"] == "Roku":
                tip = (
                    "Roku tip: Make sure 'Control by mobile apps' is enabled — "
                    "Settings → System → Advanced system settings → Control by mobile apps → Enabled."
                )
            self._set_tip(tip)
            messagebox.showerror(
                "Cast Failed",
                f"Could not send to {name}.\n\n"
                f"{msg or 'Check that the device is on and on the same Wi-Fi network.'}"
            )

    # ── Fire TV ADB Help ──────────────────────────────────────────────────────

    def _show_adb_help(self, ip: str):
        messagebox.showinfo(
            "Fire TV — One-Time ADB Setup",
            "To cast to Fire TV from your PC you need ADB.\n"
            "This is a one-time setup:\n\n"
            "── On your Fire TV ──────────────────────────\n"
            "1.  Settings → My Fire TV → About\n"
            "    Click 'Build' 7 times to unlock Developer Options.\n\n"
            "2.  Settings → My Fire TV → Developer Options\n"
            "    • ADB Debugging → ON\n"
            "    • Apps from Unknown Sources → ON\n\n"
            "── On your Windows PC ───────────────────────\n"
            "3.  Download 'Platform Tools' (ADB) from:\n"
            "    https://developer.android.com/tools/releases/platform-tools\n\n"
            "4.  Extract the zip somewhere (e.g. C:\\adb)\n\n"
            "5.  Add that folder to your PATH:\n"
            "    Search 'Environment Variables' → Path → New → C:\\adb\n\n"
            "6.  Reopen this app and cast again.\n"
            f"    Your Fire TV IP is: {ip}\n\n"
            "A prompt will appear on the TV the first time — allow the connection."
        )


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    root = tk.Tk()
    root.tk.call("tk", "scaling", 1.0)
    app = PCCaster(root)   # window/taskbar icon set inside __init__
    root.mainloop()


if __name__ == "__main__":
    main()
