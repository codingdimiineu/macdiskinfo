#!/usr/bin/env python3
"""usbsmart – liest S.M.A.R.T. von USB-Laufwerken auf macOS ohne Kernel-Treiber.

macOS reicht ATA/NVMe-Befehle nicht über seinen USB-Massenspeicher-Treiber durch.
Dieses Modul übernimmt das USB-Gerät daher kurzzeitig selbst (libusb, braucht root),
spricht das USB-Bulk-Only-Protokoll (BOT) und schickt die Befehle, die auch smartctl
unter Linux verwendet:

  * SATA-Bridges (ASMedia, JMicron, …): SAT – ATA PASS-THROUGH(16)/(12)
      IDENTIFY DEVICE, SMART READ DATA, SMART READ THRESHOLDS
  * Realtek RTL9210/9220 (NVMe): Vendor-CDB 0xE4 (wie smartctl "-d sntrealtek")
      NVMe Identify Controller, Get Log Page 0x02 (SMART/Health)

Ablauf pro Laufwerk: Volumes aushängen -> USB-Interface übernehmen (macOS-Treiber wird
abgelöst) -> Befehle -> Interface freigeben (macOS übernimmt wieder, Volumes kommen zurück).

Die Ausgabe ist JSON im Format von `smartctl -j`, damit MacDiskInfo sie direkt anzeigen kann.

Aufruf (als root):  sudo python3 usbsmart.py [--disk diskN] [--no-unmount] [--verbose] [--log DATEI]
                    sudo python3 usbsmart.py --release   (hängende USB-Boxen an macOS zurückgeben)
Ohne --disk werden alle USB-Massenspeicher mit BOT-Interface gelesen.
"""

import ctypes
import ctypes.util
import json
import os
import plistlib
import re
import struct
import subprocess
import sys

# --- libusb über ctypes -------------------------------------------------------
LIBUSB_PATHS = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "libusb", "libusb-1.0.0.dylib"),
    "/opt/homebrew/lib/libusb-1.0.0.dylib",
    "/usr/local/lib/libusb-1.0.0.dylib",
]


def load_libusb():
    for path in LIBUSB_PATHS + [ctypes.util.find_library("usb-1.0") or ""]:
        if path and os.path.exists(path):
            return ctypes.CDLL(path)
    raise OSError("libusb nicht gefunden (brew install libusb)")


class EndpointDesc(ctypes.Structure):
    _fields_ = [("bLength", ctypes.c_uint8), ("bDescriptorType", ctypes.c_uint8),
                ("bEndpointAddress", ctypes.c_uint8), ("bmAttributes", ctypes.c_uint8),
                ("wMaxPacketSize", ctypes.c_uint16), ("bInterval", ctypes.c_uint8),
                ("bRefresh", ctypes.c_uint8), ("bSynchAddress", ctypes.c_uint8),
                ("extra", ctypes.c_void_p), ("extra_length", ctypes.c_int)]


class InterfaceDesc(ctypes.Structure):
    _fields_ = [("bLength", ctypes.c_uint8), ("bDescriptorType", ctypes.c_uint8),
                ("bInterfaceNumber", ctypes.c_uint8), ("bAlternateSetting", ctypes.c_uint8),
                ("bNumEndpoints", ctypes.c_uint8), ("bInterfaceClass", ctypes.c_uint8),
                ("bInterfaceSubClass", ctypes.c_uint8), ("bInterfaceProtocol", ctypes.c_uint8),
                ("iInterface", ctypes.c_uint8), ("endpoint", ctypes.POINTER(EndpointDesc)),
                ("extra", ctypes.c_void_p), ("extra_length", ctypes.c_int)]


class Interface(ctypes.Structure):
    _fields_ = [("altsetting", ctypes.POINTER(InterfaceDesc)), ("num_altsetting", ctypes.c_int)]


class ConfigDesc(ctypes.Structure):
    _fields_ = [("bLength", ctypes.c_uint8), ("bDescriptorType", ctypes.c_uint8),
                ("wTotalLength", ctypes.c_uint16), ("bNumInterfaces", ctypes.c_uint8),
                ("bConfigurationValue", ctypes.c_uint8), ("iConfiguration", ctypes.c_uint8),
                ("bmAttributes", ctypes.c_uint8), ("MaxPower", ctypes.c_uint8),
                ("interface", ctypes.POINTER(Interface)),
                ("extra", ctypes.c_void_p), ("extra_length", ctypes.c_int)]


