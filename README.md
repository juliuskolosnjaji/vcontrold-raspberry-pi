# Vcontrold + CAN + MQTT + Home Assistant Setup (Raspberry Pi 3B, Raspberry Pi OS Lite 64-bit)

## Schnellstart

Auf einem frisch aufgesetzten Raspberry Pi OS Lite 64-bit (Bookworm) per SSH:

```bash
sudo apt update && sudo apt install -y git
git clone https://github.com/juliuskolosnjaji/vcontrold-raspberry-pi.git
cd vcontrold-raspberry-pi
sudo bash install.sh
```

`install.sh` erledigt automatisch:
- Systemabhängigkeiten (Build-Tools, `can-utils`, Python)
- Build & Installation von vcontrold
- Python-venv mit allen Abhängigkeiten (`venv/`)
- CAN-Overlay (PiCAN2/3) in der Boot-Config aktivieren
- Config-Dateien aus den `.example`-Vorlagen anlegen
- Alle systemd-Dienste installieren (vcontrold, UI und `can0-up` werden direkt gestartet;
  die MQTT-Bridges erst nach deiner manuellen Konfiguration, siehe Ausgabe am Ende des Skripts)

Am Ende zeigt das Skript eine Checkliste der noch nötigen manuellen Schritte (Protokoll wählen,
`mqtt.env`/`ui.env` befüllen, CAN-Mapping ermitteln). Die folgenden Abschnitte erklären diese
Schritte im Detail — bei einem frischen Setup über `install.sh` kannst du direkt bei
**„Wichtig – Protokoll klären"** in Abschnitt 1 weiterlesen und die `sudo cp .../systemd/...`-Befehle
in den weiteren Abschnitten überspringen (die Units sind durch `install.sh` schon installiert).

---

Ziel-Architektur:

```
Viessmann Vitogas100 --Optolink--> USB --> Raspberry Pi --> vcontrold (daemon)
                                                     |
                                          scripts/vcontrold_to_mqtt.py (Cronjob)
                                                     |
Technische Alternative UVR --CAN Bus--> PiCAN2/3 HAT (can0)
                                                     |
                                          scripts/can_to_mqtt.py (systemd)
                                                     |
                                                     v
                                        MQTT Broker (läuft auf Home Assistant)
                                                     |
                                                     v
                                              Home Assistant
                                                     |
                                    (Commands über MQTT zurück)
                                                     |
                                          scripts/mqtt_command_listener.py (systemd)
                                                     |
                                                     v
                                        vclient (vcontrold CLI) -> Heizung
```

## 1. vcontrold installieren

```bash
sudo bash install_vcontrold.sh
```

Das Skript:
- installiert Build-Abhängigkeiten (`libxml2-dev`, `python3-docutils`, `cmake`, `build-essential`, `git`)
- klont `https://github.com/openv/vcontrold`
- baut mit `cmake` + `make` und installiert nach `/usr/local`
- legt eine udev-Regel für den Optolink-USB-Adapter an, damit das Gerät stabil unter `/dev/optolink` erreichbar ist
- kopiert eine `vcontrold.xml`-Vorlage nach `/etc/vcontrold.xml`

