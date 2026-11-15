#!/usr/bin/env python3
"""MacDiskInfo – ein CrystalDiskInfo-ähnliches Festplatten-Gesundheitstool für macOS.

Datenquellen:
  * `diskutil` (plist)  – Laufwerksliste, Modell, Größe, Anschluss, Volumes
  * `smartctl -a -j`    – S.M.A.R.T.-Werte (smartmontools; wird von build_app.sh ins
                          App-Bundle kopiert, sonst Homebrew-Installation)
  * `mount`             – eingehängte Volumes je Laufwerk

Bewertung angelehnt an CrystalDiskInfo:
  NVMe: Schlecht  = SMART fehlgeschlagen oder Critical-Warning-Bit gesetzt
        Vorsicht  = Media-Fehler > 0, Reserve <= Schwelle oder Verschleiß >= 90 %
        Gut       = sonst, Gesundheit = 100 % - "Percentage Used"
  ATA:  Schlecht  = SMART fehlgeschlagen oder ein Attribut <= Schwellwert
        Vorsicht  = Rohwert von 05/C5/C6 (reallokierte/schwebende/unkorrigierbare Sektoren) > 0
        Gut       = sonst

USB-Laufwerke: macOS reicht SMART-Befehle über USB-Gehäuse nicht durch. Über
"Funktion -> USB-Laufwerke direkt lesen" übernimmt usbsmart.py (libusb, Admin-Rechte)
die USB-Box kurz selbst und liest SAT- bzw. Realtek-NVMe-Daten; die Ergebnisse werden
pro USB-Gerät (Vendor, Product, locationID) zwischengespeichert und bei jeder
Aktualisierung den Laufwerken wieder zugeordnet.
"""

import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

# Start mit "python3 -I" (App-Bundle) lässt den Skriptordner aus sys.path weg – usbsmart.py liegt daneben
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import usbsmart  # noqa: E402

APP_NAME = "MacDiskInfo"
VERSION = "1.0"

# --- Farben im Stil von CrystalDiskInfo -------------------------------------
BG = "#f3f3f3"
PANEL = "#ffffff"
BORDER = "#c8c8c8"
TEXT = "#1a1a1a"
MUTED = "#6b6b6b"
STATUS_COLORS = {
    "good": ("#3b8ee8", "#ffffff"),     # blau wie bei CDI
    "caution": ("#f2c230", "#1a1a1a"),
    "bad": ("#e04343", "#ffffff"),
    "unknown": ("#9a9a9a", "#ffffff"),
}
STATUS_LABELS = {"good": "Gut", "caution": "Vorsicht", "bad": "Schlecht", "unknown": "Unbekannt"}

TEMP_CAUTION = 50   # °C
TEMP_BAD = 60       # °C

ATA_CRITICAL_IDS = {5, 197, 198}


# --- Hilfsfunktionen ---------------------------------------------------------
BUNDLED_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "smartmontools")


def find_smartctl():
    # 1. mitgeliefertes smartctl im App-Bundle (Resources/smartmontools), 2. System/Homebrew
    bundled = os.path.join(BUNDLED_DIR, "smartctl")
    if os.access(bundled, os.X_OK):
        return bundled
    found = shutil.which("smartctl")
    if found:
        return found
    for path in ("/opt/homebrew/bin/smartctl", "/usr/local/bin/smartctl", "/opt/local/sbin/smartctl"):
        if os.path.exists(path):
            return path
    return None


def run(cmd, timeout=20):
    try:
        res = subprocess.run(cmd, capture_output=True, timeout=timeout)
        return res.stdout
    except (OSError, subprocess.TimeoutExpired):
        return b""


def diskutil_plist(verb, *args):
    # -plist muss direkt hinter dem Verb stehen (z. B. "diskutil list -plist physical")
    out = run(["/usr/sbin/diskutil", *verb.split(), "-plist", *args])
    try:
        return plistlib.loads(out) if out else {}
    except Exception:
        return {}


def fmt_bytes(n, decimal=True):
    if n is None:
        return "----"
    base = 1000.0 if decimal else 1024.0
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    value = float(n)
    for unit in units:
        if value < base or unit == units[-1]:
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= base
    return f"{value:.1f} PB"


def fmt_int(n):
    return "----" if n is None else f"{n:,}".replace(",", ".")


