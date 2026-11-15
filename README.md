# macdiskinfo

Short (for the GitHub “About” field):

Disk health tool for macOS in the style of CrystalDiskInfo: health status, temperature and S.M.A.R.T. data for NVMe and SATA drives, plus experimental reading of USB drives without a kernel extension. Python + Tkinter, built on smartmontools.

Topics: macos, smart, smartctl, ssd, nvme, disk-health, crystaldiskinfo, libusb, python, tkinter

Medium (one paragraph):

MacDiskInfo shows how healthy your Mac’s drives are, the way CrystalDiskInfo does on Windows. Each drive gets a color-coded rating (Good, Caution or Bad) and its temperature. It also shows the key details: firmware, serial number, power-on hours, power cycles, and total data read and written. Below that is the full S.M.A.R.T. attribute table, with problem values highlighted. The data comes from diskutil and smartmontools (smartctl). smartctl is bundled inside the app, and Homebrew is only needed to build it.

Detailed (features and limits):

Internal SSDs and directly attached drives are read in full via smartctl. NVMe SSDs also show their remaining life as a percentage.
Health rating follows CrystalDiskInfo:
SATA: “Bad” if any attribute drops to or below its threshold; “Caution” if any reallocated, pending or uncorrectable sectors appear.
NVMe: “Bad” if a critical-warning bit is set; “Caution” from 90 % wear.
Attribute names come from the smartmontools drive database, so they match the vendor-specific names smartctl uses.
Convenience: auto-refresh, copy a full report to the clipboard, switch drives with the arrow keys.
USB drives: macOS doesn’t pass S.M.A.R.T. commands through to USB enclosures. MacDiskInfo can briefly take over the enclosure itself. That needs the admin password, but no kernel extension, and the Mac’s security settings stay unchanged. The drive is unmounted for a few seconds while it’s read.
With an ASMedia SATA enclosure this worked.
When several enclosures were read at once, they once stayed disconnected from macOS and had to be replugged. The feature is therefore marked experimental.
Requirement: Python 3 from python.org. The app bundle is a thin wrapper around the Python script, not a standalone program.

The README and the docs in the ZIP are still in German.
