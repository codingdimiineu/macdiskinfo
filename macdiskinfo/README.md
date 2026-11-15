# MacDiskInfo

Ein Festplatten-Gesundheitstool für macOS im Stil von [CrystalDiskInfo](https://crystalmark.info/en/software/crystaldiskinfo/) – geschrieben in Python mit Tkinter-Oberfläche.

MacDiskInfo zeigt für jedes Laufwerk Zustand (Gut / Vorsicht / Schlecht), Temperatur, Firmware, Seriennummer, gelesene und geschriebene Datenmenge, Betriebsstunden und die vollständige S.M.A.R.T.-Attributtabelle.

> **Hinweis:** MacDiskInfo ist kein Port von CrystalDiskInfo. CrystalDiskInfo ist Open Source (MIT), nutzt aber Windows-eigene Schnittstellen. MacDiskInfo liest dieselben Daten über `diskutil` und [smartmontools](https://www.smartmontools.org/) (`smartctl`).

---

## Funktionen

- **Laufwerksleiste** mit Zustand und Temperatur aller physischen Laufwerke (Klick oder ← → zum Wechseln)
- **Zustand** mit Farbcode wie bei CrystalDiskInfo, bei NVMe-SSDs zusätzlich Restlebensdauer in %
- **Temperatur**: gelb ab 50 °C, rot ab 60 °C
- **Infofeld**: Firmware, Seriennummer, Schnittstelle, Volumes, Standard, Funktionen (S.M.A.R.T., TRIM), Gesamt gelesen/geschrieben, Einschaltvorgänge, Betriebsstunden
- **S.M.A.R.T.-Tabelle** für NVMe und ATA/SATA, auffällige Werte farbig hinterlegt
- **Auto-Aktualisierung** (1, 5, 10 oder 30 Minuten), **Cmd+R** aktualisiert sofort
- **Bericht kopieren**: alle Laufwerke als Text in die Zwischenablage
- **Attributnamen aus der smartmontools-Datenbank** (`drivedb.h`), herstellerspezifisch wie bei `smartctl`
- **USB-Direktlesen (experimentell)**: S.M.A.R.T. von externen USB-Laufwerken ohne Kernel-Treiber, siehe unten

## Voraussetzungen

| Wofür | Was |
|---|---|
| Starten | macOS 12 oder neuer, [Python 3 von python.org](https://www.python.org/downloads/macos/) (bringt Tk mit) |
| Neu bauen der App | [Homebrew](https://brew.sh) mit `smartmontools` und `libusb` |

```bash
brew install smartmontools libusb
```

Getestet auf einem MacBook Air (Apple Silicon) mit macOS 26 und Python 3.14.

## Installation und Start

```bash
git clone <URL dieses Repositorys>
cd macdiskinfo
./build_app.sh
open MacDiskInfo.app
```

`build_app.sh` erzeugt `MacDiskInfo.app` und kopiert dabei `smartctl` (inkl. `drivedb.h`) und `libusb` aus Homebrew in das App-Bundle. Die fertige App läuft danach ohne Homebrew.

Ohne App-Bundle direkt starten:

```bash
python3 macdiskinfo.py
```

> Der Pfad zu Python ist in `build_app.sh` fest eingetragen (`PY=…`). Wer Python anders installiert hat, passt diese Zeile an.

## Bedienung

| Aktion | So geht's |
|---|---|
| Laufwerk wählen | Kachel oben anklicken oder ← → |
| Aktualisieren | Cmd+R oder F5, Menü **Datei → Aktualisieren** |
| Bericht kopieren | Menü **Datei → Bericht kopieren** |
| Auto-Aktualisierung | Menü **Funktion → Auto-Aktualisierung** |
| USB-Laufwerke direkt lesen | Button unter dem Hinweis oder Menü **Funktion** (Admin-Passwort nötig) |
| USB-Laufwerke zurückgeben (Notfall) | Menü **Funktion → USB-Laufwerke an macOS zurückgeben** |

## Wie der Zustand bewertet wird

Angelehnt an CrystalDiskInfo, festgelegt in `evaluate_health()` in `macdiskinfo.py`:

**NVMe**
- **Schlecht**: S.M.A.R.T. fehlgeschlagen oder ein Critical-Warning-Bit gesetzt
- **Vorsicht**: Medienfehler > 0, verfügbare Reserve unter dem Schwellwert oder Verschleiß ≥ 90 %
- **Gut**: sonst; Gesundheit in % = 100 − „Percentage Used“

**ATA/SATA**
- **Schlecht**: S.M.A.R.T. fehlgeschlagen oder ein Attribut ≤ Schwellwert
- **Vorsicht**: Rohwert von 05 (reallokierte Sektoren), C5 (schwebende Sektoren) oder C6 (unkorrigierbare Sektoren) > 0
- **Gut**: sonst

Die Bewertung ist eine Orientierung, keine Garantie. Für wichtige Daten gilt immer: Backup.

## USB-Laufwerke

macOS reicht S.M.A.R.T.-Befehle über seinen USB-Massenspeicher-Treiber nicht durch. `smartctl` meldet bei USB-Laufwerken deshalb „Operation not supported by device“, und MacDiskInfo zeigt sie zunächst nur mit Basisdaten (Modell, Größe, Volumes) an.

Mit **„USB-Laufwerke direkt lesen“** übernimmt MacDiskInfo die USB-Box für ein paar Sekunden selbst (über `libusb`, ohne Kernel-Erweiterung, Systemsicherheit bleibt unverändert), schickt dieselben Befehle wie `smartctl` unter Linux und gibt die Box danach an macOS zurück. Dafür werden die Volumes des Laufwerks kurz ausgehängt, und macOS fragt nach dem Admin-Passwort.

| USB-Bridge-Chip | Verfahren | Stand |
|---|---|---|
| ASMedia ASM105x (SATA) | SAT – ATA PASS-THROUGH | ✅ funktioniert (Einzeltest im Terminal) |
| Realtek RTL9210/9220 (NVMe) | Vendor-Befehl 0xE4 wie `smartctl -d sntrealtek` | ⚠️ implementiert, noch nicht erfolgreich getestet |
| JMicron JMS561 (RAID-Box) | SAT | ❌ Box antwortet nicht |
| Andere SATA-Bridges | SAT | ungetestet |

> ⚠️ **Experimentell – bekanntes Problem:** Beim Lesen mehrerer USB-Laufwerke auf einmal über die App kam es vor, dass die Laufwerke danach nicht zu macOS zurückkehrten. Die Daten waren sicher (die Volumes waren vorher sauber ausgehängt), aber die Laufwerke mussten neu eingesteckt werden. Seitdem gibt es eine automatische Notfall-Rückgabe, ein Protokoll unter `~/Library/Logs/MacDiskInfo/usbsmart.log` und den Menüpunkt **„USB-Laufwerke an macOS zurückgeben“**. Die Ursache ist noch nicht gefunden. Funktion nicht verwenden, während auf die Laufwerke geschrieben wird (z. B. laufende Backups).

Technische Details: [docs/USB-DIREKTLESEN.md](docs/USB-DIREKTLESEN.md)

## Projektaufbau

```
macdiskinfo/
├── macdiskinfo.py        Oberfläche, Laufwerksliste, Bewertung (Tkinter)
├── usbsmart.py           USB-Direktlesen über libusb (läuft mit Admin-Rechten)
├── build_app.sh          baut MacDiskInfo.app inkl. smartctl und libusb
├── LICENSE               MIT-Lizenz
└── docs/
    └── USB-DIREKTLESEN.md
```

Datenquellen im Überblick:

- `diskutil list/info/apfs list -plist` – Laufwerke, Modell, Größe, Anschluss, Volumes
- `mount` – eingehängte Volumes
- `smartctl -a -j` – S.M.A.R.T. für interne und direkt angebundene Laufwerke
- `usbsmart.py` – S.M.A.R.T. für USB-Laufwerke (optional, mit Admin-Rechten)

## Grenzen

- Externe USB-Laufwerke liefern S.M.A.R.T. nur über das experimentelle USB-Direktlesen.
- Laufwerke in Thunderbolt-/USB4-NVMe-Gehäusen erscheinen als echte NVMe-Laufwerke und funktionieren direkt über `smartctl`.
- Die Oberfläche ist hell gestaltet, einen Dark Mode gibt es noch nicht.
- Python von python.org wird zum Starten gebraucht; das App-Bundle ist ein Wrapper, kein eigenständiges Programm.

## Lizenzen der mitgelieferten Komponenten

Das gebaute App-Bundle enthält:

- **smartmontools** (`smartctl`, `drivedb.h`) – GPL-2.0, Lizenztext in `Contents/Resources/smartmontools/COPYING`
- **libusb** – LGPL-2.1, Lizenztext in `Contents/Resources/libusb/COPYING`

Dieses Repository enthält nur den eigenen Quellcode, die Binärdateien kommen beim Bauen aus Homebrew. Wer die fertige `MacDiskInfo.app` weitergibt, verteilt damit auch smartctl und muss die Bedingungen der GPL-2.0 einhalten (u. a. Lizenztext beilegen und den Quellcode von smartmontools zugänglich machen).

## Lizenz

Der Quellcode dieses Repositorys steht unter der [MIT-Lizenz](LICENSE).

Die beim Bauen mitgelieferten Komponenten behalten ihre eigenen Lizenzen (smartmontools: GPL-2.0, libusb: LGPL-2.1), siehe oben.