def mount_map():
    """/dev/diskXsY -> Mountpoint, aus `mount`."""
    result = {}
    for line in run(["/sbin/mount"]).decode(errors="replace").splitlines():
        m = re.match(r"^/dev/(disk\d+s\d+(?:s\d+)?) on (.+?) \(", line)
        if m:
            result[m.group(1)] = m.group(2)
    return result


def volumes_for_disk(disk_id, all_parts, apfs, mounts):
    """Sammelt (Name, Mountpoint) aller Volumes, die physisch auf disk_id liegen."""
    vols = []
    parts = []
    for entry in all_parts:
        if entry.get("DeviceIdentifier") == disk_id:
            parts = [p.get("DeviceIdentifier") for p in entry.get("Partitions", [])]
            for p in entry.get("Partitions", []):
                if p.get("MountPoint"):
                    vols.append((p.get("VolumeName") or p["DeviceIdentifier"], p["MountPoint"]))
    for container in apfs.get("Containers", []):
        stores = {s.get("DeviceIdentifier") for s in container.get("PhysicalStores", [])}
        if stores & set(parts):
            for v in container.get("Volumes", []):
                mp = mounts.get(v.get("DeviceIdentifier", ""))
                if mp:
                    vols.append((v.get("Name", v.get("DeviceIdentifier")), mp))
    # Duplikate entfernen, Reihenfolge behalten
    seen, unique = set(), []
    for name, mp in vols:
        # interne macOS-Systemvolumes ausblenden, nur das Datenvolume zeigen
        if mp.startswith("/System/Volumes/") and mp != "/System/Volumes/Data":
            continue
        if mp == "/System/Volumes/Data":
            mp = "/ (Macintosh HD)"
        if mp not in seen:
            seen.add(mp)
            unique.append((name, mp))
    return unique


# --- Datenmodell -------------------------------------------------------------
def evaluate_health(disk):
    """Setzt disk['status'] und disk['health_pct'] nach den Regeln im Modul-Docstring."""
    s = disk.get("smart")
    if not s:
        disk["status"], disk["health_pct"] = "unknown", None
        return
    passed = s.get("smart_status", {}).get("passed")
    nvme = s.get("nvme_smart_health_information_log")
    if nvme is not None:
        used = nvme.get("percentage_used")
        disk["health_pct"] = max(0, 100 - used) if used is not None else None
        if passed is False or nvme.get("critical_warning", 0) != 0:
            disk["status"] = "bad"
        elif (nvme.get("media_errors", 0) > 0
              or nvme.get("available_spare", 100) < nvme.get("available_spare_threshold", 0)
              or (used is not None and used >= 90)):
            disk["status"] = "caution"
        else:
            disk["status"] = "good"
        return
    table = s.get("ata_smart_attributes", {}).get("table", [])
    disk["health_pct"] = None
    if passed is False:
        disk["status"] = "bad"
        return
    status = "good" if (passed is True or table) else "unknown"
    for attr in table:
        thresh = attr.get("thresh", 0)
        if thresh and attr.get("value", 255) <= thresh:
            disk["status"] = "bad"
            return
        if attr.get("id") in ATA_CRITICAL_IDS and attr.get("raw", {}).get("value", 0) > 0:
            status = "caution"
    disk["status"] = status