class DeviceDesc(ctypes.Structure):
    _fields_ = [("bLength", ctypes.c_uint8), ("bDescriptorType", ctypes.c_uint8),
                ("bcdUSB", ctypes.c_uint16), ("bDeviceClass", ctypes.c_uint8),
                ("bDeviceSubClass", ctypes.c_uint8), ("bDeviceProtocol", ctypes.c_uint8),
                ("bMaxPacketSize0", ctypes.c_uint8), ("idVendor", ctypes.c_uint16),
                ("idProduct", ctypes.c_uint16), ("bcdDevice", ctypes.c_uint16),
                ("iManufacturer", ctypes.c_uint8), ("iProduct", ctypes.c_uint8),
                ("iSerialNumber", ctypes.c_uint8), ("bNumConfigurations", ctypes.c_uint8)]


VERBOSE = "--verbose" in sys.argv
LOG_PATH = sys.argv[sys.argv.index("--log") + 1] if "--log" in sys.argv else None


def log(msg):
    line = "[usbsmart] " + msg
    if VERBOSE:
        print(line, file=sys.stderr, flush=True)
    if LOG_PATH:
        import time
        try:
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + line + "\n")
        except OSError:
            pass


LIBUSB_ERROR_PIPE = -9
LIBUSB_ERROR_TIMEOUT = -7


class USBError(Exception):
    pass


class BOTDevice:
    """Ein USB-Massenspeicher, über Bulk-Only-Transport direkt angesprochen."""

    def __init__(self, usb, ctx, dev):
        self.usb, self.ctx, self.dev = usb, ctx, dev
        self.handle = ctypes.c_void_p()
        self.iface = self.alt = self.ep_in = self.ep_out = None
        self.tag = 1
        self._find_bot_interface()

    def _find_bot_interface(self):
        cfg = ctypes.POINTER(ConfigDesc)()
        if self.usb.libusb_get_active_config_descriptor(self.dev, ctypes.byref(cfg)) != 0:
            if self.usb.libusb_get_config_descriptor(self.dev, 0, ctypes.byref(cfg)) != 0:
                raise USBError("Konfigurationsdeskriptor nicht lesbar")
        try:
            for i in range(cfg.contents.bNumInterfaces):
                itf = cfg.contents.interface[i]
                for a in range(itf.num_altsetting):
                    alt = itf.altsetting[a]
                    if (alt.bInterfaceClass, alt.bInterfaceSubClass, alt.bInterfaceProtocol) == (8, 6, 0x50):
                        for e in range(alt.bNumEndpoints):
                            ep = alt.endpoint[e]
                            if ep.bmAttributes & 3 == 2:  # bulk
                                if ep.bEndpointAddress & 0x80:
                                    self.ep_in = ep.bEndpointAddress
                                else:
                                    self.ep_out = ep.bEndpointAddress
                        self.iface, self.alt = alt.bInterfaceNumber, alt.bAlternateSetting
                        return
        finally:
            self.usb.libusb_free_config_descriptor(cfg)
        raise USBError("kein Bulk-Only-Interface (BOT) gefunden")

    def open(self):
        rc = self.usb.libusb_open(self.dev, ctypes.byref(self.handle))
        if rc != 0:
            raise USBError(f"libusb_open fehlgeschlagen ({rc}) – läuft das Skript als root?")
        self.usb.libusb_set_auto_detach_kernel_driver(self.handle, 1)
        rc = self.usb.libusb_claim_interface(self.handle, self.iface)
        if rc != 0:
            raise USBError(f"Interface nicht übernehmbar ({rc}) – root nötig, Volumes ausgehängt?")
        rc = self.usb.libusb_set_interface_alt_setting(self.handle, self.iface, self.alt)
        if rc != 0:
            raise USBError(f"Alt-Setting {self.alt} nicht setzbar ({rc})")
        log(f"Interface {self.iface}/Alt {self.alt} übernommen, EP in={self.ep_in:#x} out={self.ep_out:#x}")
        self.reset_recovery()
        # Testlauf: TEST UNIT READY (ohne Daten); zweimal, erster liefert oft "Unit Attention"
        for _ in range(2):
            try:
                st, _ = self.scsi_in([0, 0, 0, 0, 0, 0], 0)
                log(f"TEST UNIT READY -> Status {st}")
            except USBError as e:
                log(f"TEST UNIT READY fehlgeschlagen: {e}")
                self.reset_recovery()

    def reset_recovery(self):
        """Bulk-Only Mass Storage Reset + Clear-Halt beider Endpoints (USB MSC BOT 5.3.4)."""
        rc = self.usb.libusb_control_transfer(self.handle, 0x21, 0xFF, 0, self.iface, None, 0, 2000)
        rc_in = self.usb.libusb_clear_halt(self.handle, self.ep_in)
        rc_out = self.usb.libusb_clear_halt(self.handle, self.ep_out)
        log(f"BOT-Reset rc={rc}, clear_halt in={rc_in} out={rc_out}")

    def close(self):
        if self.handle:
            rc = self.usb.libusb_release_interface(self.handle, self.iface)
            self.usb.libusb_close(self.handle)
            self.handle = ctypes.c_void_p()
            log(f"Interface freigegeben rc={rc}")

    def _bulk(self, ep, buf, length, timeout=5000):
        done = ctypes.c_int(0)
        rc = self.usb.libusb_bulk_transfer(self.handle, ep, buf, length, ctypes.byref(done), timeout)
        return rc, done.value

    def _read_csw(self):
        csw = (ctypes.c_uint8 * 13)()
        rc, n = self._bulk(self.ep_in, csw, 13)
        if rc == LIBUSB_ERROR_PIPE:
            self.usb.libusb_clear_halt(self.handle, self.ep_in)
            rc, n = self._bulk(self.ep_in, csw, 13)
        if rc != 0 or n != 13 or bytes(csw[:4]) != b"USBS":
            raise USBError(f"ungültiger CSW (rc={rc}, n={n})")
        _sig, _tag, residue, status = struct.unpack("<4sIIB", bytes(csw))
        return status, residue

    def scsi_in(self, cdb, length):
        """SCSI-Befehl mit Daten vom Gerät; liefert (status, daten)."""
        cbw = struct.pack("<4sIIBBB", b"USBC", self.tag, length, 0x80 if length else 0, 0, len(cdb))
        cbw += bytes(cdb) + b"\0" * (16 - len(cdb))
        self.tag += 1
        out = (ctypes.c_uint8 * 31).from_buffer_copy(cbw)
        rc, _ = self._bulk(self.ep_out, out, 31)
        if rc != 0:
            raise USBError(f"CBW nicht gesendet ({rc})")
        data = b""
        if length:
            buf = (ctypes.c_uint8 * length)()
            rc, n = self._bulk(self.ep_in, buf, length, timeout=10000)
            log(f"CDB {bytes(cdb[:1]).hex()}: Datenphase rc={rc}, {n} Byte")
            if rc == LIBUSB_ERROR_PIPE:
                self.usb.libusb_clear_halt(self.handle, self.ep_in)
            elif rc != 0:
                self.reset_recovery()
                raise USBError(f"Datenphase für CDB 0x{cdb[0]:02x} fehlgeschlagen ({rc})")
            data = bytes(buf[:n])
        status, residue = self._read_csw()
        log(f"CDB {bytes(cdb[:1]).hex()}: CSW Status {status}, Residue {residue}")
        return status, data