**Wichtig – Protokoll klären:** vcontrold unterscheidet zwei Protokollfamilien (`xml/300` = P300, `xml/kw` = älteres KW-Protokoll). Die Vitogas 100 läuft je nach Regelung (Vitotronic 100 GC1, oder ältere Vitotronic 100/200 Typen) auf **KW** oder **P300**. Das musst du für dein Gerät verifizieren (siehe OpenV-Wiki: https://github.com/openv/openv/wiki/ und Typenschild/Handbuch deiner Vitotronic). In `/etc/vcontrold.xml` unter `<device>` das passende Protokoll-XML referenzieren, z.B. `deviceP300.xml` oder `deviceKW.xml` – Namen im Zweifel per `ls /usr/local/etc/vcontrold/xml/` prüfen.

`install.sh` installiert und startet `vcontrold.service` bereits automatisch. Status prüfen:

```bash
sudo systemctl status vcontrold
```

Test:

```bash
vclient -h localhost -p 3002 -c "getTempAussen"
```

(Kommandoname hängt vom Datenpunkt-Namen in deiner Geräte-XML ab — mit `vclient -c "list"` bzw. der XML-Datei nachsehen, welche Getter/Setter für dein Gerät existieren.)

## 2. Vcontrold-Werte per Cronjob an MQTT senden

`scripts/vcontrold_to_mqtt.py` fragt eine Liste von Datenpunkten per `vclient` ab und published sie auf `heizung/<datenpunkt>`.

Konfiguration in `config/mqtt.env` (Broker-Host = deine Home-Assistant-IP, Port meist 1883):

```bash
cp config/mqtt.env.example config/mqtt.env
nano config/mqtt.env
```

Abhängigkeiten sind bereits in der von `install.sh` angelegten venv installiert (`venv/bin/pip install -r requirements.txt`, falls manuell nötig).

Cronjob einrichten (alle 60 Sekunden ist mit normalem Cron nicht möglich, minimal ist 1 Minute — für höhere Frequenz besser systemd-Timer, siehe unten):

```bash
crontab -e
```

Zeile einfügen (Pfad an dein tatsächliches Installationsverzeichnis anpassen, z.B. `/home/pi/vcontrold-raspberry-pi`):

```
* * * * * /home/pi/vcontrold-raspberry-pi/venv/bin/python3 /home/pi/vcontrold-raspberry-pi/scripts/vcontrold_to_mqtt.py >> /var/log/vcontrold_to_mqtt.log 2>&1
```

**Alternative (empfohlen für <60s Intervall):** `systemd/vcontrold-to-mqtt.timer` + `.service` verwenden statt Cron — liegt bei, falls gewünscht.

## 3. CAN-Bus (Technische Alternative UVR) an MQTT — bidirektional

Hardware: PiCAN2/PiCAN3 HAT (MCP2515). SPI + CAN-Overlay aktivieren in `/boot/config.txt` (bzw. `/boot/firmware/config.txt` auf neueren Raspbian-Versionen):

```
dtparam=spi=on
dtoverlay=mcp2515-can0,oscillator=16000000,interrupt=25
dtoverlay=spi-bcm2835-overlay
```

(Oszillator-Frequenz und Interrupt-Pin je nach HAT-Modell anpassen — siehe Doku deines PiCAN-Boards.)

CAN-Interface hochfahren (Baudrate der UVR i.d.R. 20 kBit/s — unbedingt in der UVR-Konfiguration nachsehen, TA nutzt üblicherweise 20 kBit/s für den DL/CAN-Bus zwischen Reglern). `install.sh` installiert und startet `can0-up.service` bereits automatisch; manuell:

```bash
sudo cp systemd/can0-up.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now can0-up
```

Die CAN-Anbindung läuft in **beide Richtungen** über zwei getrennte Dienste (von `install.sh` bereits installiert, aber bewusst **nicht gestartet**, bis du `FRAME_MAP`/`COMMAND_MAP` befüllt hast):

**a) UVR → Pi → MQTT** (`scripts/can_to_mqtt.py`): liest Frames von `can0` mit `python-can`, dekodiert sie (**Platzhalter-Mapping in `FRAME_MAP`** — die CAN-IDs/Bytes der Technische-Alternative-Geräte sind proprietär und müssen anhand deiner UVR-Konfiguration bzw. Community-Dokumentation ermittelt werden) und published auf `uvr/<kanal>`.

```bash
sudo systemctl start can-to-mqtt   # nach Anpassung von FRAME_MAP
```

**b) Home Assistant → MQTT → Pi → UVR** (`scripts/mqtt_to_can.py`): abonniert `uvr/cmd/<kanal>` und sendet daraufhin einen CAN-Frame an die UVR (**Platzhalter-Mapping in `COMMAND_MAP`**, ebenfalls anhand deiner UVR-CAN-Spezifikation zu befüllen).

```bash
sudo systemctl start mqtt-to-can   # nach Anpassung von COMMAND_MAP
```

**Protokoll herausfinden:** Da die CAN-IDs/Byte-Layouts von Technische Alternative nicht offiziell dokumentiert sind, starte `can_to_mqtt.py` zunächst mit leerem `FRAME_MAP` — es loggt dann alle unbekannten Frames (`id=0x... data=...`) nach stderr. Über gezielte Änderungen an der UVR (z.B. eine Pumpe manuell ein-/ausschalten) lässt sich beobachten, welche ID/Bytes sich ändern, und so das Mapping empirisch ableiten. Alternativ: `candump can0` (aus `can-utils`, `sudo apt install can-utils`) zum manuellen Mitschneiden nutzen.

## 4. Commands von Home Assistant zurück an vcontrold