def collect_disks(smartctl, usb_results=None):
    """usb_results: {(vid, pid, locationID): Eintrag aus usbsmart.scan()} – Daten aus dem USB-Direktlesen."""
    usb_by_disk = {}
    if usb_results:
        for key, disk_id in usbsmart.usb_disk_map().items():
            if key in usb_results:
                usb_by_disk[disk_id] = usb_results[key]
    listing = diskutil_plist("list", "physical")
    all_parts = listing.get("AllDisksAndPartitions", [])
    apfs = diskutil_plist("apfs list")
    mounts = mount_map()
    disks = []
    for disk_id in listing.get("WholeDisks", []):
        info = diskutil_plist("info", disk_id)
        disk = {
            "id": disk_id,
            "model": (info.get("MediaName") or info.get("IORegistryEntryName") or disk_id).strip(),
            "size": info.get("TotalSize") or info.get("Size"),
            "bus": info.get("BusProtocol", "?"),
            "internal": info.get("Internal", False),
            "ssd": info.get("SolidState"),
            "removable": info.get("RemovableMediaOrExternalDevice", info.get("Removable", False)),
            "volumes": volumes_for_disk(disk_id, all_parts, apfs, mounts),
            "smart": None,
            "smart_error": None,
            "smart_source": None,
            "usb_direct": None,
        }
        if smartctl:
            cmd = [smartctl, "-a", "-j", disk_id]
            drivedb = os.path.join(BUNDLED_DIR, "drivedb.h")
            if smartctl.startswith(BUNDLED_DIR) and os.path.exists(drivedb):
                cmd[1:1] = ["-B", drivedb]
            out = run(cmd, timeout=30)
            try:
                data = json.loads(out) if out else {}
            except ValueError:
                data = {}
            if data.get("model_name") or data.get("smart_status") or data.get("ata_smart_attributes"):
                disk["smart"] = data
            else:
                msgs = data.get("smartctl", {}).get("messages", [])
                disk["smart_error"] = msgs[0]["string"] if msgs else "Keine SMART-Antwort"
        else:
            disk["smart_error"] = "smartctl nicht gefunden (brew install smartmontools)"
        usb = usb_by_disk.get(disk_id)
        if disk["smart"] is None and usb:
            disk["usb_direct"] = usb
            if usb.get("smart"):
                disk["smart"] = usb["smart"]
                disk["smart_source"] = "USB direkt, Stand " + usb.get("_time", "?")
        evaluate_health(disk)
        disks.append(disk)
    return disks


def temperature(disk):
    s = disk.get("smart") or {}
    t = s.get("temperature", {}).get("current")
    if t is None and s.get("nvme_smart_health_information_log"):
        t = s["nvme_smart_health_information_log"].get("temperature")
    return t


def temp_status(t):
    if t is None:
        return "unknown"
    if t >= TEMP_BAD:
        return "bad"
    if t >= TEMP_CAUTION:
        return "caution"
    return "good"


def info_rows(disk):
    """Felder rechts oben, wie bei CrystalDiskInfo."""
    s = disk.get("smart") or {}
    nvme = s.get("nvme_smart_health_information_log")
    reads = writes = None
    if nvme:
        if nvme.get("data_units_read") is not None:
            reads = nvme["data_units_read"] * 512000
            writes = nvme.get("data_units_written", 0) * 512000
    else:
        for attr in s.get("ata_smart_attributes", {}).get("table", []):
            name = attr.get("name", "")
            if name == "Total_LBAs_Written" or (attr.get("id") == 241 and writes is None):
                writes = attr["raw"]["value"] * 512
            elif name == "Total_LBAs_Read" or (attr.get("id") == 242 and reads is None):
                reads = attr["raw"]["value"] * 512

    if nvme is not None:
        standard = "NVM Express " + s.get("nvme_version", {}).get("string", "")
        interface = "NVMe" + (" (intern)" if disk["internal"] else " (extern)")
        transfer = "PCIe" if disk["internal"] else disk["bus"]
    elif s:
        standard = s.get("ata_version", {}).get("string", "----")
        interface = s.get("device", {}).get("protocol", disk["bus"])
        sv = s.get("interface_speed", {})
        transfer = (sv.get("current", {}).get("string", "----") + " | "
                    + sv.get("max", {}).get("string", "----")) if sv else "----"
    else:
        standard, interface, transfer = "----", disk["bus"], "----"

    features = []
    if s.get("smart_support", {}).get("available") or nvme is not None:
        features.append("S.M.A.R.T.")
    if s.get("nvme_optional_nvm_commands", {}).get("dataset_management") or s.get("trim", {}).get("supported"):
        features.append("TRIM")
    if s.get("nvme_optional_admin_commands", {}).get("self_test"):
        features.append("Selbsttest")
    if disk.get("ssd"):
        features.append("SSD")

    volumes = ", ".join(mp for _, mp in disk["volumes"]) or "----"
    poh = s.get("power_on_time", {}).get("hours")
    return [
        ("Firmware", s.get("firmware_version", "----")),
        ("Seriennummer", s.get("serial_number", "----")),
        ("Schnittstelle", interface),
        ("Übertragung", transfer),
        ("Volumes", volumes),
        ("Standard", standard),
        ("Funktionen", ", ".join(features) or "----"),
        ("Gesamt gelesen", fmt_bytes(reads)),
        ("Gesamt geschrieben", fmt_bytes(writes)),
        ("Einschaltvorgänge", fmt_int(s.get("power_cycle_count")) + (" mal" if s.get("power_cycle_count") is not None else "")),
        ("Betriebsstunden", fmt_int(poh) + (" Std." if poh is not None else "")),
    ]