# --- Befehle -----------------------------------------------------------------
def inquiry(d):
    st, data = d.scsi_in([0x12, 0, 0, 0, 36, 0], 36)
    if st != 0 or len(data) < 36:
        return None
    return {"vendor": data[8:16].decode(errors="replace").strip(),
            "product": data[16:32].decode(errors="replace").strip(),
            "revision": data[32:36].decode(errors="replace").strip()}


def ata_in(d, command, feature=0, count=1, lba_mid=0, lba_high=0, use12=False):
    """SAT ATA PASS-THROUGH, PIO Data-In, 512 Byte je Sektor."""
    if use12:
        cdb = [0xA1, 4 << 1, 0x0E, feature, count, 0, lba_mid, lba_high, 0, command, 0, 0]
    else:
        cdb = [0x85, 4 << 1, 0x0E, 0, feature, 0, count, 0, 0, 0, lba_mid, 0, lba_high, 0, command, 0]
    try:
        st, data = d.scsi_in(cdb, 512 * count)
    except USBError as e:
        log(f"ATA 0x{command:02x}/{feature:#04x} ({'12' if use12 else '16'}): {e}")
        return None
    return data if st == 0 and len(data) >= 512 and any(data) else None


def ata_string(words_bytes):
    swapped = bytearray()
    for i in range(0, len(words_bytes), 2):
        swapped += bytes([words_bytes[i + 1], words_bytes[i]])
    return swapped.decode(errors="replace").strip()


