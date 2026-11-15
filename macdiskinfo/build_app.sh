#!/bin/bash
# Baut MacDiskInfo.app (Python/Tkinter-Wrapper) neben diesem Skript.
set -euo pipefail
cd "$(dirname "$0")"

PY="/Library/Frameworks/Python.framework/Versions/3.14/bin/python3"
APP="MacDiskInfo.app"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp macdiskinfo.py usbsmart.py "$APP/Contents/Resources/"

# libusb für das USB-Direktlesen (usbsmart.py) mitliefern; linkt nur gegen System-Frameworks. LGPL-2.1.
LIBUSB_PREFIX="$(brew --prefix libusb 2>/dev/null || echo /opt/homebrew/opt/libusb)"
mkdir -p "$APP/Contents/Resources/libusb"
cp "$LIBUSB_PREFIX/lib/libusb-1.0.0.dylib" "$APP/Contents/Resources/libusb/"
cp "$LIBUSB_PREFIX/COPYING" "$APP/Contents/Resources/libusb/COPYING" 2>/dev/null || true

# smartmontools mitliefern (linkt nur gegen System-Libraries, daher ohne Homebrew lauffähig).
# GPL-2: Lizenztext wird mitkopiert.
SMARTCTL="$(command -v smartctl || echo /opt/homebrew/bin/smartctl)"
SMART_PREFIX="$(cd "$(dirname "$(readlink -f "$SMARTCTL")")/.." && pwd)"
SM="$APP/Contents/Resources/smartmontools"
mkdir -p "$SM"
cp "$SMART_PREFIX/bin/smartctl" "$SM/"
chmod 755 "$SM/smartctl"
cp "$SMART_PREFIX/share/smartmontools/drivedb.h" "$SM/" 2>/dev/null || true
cp "$SMART_PREFIX/COPYING" "$SM/COPYING" 2>/dev/null || true
echo "smartctl mitgeliefert: $("$SM/smartctl" --version | head -1)"

cat > "$APP/Contents/MacOS/MacDiskInfo" <<EOF
#!/bin/bash
DIR="\$(cd "\$(dirname "\$0")/../Resources" && pwd)"
exec "$PY" -I "\$DIR/macdiskinfo.py"
EOF
chmod +x "$APP/Contents/MacOS/MacDiskInfo"

cat > "$APP/Contents/Info.plist" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>MacDiskInfo</string>
  <key>CFBundleDisplayName</key><string>MacDiskInfo</string>
  <key>CFBundleIdentifier</key><string>io.github.macdiskinfo</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleExecutable</key><string>MacDiskInfo</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>LSMinimumSystemVersion</key><string>12.0</string>
  <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
EOF

echo "Fertig: $(pwd)/$APP"