NVME_ATTRS = [
    ("01", "Kritische Warnungen", "critical_warning"),
    ("02", "Temperatur (°C)", "temperature"),
    ("03", "Verfügbare Reserve (%)", "available_spare"),
    ("04", "Reserve-Schwellwert (%)", "available_spare_threshold"),
    ("05", "Verschleiß / Percentage Used (%)", "percentage_used"),
    ("06", "Gelesene Dateneinheiten", "data_units_read"),
    ("07", "Geschriebene Dateneinheiten", "data_units_written"),
    ("08", "Host-Lesebefehle", "host_reads"),
    ("09", "Host-Schreibbefehle", "host_writes"),
    ("0A", "Controller-Beschäftigungszeit (min)", "controller_busy_time"),
    ("0B", "Einschaltvorgänge", "power_cycles"),
    ("0C", "Betriebsstunden", "power_on_hours"),
    ("0D", "Unsichere Abschaltungen", "unsafe_shutdowns"),
    ("0E", "Medien-/Datenintegritätsfehler", "media_errors"),
    ("0F", "Fehlerprotokoll-Einträge", "num_err_log_entries"),
]


def attribute_rows(disk):
    """(Status, ID, Name, Aktuell, Schlechtester, Schwellwert, Rohwert) bzw. NVMe-Variante."""
    s = disk.get("smart") or {}
    nvme = s.get("nvme_smart_health_information_log")
    rows = []
    if nvme is not None:
        for aid, name, key in NVME_ATTRS:
            if key not in nvme:
                continue
            val = nvme[key]
            st = "good"
            if key == "critical_warning" and val:
                st = "bad"
            elif key == "media_errors" and val:
                st = "caution"
            elif key == "percentage_used" and val >= 90:
                st = "caution"
            elif key == "available_spare" and val < nvme.get("available_spare_threshold", 0):
                st = "caution"
            rows.append((st, aid, name, "", "", "", f"{val:,}".replace(",", ".")))
        return "nvme", rows
    for attr in s.get("ata_smart_attributes", {}).get("table", []):
        st = "good"
        thresh = attr.get("thresh", 0)
        raw = attr.get("raw", {})
        if thresh and attr.get("value", 255) <= thresh:
            st = "bad"
        elif attr.get("id") in ATA_CRITICAL_IDS and raw.get("value", 0) > 0:
            st = "caution"
        rows.append((st, f"{attr.get('id', 0):02X}", attr.get("name", "").replace("_", " "),
                     attr.get("value", ""), attr.get("worst", ""), thresh,
                     raw.get("string", str(raw.get("value", "")))))
    return "ata", rows