ATA_NAMES = {
    1: "Raw_Read_Error_Rate", 2: "Throughput_Performance", 3: "Spin_Up_Time", 4: "Start_Stop_Count",
    5: "Reallocated_Sector_Ct", 7: "Seek_Error_Rate", 8: "Seek_Time_Performance", 9: "Power_On_Hours",
    10: "Spin_Retry_Count", 11: "Calibration_Retry_Count", 12: "Power_Cycle_Count",
    160: "Uncorrectable_Sector_Count", 161: "Valid_Spare_Block_Count", 163: "Initial_Bad_Block_Count",
    164: "Total_Erase_Count", 165: "Max_Erase_Count", 166: "Min_Erase_Count", 167: "Average_Erase_Count",
    168: "Max_Erase_Count_of_Spec", 169: "Remaining_Lifetime_Perc", 170: "Available_Reservd_Space",
    171: "Program_Fail_Count", 172: "Erase_Fail_Count", 173: "Wear_Leveling_Count", 174: "Unexpect_Power_Loss_Ct",
    175: "Program_Fail_Count_Chip", 176: "Erase_Fail_Count_Chip", 177: "Wear_Leveling_Count",
    178: "Used_Rsvd_Blk_Cnt_Chip", 179: "Used_Rsvd_Blk_Cnt_Tot", 180: "Unused_Rsvd_Blk_Cnt_Tot",
    181: "Program_Fail_Cnt_Total", 182: "Erase_Fail_Count_Total", 183: "Runtime_Bad_Block",
    184: "End-to-End_Error", 187: "Reported_Uncorrect", 188: "Command_Timeout", 189: "High_Fly_Writes",
    190: "Airflow_Temperature_Cel", 191: "G-Sense_Error_Rate", 192: "Power-Off_Retract_Count",
    193: "Load_Cycle_Count", 194: "Temperature_Celsius", 195: "Hardware_ECC_Recovered",
    196: "Reallocated_Event_Count", 197: "Current_Pending_Sector", 198: "Offline_Uncorrectable",
    199: "UDMA_CRC_Error_Count", 200: "Multi_Zone_Error_Rate", 206: "Flying_Height",
    218: "CRC_Error_Count", 230: "Media_Wearout_Indicator", 231: "SSD_Life_Left", 232: "Available_Reservd_Space",
    233: "Media_Wearout_Indicator", 234: "Thermal_Throttle", 235: "Good_Block_Count",
    240: "Head_Flying_Hours", 241: "Total_LBAs_Written", 242: "Total_LBAs_Read",
    244: "Average_Erase_Count", 245: "Max_Erase_Count", 246: "Total_Erase_Count",
}


DRIVEDB_PATHS = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "smartmontools", "drivedb.h"),
    "/opt/homebrew/share/smartmontools/drivedb.h",
    "/usr/local/share/smartmontools/drivedb.h",
]


def _drivedb_entries(text):
    """Zerlegt drivedb.h in Einträge aus je 5 C-Strings (Familie, Modell-Regex, FW-Regex, Warnung, Presets)."""
    entries, fields, cur, i, n = [], None, None, 0, len(text)
    while i < n:
        c = text[i]
        if text.startswith("//", i):
            i = text.find("\n", i)
            i = n if i < 0 else i
            continue
        if text.startswith("/*", i):
            i = text.find("*/", i)
            i = n if i < 0 else i + 2
            continue
        if c == "{":
            fields, cur = [], None
        elif c == "}" and fields is not None:
            if cur is not None:
                fields.append(cur)
            if len(fields) == 5:
                entries.append(fields)
            fields = cur = None
        elif c == "," and fields is not None:
            fields.append(cur or "")
            cur = None
        elif c == '"' and fields is not None:
            j, buf = i + 1, []
            while j < n and text[j] != '"':
                if text[j] == "\\" and j + 1 < n:
                    buf.append(text[j + 1])
                    j += 2
                    continue
                buf.append(text[j])
                j += 1
            cur = (cur or "") + "".join(buf)
            i = j
        i += 1
    return entries


_DRIVEDB = None