`scripts/mqtt_command_listener.py` abonniert `heizung/cmd/#` und ruft für jeden empfangenen Befehl den passenden `vclient`-Set-Befehl auf, z.B. Topic `heizung/cmd/solltemperatur` mit Payload `21.5` → `vclient -c "setSolltempNormal 21.5"`.

Die Zuordnung Topic → vclient-Kommando steht in `config/command_map.json` — dort trägst du die tatsächlichen Setter-Namen deiner Geräte-XML ein.

Von `install.sh` bereits installiert, aber nicht gestartet — nach Anpassung von `command_map.json`:

```bash
sudo systemctl start mqtt-command-listener
```

## 5. Home Assistant einbinden

`homeassistant/configuration_snippet.yaml` enthält Beispiel-`mqtt: sensor:` und `mqtt: number:`/`climate:`-Einträge für die veröffentlichten Topics. In `configuration.yaml` von Home Assistant einbinden oder per MQTT-Discovery automatisch erkennen lassen (Discovery-Variante ist im Snippet als Kommentar skizziert).

## 6. Web-UI (Konsole, Config-Import, MQTT-Einstellungen, Diagnose, CAN-Sniffer)

Im Ordner `ui/` liegt eine kleine Flask-App zum Testen und Verwalten:

- **Konsole**: Getter/Setter aus deiner Geräte-XML per Dropdown auswählen oder frei eingeben, direkt per `vclient` ausführen. Set-Befehle erfordern eine Bestätigung.
- **Config-Import**: Geräte-XML hochladen, Backup der bisherigen `/etc/vcontrold.xml` wird automatisch angelegt, danach `systemctl restart vcontrold`.
- **MQTT-Einstellungen**: `config/mqtt.env` (Broker-Host, Port, Zugangsdaten, Topic-Präfixe) direkt im Browser bearbeiten und die Verbindung testen. Beim Speichern werden bereits laufende Bridge-Dienste (`can-to-mqtt`, `mqtt-to-can`, `mqtt-command-listener`, `vcontrold-to-mqtt.timer`) automatisch neu gestartet — kein manuelles Editieren per SSH mehr nötig.
- **Diagnose**: Status aller Dienste (vcontrold, can-to-mqtt, mqtt-to-can, mqtt-command-listener, can0-up), Live-Logs, MQTT-Verbindungstest, CAN-Interface-Status.
- **CAN-Sniffer**: zeichnet für N Sekunden rohe CAN-Frames auf — hilft dabei, das UVR-Protokoll für `FRAME_MAP`/`COMMAND_MAP` empirisch zu ermitteln.

`install.sh` legt `ui/ui.env` aus der Vorlage an und startet den Dienst bereits automatisch. Danach unbedingt:

```bash
nano ui/ui.env   # UI_USERNAME/UI_PASSWORD ändern! DEVICE_XML_PATH auf deine Geräte-XML setzen
sudo systemctl restart vcontrold-ui
```

Erreichbar unter `http://<pi-ip>:5000` (läuft **als root**, da Config-Import nach `/etc/` schreibt und `systemctl restart` ausführt).

**Sicherheitshinweis:** Die UI kann Sollwerte an die echte Heizung senden. Nur im vertrauenswürdigen LAN betreiben (nicht ins Internet weiterleiten), starkes Passwort in `ui.env` setzen. Basic-Auth über HTTP ist unverschlüsselt — bei Bedarf zusätzlich per Reverse-Proxy (z.B. Caddy/nginx) mit HTTPS absichern.

## Offene Punkte, die nur du klären kannst

1. **Protokoll deiner Vitotronic** (KW vs. P300) und die exakten Datenpunkt-Namen — steht in der zu deinem Regler passenden XML-Datei im vcontrold-Repo.
2. **CAN-Bitrate und Frame-Format der UVR** — abhängig vom TA-Gerätetyp (z.B. UVR1611, UVR16x2) und dessen CAN-Konfiguration. Nötig für `FRAME_MAP` (Lesen) und `COMMAND_MAP` (Schreiben) in den CAN-Skripten.
3. **PiCAN-Board-Variante** (PiCAN2 vs. PiCAN3, Oszillatorfrequenz) für den korrekten Device-Tree-Overlay-Parameter.
4. **MQTT-Zugangsdaten** des Home-Assistant-Mosquitto-Brokers (Host/User/Passwort) in `config/mqtt.env`.