# --- GUI ---------------------------------------------------------------------
class App:
    def __init__(self, root):
        self.root = root
        self.smartctl = find_smartctl()
        self.disks = []
        self.usb_results = {}
        self.selected = 0
        self.auto_refresh = tk.IntVar(value=0)
        self._after_id = None
        self._loading = False

        root.title(f"{APP_NAME} {VERSION}")
        root.configure(bg=BG)
        root.geometry("860x760")
        root.minsize(720, 600)

        self._build_menu()
        self._build_style()

        self.diskbar = tk.Frame(root, bg=BG)
        self.diskbar.pack(fill="x", padx=10, pady=(10, 4))

        self.title_lbl = tk.Label(root, text="Laufwerke werden gelesen …", bg=BG, fg=TEXT,
                                  font=("Helvetica", 17, "bold"))
        self.title_lbl.pack(fill="x", padx=12, pady=(4, 6))

        top = tk.Frame(root, bg=BG)
        top.pack(fill="x", padx=10)

        left = tk.Frame(top, bg=BG)
        left.pack(side="left", fill="y", padx=(0, 10))
        tk.Label(left, text="Zustand", bg=BG, fg=MUTED, font=("Helvetica", 11)).pack(anchor="w")
        self.health_box = tk.Label(left, text="", width=14, height=3, font=("Helvetica", 18, "bold"),
                                   bg=STATUS_COLORS["unknown"][0], fg="white", relief="flat")
        self.health_box.pack(pady=(2, 10))
        tk.Label(left, text="Temperatur", bg=BG, fg=MUTED, font=("Helvetica", 11)).pack(anchor="w")
        self.temp_box = tk.Label(left, text="-- °C", width=14, height=2, font=("Helvetica", 18, "bold"),
                                 bg=STATUS_COLORS["unknown"][0], fg="white")
        self.temp_box.pack(pady=(2, 0))

        self.info = tk.Frame(top, bg=PANEL, highlightbackground=BORDER, highlightthickness=1)
        self.info.pack(side="left", fill="both", expand=True)
        self.info_values = {}
        for r, (label, _) in enumerate(info_rows({"smart": None, "internal": False, "bus": "", "volumes": []})):
            tk.Label(self.info, text=label, bg=PANEL, fg=MUTED, anchor="e",
                     font=("Helvetica", 12)).grid(row=r, column=0, sticky="e", padx=(10, 6), pady=2)
            val = tk.Label(self.info, text="----", bg=PANEL, fg=TEXT, anchor="w",
                           font=("Helvetica", 12, "bold"), wraplength=430, justify="left")
            val.grid(row=r, column=1, sticky="w", padx=(0, 10), pady=2)
            self.info_values[label] = val
        self.info.columnconfigure(1, weight=1)

        self.note = tk.Label(root, text="", bg=BG, fg="#a05a00", anchor="w", justify="left",
                             font=("Helvetica", 11), wraplength=780)
        self.note.pack(fill="x", padx=12, pady=(6, 0))
        self.usb_btn = ttk.Button(root, text="USB-Laufwerke direkt lesen …", command=self.read_usb)

        self.status = tk.Label(root, text="", bg=BG, fg=MUTED, anchor="w", font=("Helvetica", 10))
        self.status.pack(side="bottom", fill="x", padx=12, pady=(0, 8))

        table_frame = tk.Frame(root, bg=BG)
        table_frame.pack(fill="both", expand=True, padx=10, pady=(6, 4))
        cols = ("id", "name", "cur", "worst", "thresh", "raw")
        self.tree = ttk.Treeview(table_frame, columns=cols, show="headings", style="CDI.Treeview")
        headings = {"id": "ID", "name": "Attribut", "cur": "Aktuell", "worst": "Schlechtester",
                    "thresh": "Schwellwert", "raw": "Rohwert"}
        widths = {"id": 46, "name": 260, "cur": 70, "worst": 115, "thresh": 105, "raw": 150}
        for c in cols:
            self.tree.heading(c, text=headings[c])
            self.tree.column(c, width=widths[c], anchor="w" if c == "name" else "center",
                             stretch=(c in ("name", "raw")))
        for st, (bg, fg) in STATUS_COLORS.items():
            self.tree.tag_configure(st, foreground=TEXT)
        self.tree.tag_configure("caution", background="#fff4cc")
        self.tree.tag_configure("bad", background="#ffd9d9")
        sb = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        root.bind("<F5>", lambda e: self.refresh())
        root.bind("<Command-r>", lambda e: self.refresh())
        root.bind("<Left>", lambda e: self.select((self.selected - 1) % max(1, len(self.disks))))
        root.bind("<Right>", lambda e: self.select((self.selected + 1) % max(1, len(self.disks))))
        self.refresh()

    def _build_style(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("CDI.Treeview", background=PANEL, fieldbackground=PANEL, foreground=TEXT,
                        rowheight=22, font=("Helvetica", 12))
        style.configure("CDI.Treeview.Heading", font=("Helvetica", 11, "bold"))
        style.map("CDI.Treeview", background=[("selected", "#cfe3fb")], foreground=[("selected", TEXT)])

    def _build_menu(self):
        menubar = tk.Menu(self.root)
        fm = tk.Menu(menubar, tearoff=0)
        fm.add_command(label="Aktualisieren", accelerator="Cmd+R", command=self.refresh)
        fm.add_command(label="Bericht kopieren", command=self.copy_report)
        menubar.add_cascade(label="Datei", menu=fm)
        om = tk.Menu(menubar, tearoff=0)
        for label, minutes in (("Aus", 0), ("1 Minute", 1), ("5 Minuten", 5), ("10 Minuten", 10), ("30 Minuten", 30)):
            om.add_radiobutton(label=f"Auto-Aktualisierung: {label}", value=minutes,
                               variable=self.auto_refresh, command=self._schedule)
        om.add_separator()
        om.add_command(label="USB-Laufwerke direkt lesen (Admin) …", command=self.read_usb)
        menubar.add_cascade(label="Funktion", menu=om)
        self.root.config(menu=menubar)

    def _schedule(self):
        if self._after_id:
            self.root.after_cancel(self._after_id)
            self._after_id = None
        minutes = self.auto_refresh.get()
        if minutes:
            self._after_id = self.root.after(minutes * 60_000, self._auto_tick)

    def _auto_tick(self):
        self.refresh()
        self._schedule()

    def refresh(self):
        if self._loading:
            return
        self._loading = True
        self.status.config(text="Lese S.M.A.R.T.-Daten …")

        def worker():
            disks = collect_disks(self.smartctl, self.usb_results)
            self.root.after(0, lambda: self._on_loaded(disks))

        threading.Thread(target=worker, daemon=True).start()

    def _on_loaded(self, disks):
        self._loading = False
        self.disks = disks
        self.selected = min(self.selected, max(0, len(disks) - 1))
        self._render_diskbar()
        self.select(self.selected)
        if not self.smartctl:
            src = "smartctl fehlt"
        elif self.smartctl.startswith(BUNDLED_DIR):
            src = "smartctl (mitgeliefert)"
        else:
            src = self.smartctl
        self.status.config(text=f"{len(disks)} Laufwerk(e) · Quelle: diskutil + {src} · "
                                f"← → wechselt Laufwerk, Cmd+R aktualisiert")

    def _render_diskbar(self):
        for w in self.diskbar.winfo_children():
            w.destroy()
        for i, d in enumerate(self.disks):
            bg, fg = STATUS_COLORS[d["status"]]
            t = temperature(d)
            frame = tk.Frame(self.diskbar, bg=PANEL, highlightthickness=2,
                             highlightbackground=("#1a1a1a" if i == self.selected else BORDER), cursor="hand2")
            frame.pack(side="left", padx=(0, 6))
            dot = tk.Label(frame, text=STATUS_LABELS[d["status"]], bg=bg, fg=fg,
                           font=("Helvetica", 10, "bold"), padx=6)
            dot.pack(fill="x")
            lbl = tk.Label(frame, text=f"{t} °C" if t is not None else "-- °C", bg=PANEL, fg=TEXT,
                           font=("Helvetica", 12, "bold"), padx=8)
            lbl.pack()
            sub = tk.Label(frame, text=d["id"], bg=PANEL, fg=MUTED, font=("Helvetica", 10), padx=8)
            sub.pack()
            for w in (frame, dot, lbl, sub):
                w.bind("<Button-1>", lambda e, idx=i: self.select(idx))

    def select(self, idx):
        if not self.disks:
            self.title_lbl.config(text="Keine Laufwerke gefunden")
            return
        self.selected = idx
        self._render_diskbar()
        d = self.disks[idx]
        self.title_lbl.config(text=f"{d['model']}  {fmt_bytes(d['size'])}")

        bg, fg = STATUS_COLORS[d["status"]]
        health = STATUS_LABELS[d["status"]]
        if d.get("health_pct") is not None:
            health += f"\n{d['health_pct']} %"
        self.health_box.config(text=health, bg=bg, fg=fg)

        t = temperature(d)
        tbg, tfg = STATUS_COLORS[temp_status(t)]
        self.temp_box.config(text=f"{t} °C" if t is not None else "-- °C", bg=tbg, fg=tfg)

        for label, value in info_rows(d):
            self.info_values[label].config(text=value)

        is_usb = d["bus"] == "USB"
        if d.get("smart") is None:
            hint = d.get("smart_error") or ""
            if is_usb:
                usb = d.get("usb_direct")
                if usb and usb.get("error"):
                    hint = f"USB-Direktlesen fehlgeschlagen: {usb['error']}."
                else:
                    hint = ("Kein S.M.A.R.T. über den macOS-USB-Treiber. Mit „USB-Laufwerke direkt lesen“ "
                            "spricht MacDiskInfo die USB-Box selbst an (Admin-Passwort nötig).")
            self.note.config(text=hint)
        else:
            msgs = [m["string"] for m in d["smart"].get("smartctl", {}).get("messages", [])
                    if "Error Information Log" not in m.get("string", "")]
            if d.get("smart_source"):
                msgs.insert(0, f"Quelle: {d['smart_source']}")
            self.note.config(text="; ".join(msgs))
        if is_usb:
            self.usb_btn.pack(after=self.note, anchor="w", padx=12, pady=(4, 0))
        else:
            self.usb_btn.pack_forget()

        kind, rows = attribute_rows(d)
        self.tree.delete(*self.tree.get_children())
        if kind == "nvme":
            self.tree.heading("cur", text="")
            self.tree.heading("worst", text="")
            self.tree.heading("thresh", text="")
        else:
            self.tree.heading("cur", text="Aktuell")
            self.tree.heading("worst", text="Schlechtester")
            self.tree.heading("thresh", text="Schwellwert")
        for st, *vals in rows:
            self.tree.insert("", "end", values=vals, tags=(st,))

    def read_usb(self):
        """USB-Laufwerke über usbsmart.py lesen – mit macOS-Admin-Dialog (osascript)."""
        if self._loading:
            return
        usb_disks = [d for d in self.disks if d["bus"] == "USB"]
        if not usb_disks:
            messagebox.showinfo(APP_NAME, "Keine USB-Laufwerke gefunden.")
            return
        vols = [mp for d in usb_disks for _, mp in d["volumes"]]
        text = ("MacDiskInfo übernimmt jede USB-Box für ein paar Sekunden selbst und gibt sie danach "
                "an macOS zurück.\n\n")
        if vols:
            text += ("Dabei werden diese Volumes kurz ausgehängt und wieder eingehängt:\n  "
                     + "\n  ".join(vols) + "\n\nBitte vorher Dateien darauf schließen und laufende "
                     "Backups abwarten. ")
        text += "Ist ein Laufwerk in Benutzung, wird es übersprungen.\n\nFortfahren?"
        if not messagebox.askokcancel("USB-Laufwerke direkt lesen", text, icon="warning"):
            return
        self._loading = True
        self.status.config(text="Lese USB-Laufwerke direkt … (Admin-Passwort im macOS-Dialog eingeben)")
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "usbsmart.py")
        shell = f"'{sys.executable}' -I '{script}'"
        osa = ('do shell script "' + shell.replace('"', '\\"') + '" with administrator privileges '
               'with prompt "MacDiskInfo möchte S.M.A.R.T.-Daten von USB-Laufwerken lesen."')

        def worker():
            res = subprocess.run(["/usr/bin/osascript", "-e", osa], capture_output=True, text=True)
            self.root.after(0, lambda: self._on_usb_done(res))

        threading.Thread(target=worker, daemon=True).start()

    def _on_usb_done(self, res):
        self._loading = False
        if res.returncode != 0:
            msg = "abgebrochen" if "-128" in res.stderr else (res.stderr.strip() or "unbekannter Fehler")
            self.status.config(text=f"USB-Direktlesen: {msg}")
            return
        try:
            data = json.loads(res.stdout)
        except ValueError:
            self.status.config(text="USB-Direktlesen: ungültige Antwort")
            return
        if "_error" in data:
            self.status.config(text=f"USB-Direktlesen: {data['_error']}")
            return
        stamp = time.strftime("%H:%M")
        ok = 0
        for entry in data.values():
            entry["_time"] = stamp
            self.usb_results[tuple(entry["usb_key"])] = entry
            ok += bool(entry.get("smart"))
        self.refresh()
        self.root.after(1500, lambda: self.status.config(
            text=f"USB-Direktlesen: {ok} von {len(data)} USB-Laufwerk(en) mit S.M.A.R.T. gelesen ({stamp})."))

    def copy_report(self):
        lines = [f"{APP_NAME} {VERSION}", ""]
        for d in self.disks:
            lines.append(f"== {d['id']}: {d['model']} ({fmt_bytes(d['size'])})")
            pct = f" ({d['health_pct']} %)" if d.get("health_pct") is not None else ""
            lines.append(f"Zustand: {STATUS_LABELS[d['status']]}{pct}")
            t = temperature(d)
            lines.append(f"Temperatur: {t if t is not None else '--'} °C")
            for label, value in info_rows(d):
                lines.append(f"{label}: {value}")
            _, rows = attribute_rows(d)
            for _st, aid, name, cur, worst, thresh, raw in rows:
                lines.append(f"  {aid} {name:<38} {cur!s:>5} {worst!s:>5} {thresh!s:>5} {raw}")
            lines.append("")
        self.root.clipboard_clear()
        self.root.clipboard_append("\n".join(lines))
        self.status.config(text="Bericht in die Zwischenablage kopiert.")


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