def attribute_names(model, firmware=""):
    """Attributnamen {id: name} wie smartctl: DEFAULT-Eintrag + erster passender Modelleintrag."""
    global _DRIVEDB
    if _DRIVEDB is None:
        _DRIVEDB = []
        for path in DRIVEDB_PATHS:
            if os.path.exists(path):
                with open(path, encoding="utf-8", errors="replace") as f:
                    _DRIVEDB = _drivedb_entries(f.read())
                break
    names = dict(ATA_NAMES)

    def apply(presets):
        for m in re.finditer(r"-v\s+(\d+),([^,\s]+)(?:,([^\s,]+))?", presets):
            if m.group(3):
                names[int(m.group(1))] = m.group(3)

    for family, model_re, fw_re, _warn, presets in _DRIVEDB:
        if family.startswith("DEFAULT"):
            apply(presets)
            break
    for family, model_re, fw_re, _warn, presets in _DRIVEDB:
        if family.startswith(("VERSION", "DEFAULT", "USB:")) or not model_re:
            continue
        try:
            if re.fullmatch(model_re, model) and (not fw_re or re.fullmatch(fw_re, firmware)):
                apply(presets)
                break
        except re.error:
            continue
    return names


def read_sat(d):
    for use12 in (False, True):
        ident = ata_in(d, 0xEC, use12=use12)
        if ident:
            break
    else:
        return None, "Bridge beantwortet ATA PASS-THROUGH nicht (kein SAT)"
    words = struct.unpack("<256H", ident[:512])
    result = {
        "device": {"protocol": "ATA", "type": "sat (USB, direkt)"},
        "model_name": ata_string(ident[54:94]),
        "serial_number": ata_string(ident[20:40]),
        "firmware_version": ata_string(ident[46:54]),
        "smart_support": {"available": bool(words[82] & 1), "enabled": bool(words[85] & 1)},
        "trim": {"supported": bool(words[169] & 1)},
    }
    smart = ata_in(d, 0xB0, feature=0xD0, lba_mid=0x4F, lba_high=0xC2, use12=use12)
    if not smart:
        return result, "SMART READ DATA abgelehnt"
    thresh = ata_in(d, 0xB0, feature=0xD1, lba_mid=0x4F, lba_high=0xC2, use12=use12) or b"\0" * 512
    names = attribute_names(result["model_name"], result["firmware_version"])
    thresholds = {thresh[2 + i * 12]: thresh[3 + i * 12] for i in range(30) if thresh[2 + i * 12]}
    table, failed = [], False
    for i in range(30):
        e = smart[2 + i * 12: 14 + i * 12]
        aid = e[0]
        if not aid:
            continue
        flags = e[1] | e[2] << 8
        value, worst = e[3], e[4]
        raw = int.from_bytes(e[5:11], "little")
        raw_str = str(raw)
        if aid in (190, 194):
            raw_str = str(e[5])
        th = thresholds.get(aid, 0)
        if th and value <= th and flags & 1:
            failed = True
        table.append({"id": aid, "name": names.get(aid, "Unknown_Attribute"), "value": value,
                      "worst": worst, "thresh": th, "flags": {"value": flags},
                      "raw": {"value": raw, "string": raw_str}})
        if aid in (190, 194) and "temperature" not in result:
            result["temperature"] = {"current": e[5]}
        if aid == 9:
            result["power_on_time"] = {"hours": raw & 0xFFFFFFFF}
        if aid == 12:
            result["power_cycle_count"] = raw
    result["ata_smart_attributes"] = {"table": table}
    result["smart_status"] = {"passed": not failed}
    return result, None


def realtek_nvme(d, opcode, cdw10, size):
    cdb = [0xE4, size & 0xFF, size >> 8, opcode, cdw10 & 0xFF] + [0] * 11
    st, data = d.scsi_in(cdb, size)
    return data if st == 0 and data and any(data) else None


def le(b):
    return int.from_bytes(b, "little")


