# USB-Direktlesen – technische Details

Dieses Dokument beschreibt, wie `usbsmart.py` S.M.A.R.T.-Daten von USB-Laufwerken auf macOS liest, und was dabei bisher getestet wurde.

## Warum das nötig ist

`smartctl` greift auf macOS über IOKit zu. Für interne NVMe- und SATA-Laufwerke funktioniert das. USB-Gehäuse dagegen hängen am Massenspeicher-Treiber von macOS (`IOUSBMassStorageDriver` bzw. UAS), und der reicht ATA- oder NVMe-Befehle nicht durch. `smartctl` meldet dann:

```
Smartctl open device: diskN failed: Operation not supported by device
```

Bekannte Alternativen und warum sie hier nicht genutzt werden:

| Weg | Problem |
|---|---|
| SAT-SMART-Kext (z. B. von BinaryFruit/DriveDx) | Kernel-Erweiterung; auf Apple Silicon muss dafür die Systemsicherheit im Wiederherstellungsmodus auf „Reduziert“ gesetzt werden. Hilft nur bei SATA-Bridges, nicht bei NVMe-Gehäusen. |
| Linux-VM mit USB-Durchreichung | funktioniert breit, aber schwergewichtig (QEMU, Linux-Image) und langsam |
| Thunderbolt-/USB4-Gehäuse | SSD erscheint als echtes NVMe-Gerät, `smartctl` funktioniert direkt – erfordert aber neue Hardware |

## Ablauf

`usbsmart.py` arbeitet komplett im User-Space mit `libusb` (über `ctypes`, ohne zusätzliche Python-Pakete):

1. **Zuordnung** – Aus der IORegistry (`ioreg -a -r -c IOUSBHostDevice -l`) wird ermittelt, welches USB-Gerät (Vendor-ID, Product-ID, locationID) zu welchem `diskN` gehört. Bei verschachtelten Geräten (Hub → Laufwerk) zählt das innerste Gerät.
2. **Aushängen** – `diskutil unmountDisk diskN`. Verweigert macOS das (z. B. weil ein Prozess die Platte benutzt), wird das Laufwerk übersprungen und nicht angefasst.
3. **Übernehmen** – `libusb_set_auto_detach_kernel_driver` + `libusb_claim_interface` auf dem Bulk-Only-Interface (Klasse 8, Subklasse 6, Protokoll 0x50). Auf macOS braucht das Root-Rechte; macOS meldet das Gerät dabei neu an und bindet seinen Treiber ab.
4. **Reset** – Bulk-Only Mass Storage Reset (Class-Request 0xFF) und Clear-Halt auf beiden Endpoints, danach zweimal TEST UNIT READY.
5. **Befehle** über das USB-Bulk-Only-Protokoll (CBW → Daten → CSW):
   - **SATA-Bridges** (SAT): ATA PASS-THROUGH(16) `0x85`, Rückfall auf (12) `0xA1`
     - IDENTIFY DEVICE (`0xEC`) – Modell, Seriennummer, Firmware, TRIM
     - SMART READ DATA (`0xB0`/`0xD0`) – Attributtabelle
     - SMART READ THRESHOLDS (`0xB0`/`0xD1`)
   - **Realtek RTL9210/9220** (NVMe): Vendor-CDB `0xE4` wie `smartctl -d sntrealtek`
     - NVMe Identify Controller (Opcode `0x06`, CNS 1)
     - Get Log Page `0x02` (SMART/Health, 512 Byte)
6. **Freigeben** – `libusb_release_interface` / `libusb_close`; macOS meldet das Gerät erneut an und bindet seinen Treiber wieder.
7. **Wieder einhängen** – Das Laufwerk bekommt dabei oft eine neue Nummer (z. B. `disk6` → `disk5`). `usbsmart.py` wartet bis zu 25 s, bis das USB-Gerät wieder als Laufwerk erscheint, und hängt es samt APFS-Container wieder ein. Erscheint es nicht, folgt ein USB-Reset (`libusb_reset_device`) als Notfall-Rückgabe.

Die Ausgabe ist JSON im Format von `smartctl -j` (`model_name`, `ata_smart_attributes`, `nvme_smart_health_information_log` …), damit MacDiskInfo sie genauso anzeigt wie Daten aus `smartctl`. Attributnamen kommen aus der mitgelieferten `drivedb.h` (DEFAULT-Eintrag plus erster passender Modelleintrag, wie bei `smartctl`).

## Aufruf ohne App