def read_realtek_nvme(d):
    ident = realtek_nvme(d, 0x06, 1, 4096)
    if not ident:
        return None, "Realtek-Bridge beantwortet NVMe Identify nicht"
    log = realtek_nvme(d, 0x02, 0x02, 512)
    result = {
        "device": {"protocol": "NVMe", "type": "sntrealtek (USB, direkt)"},
        "model_name": ident[24:64].decode(errors="replace").strip(),
        "serial_number": ident[4:24].decode(errors="replace").strip(),
        "firmware_version": ident[64:72].decode(errors="replace").strip(),
        "nvme_version": {"string": "%d.%d" % (ident[83] << 8 | ident[82], ident[81]) if any(ident[80:84]) else "<1.2"},
        "nvme_optional_nvm_commands": {"dataset_management": bool(le(ident[520:522]) & 4)},
        "nvme_optional_admin_commands": {"self_test": bool(le(ident[256:258]) & 0x10)},
        "smart_support": {"available": True, "enabled": True},
    }
    if not log:
        return result, "SMART/Health-Log nicht lesbar"
    temp_k = le(log[1:3])
    h = {
        "critical_warning": log[0],
        "temperature": temp_k - 273 if temp_k else 0,
        "available_spare": log[3],
        "available_spare_threshold": log[4],
        "percentage_used": log[5],
        "data_units_read": le(log[32:48]),
        "data_units_written": le(log[48:64]),
        "host_reads": le(log[64:80]),
        "host_writes": le(log[80:96]),
        "controller_busy_time": le(log[96:112]),
        "power_cycles": le(log[112:128]),
        "power_on_hours": le(log[128:144]),
        "unsafe_shutdowns": le(log[144:160]),
        "media_errors": le(log[160:176]),
        "num_err_log_entries": le(log[176:192]),
    }
    result.update({
        "nvme_smart_health_information_log": h,
        "smart_status": {"passed": h["critical_warning"] == 0},
        "temperature": {"current": h["temperature"]},
        "power_on_time": {"hours": h["power_on_hours"]},
        "power_cycle_count": h["power_cycles"],
    })
    return result, None


REALTEK_NVME = {(0x0BDA, 0x9210), (0x0BDA, 0x9220), (0x0BDA, 0x9221)}


# --- Zuordnung USB-Gerät <-> diskN -------------------------------------------
def usb_disk_map():
    """(vid, pid, locationID) -> diskN, aus der IORegistry."""
    out = subprocess.run(["/usr/sbin/ioreg", "-a", "-r", "-c", "IOUSBHostDevice", "-l"],
                         capture_output=True).stdout
    mapping = {}
    # manche Geräte liefern Steuerzeichen in Strings, die plistlib als ungültiges XML ablehnt
    out = re.sub(rb"&#(?:[0-8]|1[124-9]|2[0-9]|3[01]);|[\x00-\x08\x0b\x0c\x0e-\x1f]", b"", out)
    try:
        roots = plistlib.loads(out) if out else []
    except Exception:
        return mapping

    def is_usb_device(node):
        return "idVendor" in node and "locationID" in node and "bDeviceClass" in node

    def find_bsd(node):
        # nicht in verschachtelte USB-Geräte (Hubs -> Laufwerk) absteigen
        if node.get("Whole") and node.get("BSD Name", "").startswith("disk"):
            return node["BSD Name"]
        for child in node.get("IORegistryEntryChildren", []):
            if is_usb_device(child):
                continue
            r = find_bsd(child)
            if r:
                return r
        return None

    def visit(node):
        if is_usb_device(node):
            bsd = find_bsd(node)
            if bsd:
                mapping[(node.get("idVendor"), node.get("idProduct"), node.get("locationID"))] = bsd
        for child in node.get("IORegistryEntryChildren", []):
            visit(child)

    for dev in roots:
        visit(dev)
    return mapping


def unmount(disk):
    r = subprocess.run(["/usr/sbin/diskutil", "unmountDisk", disk], capture_output=True, text=True)
    return r.returncode == 0, (r.stdout + r.stderr).strip()


def wait_for_disk(usb_key, seconds):
    import time
    for _ in range(int(seconds * 2)):
        disk = usb_disk_map().get(usb_key)
        if disk:
            return disk
        time.sleep(0.5)
    return None


def recover(usb_key):
    """Notfall-Rückgabe: USB-Reset erzwingt eine Neuanmeldung, danach bindet macOS seinen Treiber wieder."""
    usb = load_libusb()
    setup(usb)
    ctx = ctypes.c_void_p()
    if usb.libusb_init(ctypes.byref(ctx)) != 0:
        return False
    lst = ctypes.POINTER(ctypes.c_void_p)()
    count = usb.libusb_get_device_list(ctx, ctypes.byref(lst))
    done = False
    try:
        for i in range(count):
            desc = DeviceDesc()
            usb.libusb_get_device_descriptor(lst[i], ctypes.byref(desc))
            if (desc.idVendor, desc.idProduct, location_id(usb, lst[i])) != tuple(usb_key):
                continue
            h = ctypes.c_void_p()
            if usb.libusb_open(lst[i], ctypes.byref(h)) == 0:
                rc = usb.libusb_reset_device(h)
                usb.libusb_close(h)
                log(f"Notfall-Reset {usb_key[0]:04x}:{usb_key[1]:04x} rc={rc}")
                done = True
    finally:
        usb.libusb_free_device_list(lst, 1)
        usb.libusb_exit(ctx)
    return done


def release_all():
    """Alle USB-Massenspeicher ohne macOS-Laufwerk (hängen nach Abbruch noch an libusb) zurückgeben."""
    usb = load_libusb()
    setup(usb)
    ctx = ctypes.c_void_p()
    usb.libusb_init(ctypes.byref(ctx))
    have_disk = set(usb_disk_map())
    lst = ctypes.POINTER(ctypes.c_void_p)()
    count = usb.libusb_get_device_list(ctx, ctypes.byref(lst))
    keys = []
    for i in range(count):
        desc = DeviceDesc()
        usb.libusb_get_device_descriptor(lst[i], ctypes.byref(desc))
        key = (desc.idVendor, desc.idProduct, location_id(usb, lst[i]))
        if key in have_disk:
            continue
        try:
            BOTDevice(usb, ctx, lst[i])
        except USBError:
            continue
        keys.append(key)
    usb.libusb_free_device_list(lst, 1)
    usb.libusb_exit(ctx)
    result = {}
    for key in keys:
        recover(key)
        disk = wait_for_disk(key, 15)
        if disk:
            remount(key, disk, timeout=1)
        result["%04x:%04x" % key[:2]] = disk or "nicht zurückgekehrt – bitte Kabel kurz abziehen"
    return result


def remount(usb_key, old_disk, timeout=25):
    """Nach der Rückgabe nummeriert macOS das Laufwerk oft neu (z. B. disk6 -> disk5).
    Wartet, bis das USB-Gerät wieder als Laufwerk auftaucht, und hängt es samt APFS-Volumes ein."""
    import time
    disk = wait_for_disk(usb_key, timeout)
    if not disk:
        log(f"{old_disk} nach {timeout}s nicht zurück – Notfall-Reset")
        recover(usb_key)
        disk = wait_for_disk(usb_key, 15)
    if not disk:
        log(f"{old_disk} ist nicht zu macOS zurückgekehrt")
        return None
    time.sleep(1.0)
    subprocess.run(["/usr/sbin/diskutil", "mountDisk", disk], capture_output=True)
    # APFS-Container, deren Physical Store auf diesem Laufwerk liegt, ebenfalls einhängen
    try:
        apfs = plistlib.loads(subprocess.run(["/usr/sbin/diskutil", "apfs", "list", "-plist"],
                                             capture_output=True).stdout)
    except Exception:
        apfs = {}
    for c in apfs.get("Containers", []):
        if any(st.get("DeviceIdentifier", "").startswith(disk + "s") for st in c.get("PhysicalStores", [])):
            subprocess.run(["/usr/sbin/diskutil", "mountDisk", c["ContainerReference"]], capture_output=True)
    log(f"wieder eingehängt als {disk}")
    return disk


# --- Hauptablauf -------------------------------------------------------------
def setup(usb):
    usb.libusb_init.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
    usb.libusb_get_device_list.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))]
    usb.libusb_get_device_list.restype = ctypes.c_ssize_t
    usb.libusb_free_device_list.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_int]
    usb.libusb_get_device_descriptor.argtypes = [ctypes.c_void_p, ctypes.POINTER(DeviceDesc)]
    usb.libusb_get_active_config_descriptor.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.POINTER(ConfigDesc))]
    usb.libusb_get_config_descriptor.argtypes = [ctypes.c_void_p, ctypes.c_uint8, ctypes.POINTER(ctypes.POINTER(ConfigDesc))]
    usb.libusb_free_config_descriptor.argtypes = [ctypes.POINTER(ConfigDesc)]
    usb.libusb_get_bus_number.argtypes = [ctypes.c_void_p]
    usb.libusb_get_bus_number.restype = ctypes.c_uint8
    usb.libusb_get_port_numbers.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint8), ctypes.c_int]
    usb.libusb_open.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    usb.libusb_close.argtypes = [ctypes.c_void_p]
    usb.libusb_set_auto_detach_kernel_driver.argtypes = [ctypes.c_void_p, ctypes.c_int]
    usb.libusb_claim_interface.argtypes = [ctypes.c_void_p, ctypes.c_int]
    usb.libusb_release_interface.argtypes = [ctypes.c_void_p, ctypes.c_int]
    usb.libusb_set_interface_alt_setting.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
    usb.libusb_clear_halt.argtypes = [ctypes.c_void_p, ctypes.c_uint8]
    usb.libusb_bulk_transfer.argtypes = [ctypes.c_void_p, ctypes.c_uint8, ctypes.POINTER(ctypes.c_uint8),
                                         ctypes.c_int, ctypes.POINTER(ctypes.c_int), ctypes.c_uint]
    usb.libusb_control_transfer.argtypes = [ctypes.c_void_p, ctypes.c_uint8, ctypes.c_uint8, ctypes.c_uint16,
                                            ctypes.c_uint16, ctypes.c_void_p, ctypes.c_uint16, ctypes.c_uint]
    usb.libusb_reset_device.argtypes = [ctypes.c_void_p]
    usb.libusb_exit.argtypes = [ctypes.c_void_p]