```bash
# ein Laufwerk, mit Protokoll auf stderr
sudo python3 usbsmart.py --disk disk6 --verbose

# alle USB-Laufwerke, Protokoll in Datei
sudo python3 usbsmart.py --log ~/Library/Logs/MacDiskInfo/usbsmart.log

# Notfall: hängende USB-Boxen an macOS zurückgeben
sudo python3 usbsmart.py --release --verbose
```

| Option | Wirkung |
|---|---|
| `--disk diskN` | nur dieses Laufwerk |
| `--verbose` | Protokoll auf stderr |
| `--log DATEI` | Protokoll an Datei anhängen |
| `--no-unmount` | nicht aushängen (nur für bereits ausgehängte Laufwerke!) |
| `--release` | USB-Massenspeicher ohne macOS-Laufwerk per Reset zurückgeben |

In der App läuft der Aufruf über `osascript … with administrator privileges`, also mit dem normalen macOS-Passwortdialog. Das Protokoll landet in `~/Library/Logs/MacDiskInfo/usbsmart.log`.

## Testergebnisse

Getestet auf MacBook Air (Apple Silicon), macOS 26, libusb 1.0.30, Python 3.14.

| Laufwerk / Bridge | Ergebnis |
|---|---|
| Crucial BX300 480 GB in ASMedia ASM105x (`174c:55aa`) | ✅ Einzeltest im Terminal: IDENTIFY, alle 23 Attribute, Schwellwerte, Temperatur. Laufwerk kam danach zurück, wurde aber wegen der Neunummerierung zunächst nicht wieder eingehängt (inzwischen behoben). |
| JMicron JMS561 RAID-Box (`152d:a561`, UAS) | ❌ Übernahme klappt, aber schon INQUIRY läuft in einen Timeout – auch nach BOT-Reset. Vermutlich blockiert die RAID-Firmware Bulk-Only-Zugriffe. Laufwerk kam jedes Mal sauber zurück. |
| Realtek RTL9210 NVMe (`0bda:9210`) | ⏸ nicht getestet: macOS verweigerte das Aushängen, weil `cloudd` (iCloud) auf das Volume zugriff. Die Sicherheitsprüfung hat korrekt abgebrochen. |
| Alle drei auf einmal über die App | ❌ 0 von 3 gelesen, und alle drei Boxen blieben ohne macOS-Treiber hängen, bis sie neu eingesteckt wurden. Ursache unbekannt; bei diesem Lauf gab es noch kein Protokoll. |

## Offene Punkte

- **Ursache für das Hängenbleiben finden.** Unterschiede zum funktionierenden Einzeltest: Start über `osascript` statt `sudo`, mehrere Geräte nacheinander in einem libusb-Kontext. Nächster Schritt: Lauf über alle Laufwerke im Terminal mit `--verbose --log`, danach dasselbe über die App, und die Protokolle vergleichen.
- Notfall-Rückgabe (`--release`, automatischer Reset) ist eingebaut, aber noch nicht in einem echten Hänger getestet.
- Realtek-NVMe-Pfad an einem Laufwerk testen, das sich aushängen lässt.
- JMicron JMS583 (häufige NVMe-Bridge) ist nicht implementiert; `smartctl` nutzt dafür ein zweistufiges Protokoll (`-d sntjmicron`).
- UAS-Geräte werden auf ihr Bulk-Only-Alt-Setting umgeschaltet; ob das bei allen UAS-Bridges klappt, ist offen.

## Quellen

- smartmontools: [`scsinvme.cpp`](https://github.com/smartmontools/smartmontools/blob/master/smartmontools/scsinvme.cpp) (Realtek/JMicron/ASMedia-NVMe-Durchreichung), [`drivedb.h`](https://github.com/smartmontools/smartmontools/blob/master/smartmontools/drivedb.h)
- [Reading SMART Data from WD External Drives on macOS (Without Kernel Extensions)](https://jyates.dev/blog/wd-smart-reader) – User-Space-Zugriff über IOKit
- [BinaryFruit – DriveDx USB-Unterstützung](https://binaryfruit.com/drivedx/usb-drive-support) und [SAT SMART Driver für Apple Silicon](https://binaryfruit.com/blog/we-released-sat-smart-driver-for-apple-silicon-macs)
- USB Mass Storage Class – Bulk-Only Transport, Rev. 1.0 (Reset-Recovery, CBW/CSW)
- T10 SAT – SCSI/ATA Translation (ATA PASS-THROUGH 12/16)