def location_id(usb, dev):
    """macOS-locationID aus Bus + Portpfad nachbilden (0xBBPPPP..)."""
    ports = (ctypes.c_uint8 * 8)()
    n = usb.libusb_get_port_numbers(dev, ports, 8)
    loc = usb.libusb_get_bus_number(dev) << 24
    for i in range(max(0, n)):
        loc |= (ports[i] & 0xF) << (20 - 4 * i)
    return loc


def scan(only_disk=None, do_unmount=True):
    usb = load_libusb()
    setup(usb)
    ctx = ctypes.c_void_p()
    if usb.libusb_init(ctypes.byref(ctx)) != 0:
        raise USBError("libusb_init fehlgeschlagen")
    disks = usb_disk_map()
    results = {}
    lst = ctypes.POINTER(ctypes.c_void_p)()
    count = usb.libusb_get_device_list(ctx, ctypes.byref(lst))
    try:
        for i in range(count):
            dev = lst[i]
            desc = DeviceDesc()
            usb.libusb_get_device_descriptor(dev, ctypes.byref(desc))
            loc = location_id(usb, dev)
            disk = disks.get((desc.idVendor, desc.idProduct, loc))
            if not disk:
                cands = [v for (vid, pid, _l), v in disks.items() if (vid, pid) == (desc.idVendor, desc.idProduct)]
                disk = cands[0] if len(cands) == 1 else None
            if not disk or (only_disk and disk != only_disk):
                continue
            usb_key = (desc.idVendor, desc.idProduct, loc)
            entry = {"usb": "%04x:%04x" % (desc.idVendor, desc.idProduct), "usb_key": list(usb_key),
                     "disk_before": disk}
            try:
                d = BOTDevice(usb, ctx, dev)
            except USBError as e:
                entry["error"] = str(e)
                results[disk] = entry
                continue
            if do_unmount:
                ok, msg = unmount(disk)
                if not ok:
                    blocker = re.search(r"dissented by PID \d+ \(([^)]+)\)", msg)
                    entry["error"] = ("Laufwerk ist in Benutzung und konnte nicht ausgehängt werden"
                                      + (f" (blockiert von {os.path.basename(blocker.group(1))})" if blocker else ""))
                    results[disk] = entry
                    continue
            try:
                d.open()
                try:
                    entry["inquiry"] = inquiry(d)
                except USBError as e:
                    entry["inquiry_error"] = str(e)
                log(f"INQUIRY: {entry.get('inquiry') or entry.get('inquiry_error')}")
                if (desc.idVendor, desc.idProduct) in REALTEK_NVME:
                    data, err = read_realtek_nvme(d)
                else:
                    data, err = read_sat(d)
                if data:
                    entry["smart"] = data
                if err:
                    entry["error"] = err
            except USBError as e:
                entry["error"] = str(e)
            finally:
                d.close()
                if do_unmount:
                    entry["disk_after"] = remount(usb_key, disk)
            log(f"{disk}: {entry.get('error') or 'S.M.A.R.T. gelesen'} -> jetzt {entry.get('disk_after')}")
            results[disk] = entry
    finally:
        usb.libusb_free_device_list(lst, 1)
        usb.libusb_exit(ctx)
    return results


def main():
    args = sys.argv[1:]
    only = args[args.index("--disk") + 1] if "--disk" in args else None
    if os.geteuid() != 0:
        print(json.dumps({"_error": "root nötig: sudo python3 usbsmart.py"}))
        sys.exit(1)
    log(f"Start: {' '.join(args) or '(alle)'}")
    try:
        if "--release" in args:
            res = {"_released": release_all()}
        else:
            res = scan(only, do_unmount="--no-unmount" not in args)
    except (OSError, USBError) as e:
        res = {"_error": str(e)}
    log("Ergebnis: " + json.dumps({k: v.get("error", "ok") if isinstance(v, dict) and "usb" in v else v
                                   for k, v in res.items()}, ensure_ascii=False))
    print(json.dumps(res, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
