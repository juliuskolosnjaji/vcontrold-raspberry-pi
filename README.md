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
- CAN-Overlay (Waveshare 2-CH CAN HAT+) in der Boot-Config aktivieren
- Config-Dateien aus den `.example`-Vorlagen anlegen
- Alle systemd-Dienste installieren (vcontrold, UI und `can1-up` werden direkt gestartet;
  die MQTT-Bridges erst nach deiner manuellen Konfiguration, siehe Ausgabe am Ende des Skripts)

Am Ende zeigt das Skript eine Checkliste der noch nötigen manuellen Schritte (Protokoll wählen,
`mqtt.env`/`ui.env` befüllen, CAN-Mapping ermitteln). Die folgenden Abschnitte erklären diese
Schritte im Detail — bei einem frischen Setup über `install.sh` kannst du direkt bei
**„Wichtig – Protokoll klären"** in Abschnitt 1 weiterlesen und die `sudo cp .../systemd/...`-Befehle
in den weiteren Abschnitten überspringen (die Units sind durch `install.sh` schon installiert).

---

Ziel-Architektur:

```
Viessmann Vitogas100 --Optolink--> USB --> vcontrold (daemon, Port 3002)
                                                     ^
                                                     | vclient (get/set)
                                                     |
                                   scripts/orchestrator.py (systemd-Daemon)
                                   - fährt mehrere Read-Zyklen (config/read_cycles.json)
                                   - nimmt Set-Befehle von MQTT UND von der UVR entgegen
                                   - verifiziert jeden Set-Befehl mit einem Get danach
                                             |                        ^
                                  MQTT (heizung/*,              internal/can/rx_set/*
                                  internal/can/tx/*)                    |
                                             v                        |
                                   MQTT Broker (auf Home Assistant)    |
                                             ^                        |
                                             |                        |
                                   scripts/can_node.py (systemd-Daemon, eigener Prozess)
                                   - sendet Vcontrold-Werte blockweise (4 analog / 16 digital
                                     pro CAN-Frame) an die UVR
                                   - empfängt CAN-Frames von der UVR, published sie und
                                     leitet Set-Anfragen an den Orchestrator weiter
                                             |
                                       Waveshare 2-CH CAN HAT+ (can1)
                                             |
                                Technische Alternative UVR16x2 (CAN-Bus)
```

Zwei getrennte, dauerhaft laufende systemd-Dienste statt vieler kleiner Skripte:
`orchestrator.py` (Vcontrold-Zyklen, Set-Verifikation, Routing) und `can_node.py`
(CAN-Encoding/Decoding). Getrennt, damit ein Fehler in der CAN-Dekodierung nicht
auch die Vcontrold-Zyklen und die MQTT-Befehlsverarbeitung lahmlegt. Beide
kommunizieren über interne MQTT-Topics auf demselben Broker (kein Cronjob,
da Cronjobs zwischen zwei Läufen keine offene MQTT-/CAN-Verbindung halten und
damit nicht "on demand" auf Set-Befehle reagieren könnten).

## 1. vcontrold installieren

```bash
sudo bash install_vcontrold.sh
```

Das Skript:
- installiert Build-Abhängigkeiten (`libxml2-dev`, `python3-docutils`, `cmake`, `build-essential`, `git`)
- klont `https://github.com/openv/vcontrold` nach `/usr/local/src/vcontrold`
- baut mit `cmake` + `make` und installiert (vcontrolds `CMakeLists.txt` setzt den Prefix fest auf `/usr`, also landet der Daemon unter `/usr/sbin/vcontrold`, `vclient` unter `/usr/bin/vclient`)
- legt eine udev-Regel für den Optolink-USB-Adapter an
- legt `/etc/vcontrold/vcontrold.xml` + `/etc/vcontrold/vito.xml` an (die zwei Dateien, die vcontrold laut `man vcontrold` erwartet — `vcontrold.xml` enthält Serial-Device/Port/Device-ID, `vito.xml` die Kommando-Definitionen und wird per XInclude eingebunden)

**Protokoll bereits bekannt:** Für dieses Setup wurde die Regelung bereits identifiziert — **Vitotronic V200KW1 (Device-ID `2094`), KW-Protokoll**. Die passende, funktionsfähige Config liegt unter [`config/device-vitogas100-v200kw1/`](config/device-vitogas100-v200kw1/) (inkl. aller Getter/Setter für Temperaturen, Betriebsart, Brennerstunden etc.) und wird von `install_vcontrold.sh` automatisch nach `/etc/vcontrold/` kopiert — `<tty>` steht auf `/dev/ttyUSB0`.

Falls du das Setup auf einer **anderen** Vitotronic-Regelung nachbaust, unterscheidet vcontrold zwei Protokollfamilien (`xml/300` = P300, `xml/kw` = älteres KW2-Protokoll). Welches Protokoll dein Gerät spricht, siehe OpenV-Wiki: https://github.com/openv/openv/wiki/. Umschalten:

```bash
sudo cp /usr/local/src/vcontrold/xml/300/vcontrold.xml /etc/vcontrold/vcontrold.xml
sudo cp /usr/local/src/vcontrold/xml/300/vito.xml /etc/vcontrold/vito.xml
sudo sed -i 's#<tty>.*</tty>#<tty>/dev/optolink</tty>#' /etc/vcontrold/vcontrold.xml
sudo systemctl restart vcontrold
```

Und in `/etc/vcontrold/vcontrold.xml` unter `<device ID="..."/>` die zu deinem Vitotronic-Typ passende Geräte-ID eintragen — die verfügbaren IDs stehen am Anfang der jeweiligen `vito.xml` (`<devices><device ID="..." name="..." protocol=".../></devices>`).

`install.sh` installiert und startet `vcontrold.service` bereits automatisch. Status prüfen:

```bash
sudo systemctl status vcontrold
```

Test:

```bash
vclient -h localhost -p 3002 -c "getTempAussen"
```

(Kommandoname hängt vom Datenpunkt-Namen in deiner Geräte-XML ab — mit `vclient -c "list"` bzw. der XML-Datei nachsehen, welche Getter/Setter für dein Gerät existieren.)

## 2. Orchestrator: Read-Zyklen, Set-Befehle, Verifikation

`scripts/orchestrator.py` läuft dauerhaft als systemd-Dienst und übernimmt drei Aufgaben:

1. **Mehrere Read-Zyklen mit unterschiedlichen Intervallen** aus `config/read_cycles.json` — z.B. Temperaturen alle 30s, Zählerstände alle 5 Minuten. Jeder gelesene Wert wird auf `heizung/<Variable>` published (für Home Assistant) UND auf `internal/can/tx/<Variable>` (damit `can_node.py` denselben Stand an die UVR weiterreicht). Alle Getter eines Zyklus werden dabei in **einer** `vclient`-Verbindung abgefragt (`-c get1,get2,...` mit `-t`-Template, `$R1..$Rn`), statt pro Variable eine eigene Verbindung zu öffnen — schneller bei vielen Variablen pro Zyklus, hat aber den Kompromiss, dass ein Verbindungsfehler alle Variablen dieses Zyklus-Durchlaufs betrifft, nicht nur eine einzelne.
2. **On-demand Set-Befehle**, sowohl von Home Assistant (`heizung/cmd/<Variable>`) als auch von der UVR selbst (`can_node.py` leitet CAN-seitige Set-Anfragen über `internal/can/rx_set/<Variable>` weiter).
3. **Verifikation:** nach jedem Set-Befehl wird automatisch der zugehörige Get-Befehl nachgeschickt, und erst der so bestätigte Ist-Wert wird published — nicht der ungeprüfte Set-Rückgabewert.

**Kanonische Namen statt eigener MQTT-Bezeichner:** Es gibt keine separate Umbenennungsebene mehr —
`scripts/vito_variables.py` liest `/etc/vcontrold/vito.xml` und fasst jedes Getter/Setter-Paar
(`getTempAist`/`setTempAist`) zu einer Variable `TempAist` zusammen. Dieser exakte Name (Groß-/
Kleinschreibung wie in vito.xml) ist gleichzeitig der MQTT-Subtopic-Name **und** der CAN-Kanalname
in `config/can_mapping.json` — keine zwei verschiedenen Bezeichner für dieselbe Sache mehr zu pflegen.

Konfiguration am einfachsten über die Web-UI unter **Variablen** (siehe Abschnitt 5) — dort siehst du
alle aus `vito.xml` extrahierten Variablen in einer Tabelle, ordnest jeder einen Zyklus zu und
legst fest, welche per MQTT/CAN setzbar sein sollen (inkl. Home-Assistant-Discovery-Metadaten).
Manuell geht es auch:

```bash
cp config/mqtt.env.example config/mqtt.env
nano config/mqtt.env       # Broker-Host = deine Home-Assistant-IP
cp config/read_cycles.json.example config/read_cycles.json
nano config/read_cycles.json   # {"zyklus_name": {"interval_seconds": N, "variables": ["TempAist", ...]}}
cp config/command_map.json.example config/command_map.json
nano config/command_map.json   # {"TempRaumNorSoll": {"discovery": {...}}} pro settable Variable
```

`command_map.json` ist eine **Freigabeliste**: nur Variablen, die dort eingetragen sind, lassen sich
per MQTT/CAN setzen — auch wenn `vito.xml` einen Setter dafür hat. Die `set`/`get`-Kommandos selbst
werden automatisch aus `vito.xml` aufgelöst, in `command_map.json` stehen nur noch optionale
Discovery-Metadaten für Home Assistant (Einheit, Min/Max/Step, oder Auswahloptionen bei einem Select).

Von `install.sh` bereits installiert, aber bewusst nicht automatisch gestartet (erst nach Konfiguration):

```bash
sudo systemctl start orchestrator
sudo systemctl status orchestrator
```

Der Orchestrator braucht **kein** CAN-Mapping, um zu laufen — die Read-Zyklen und MQTT-Set-Befehle funktionieren unabhängig von `can_node.py` (siehe Abschnitt 3). Die `internal/can/tx/*`-Publishes gehen einfach ins Leere, solange `can_node.py` nicht läuft.

## 3. CAN-Bus (Technische Alternative UVR) an MQTT — bidirektional

Hardware: [Waveshare 2-CH CAN HAT+](https://www.waveshare.com/wiki/2-CH_CAN_HAT+) (2× MCP2515 über SPI1, in Reihe zwei CAN-Kanäle can0/can1 — für die UVR wird nur can1 genutzt, weil das UVR-Kabel an diesem Anschluss des Boards hängt). SPI + CAN-Overlay aktivieren in `/boot/config.txt` (bzw. `/boot/firmware/config.txt` auf neueren Raspbian-Versionen):

```
dtparam=spi=on
dtoverlay=i2c0
dtoverlay=spi1-3cs
dtoverlay=mcp2515,spi1-1,oscillator=16000000,interrupt=22
dtoverlay=mcp2515,spi1-2,oscillator=16000000,interrupt=13
```

Das sind die Werte für die **Standardverlötung** des Boards (INT_0 auf GPIO22, INT_1 auf GPIO13). `spi1-1` wird `can0`, `spi1-2` wird `can1`. Falls die Lötbrücken auf deinem Board umgesetzt wurden (siehe Wiki-Seite), die `interrupt=`-Werte entsprechend anpassen. `install.sh` schreibt diese Zeilen automatisch in die Boot-Config.

**Hinweis:** dieses Projekt nutzt `can1` für die UVR (nicht `can0`) — schlicht weil das UVR-Kabel am `spi1-2`/`can1`-Anschluss des Waveshare-Boards angeschlossen ist. Falls dein Kabel am anderen Anschluss hängt, überall `can0` statt `can1` verwenden.

CAN-Interface hochfahren (Standard-Bus-Geschwindigkeit der UVR16x2 ist **50 kBit/s** laut Handbuch — falls deine UVR-Konfiguration eine andere Bitrate zeigt, in den CAN-Einstellungen der Web-UI anpassen, siehe Abschnitt 3.1). `install.sh` installiert und startet `can1-up.service` bereits automatisch; manuell:

```bash
sudo cp systemd/can1-up.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now can1-up
```

`scripts/can_node.py` läuft als eigener systemd-Dienst und ist der **einzige** Prozess, der den CAN-Socket öffnet — getrennt vom Orchestrator, damit ein CAN-Decoder-Fehler nicht auch die Vcontrold-Zyklen und MQTT-Befehlsverarbeitung lahmlegt. Er kommuniziert mit `orchestrator.py` ausschließlich über interne MQTT-Topics (`internal/can/tx/*`, `internal/can/rx_set/*`).

**Warum das rohe TA-CAN-Protokoll reverse-engineert werden muss:** Wir haben die offiziellen TA-Schnittstellen gründlich geprüft (CMI-JSON-API bis Version 8/2025, CoE-Anleitung) — keine davon eignet sich:
- Die **CMI-JSON-API** ist explizit nur lesend ("obtain values from all connected CAN-nodes") und auf 1 Anfrage/Minute begrenzt — für Set-Befehle und schnelle Zyklen ungeeignet.
- **CoE** (CAN over Ethernet) funktioniert nur zwischen zwei physischen C.M.I.-Geräten und ist damit für den Pi kein gangbarer Weg.
- Das rohe CAN-Netzwerk-Ein-/Ausgang-Format ist bytegenau **nirgendwo öffentlich dokumentiert** (auch nicht als CANopen/J1939/Modbus — das UVR16x2-Handbuch erwähnt "CANopen" nur einmal beiläufig im Kontext der Netzwerktopologie, implementiert aber nachweislich sein eigenes proprietäres Format, keine echten CANopen-Objektverzeichnisse/SDO/PDO).

Bestätigt ist nur die Blockstruktur (aus TAs eigener CoE-Anleitung): **analoge Netzwerkausgänge werden in 4er-Blöcken übertragen** (2 Byte pro Wert = 8 Byte Payload, genau ein CAN-Frame), **digitale in 16er-Blöcken** (1 Bit pro Wert = 2 Byte Payload). Das ist in [`scripts/ta_can_protocol.py`](scripts/ta_can_protocol.py) so umgesetzt; die eigentlichen **CAN-IDs sind Platzhalter** und müssen empirisch ermittelt werden:

1. Auf der UVR16x2 unter "CAN-Bus" > "CAN-Ausgänge" einen Testkanal mit bekanntem Wert konfigurieren (z.B. einen Analogausgang auf einen leicht wiedererkennbaren Wert wie 12,3°C stellen).
2. In der Web-UI unter **CAN-Sniffer** (siehe Abschnitt 6) für ein paar Sekunden mitschneiden.
3. Den Frame mit passendem Wert im Datenfeld identifizieren (bei 4er-Blöcken: einer der vier `int16`-Werte im 8-Byte-Payload entspricht `Wert × 10`).
4. Die gefundene CAN-ID und Kanal-Position in `config/can_mapping.json` eintragen (siehe `can_mapping.json.example` für das Format).
5. Für Set-Richtung (Pi → UVR) das gleiche Vorgehen umgekehrt: einen Wert über `orchestrator.py` (bzw. testweise direkt per `cansend`) senden und beobachten, ob die UVR ihn als Netzwerkeingang übernimmt.

Alternativ/ergänzend: `technik@ta.co.at` anschreiben und nach der CAN-Wire-Protokoll-Dokumentation für Drittanbieter-CAN-Knoten fragen (TA verkauft selbst CAN-I/O-Module, die genau das brauchen).

Config anlegen und Dienst starten (von `install.sh` bereits installiert, aber bewusst **nicht gestartet**, bis `can_mapping.json` befüllt ist):

```bash
cp config/can_mapping.json.example config/can_mapping.json
nano config/can_mapping.json   # per CAN-Sniffer ermittelte CAN-IDs eintragen
sudo systemctl start can-node
sudo systemctl status can-node
```

### 3.2 Custom CAN-Variablen (Home Assistant ↔ UVR, ohne Vitotronic)

Alle bisherigen CAN-Kanäle sind entweder Vitotronic-Werte (Pi → UVR, Namen aus `vito.xml`) oder
reine Anzeigewerte von der UVR (UVR → Pi). Für einen Wert, den Home Assistant direkt **lesen und
schreiben** soll — ohne dass die Vitotronic überhaupt beteiligt ist (z.B. ein manueller Override,
den die UVR-Programmierung selbst auswertet) — gibt es `config/can_variables.json`.

**Über die Web-UI** (empfohlen): Auf der CAN-Einstellungen-Seite ganz unten "Custom CAN-Variablen"
— Name, Typ (Number/Select) und je nach Typ Einheit/Min/Max/Step oder Optionen eintragen, speichern.

**Manuell**, falls gewünscht:

```bash
cp config/can_variables.json.example config/can_variables.json
nano config/can_variables.json
```

```json
{
  "uvr_sollwert_kollektor": {
    "discovery": {"component": "number", "unit": "°C", "min": 0, "max": 100, "step": 0.5}
  },
  "uvr_pumpe_override": {
    "discovery": {"component": "select", "options": ["AUTO", "AN", "AUS"]}
  }
}
```

Jede hier eingetragene Variable bekommt automatisch eine **schreibbare** Home-Assistant-Entity
(Number oder Select) unter dem Gerät "UVR16x2 (CAN)", mit `command_topic` auf
`uvr/cmd/<name>` (Präfix aus `MQTT_TOPIC_CMD_UVR` in `config/mqtt.env`). `can_node.py` nimmt
Werte auf diesem Topic entgegen, sendet sie direkt per CAN und spiegelt sie optimistisch auf
`uvr/<name>` zurück, damit die HA-Oberfläche sofort reagiert. Bei einer `select`-Variable wird
die gewählte Option in ihren Index (0, 1, 2, …) übersetzt, da CAN nur numerische Werte kennt —
die UVR-Programmierung muss diesen Index entsprechend auswerten.

Damit der Wert tatsächlich über CAN läuft, den **gleichen Namen** zusätzlich als Kanal in einem
Write-Slot (und optional einem Read-Slot, falls die UVR den Wert bestätigend zurücksendet) in
den CAN-Einstellungen der Web-UI eintragen — siehe Abschnitt 6.

### 3.3 CANopen/SDO — bestätigter Weg für dieses Gerät

Recherche zweier Community-Quellen (Forum-Thread
[holzheizer-forum.de/thread/61195](https://www.holzheizer-forum.de/forum/thread/61195-uvr16x2-via-can-mit-raspberry-koppeln-ohne-cmi/)
und das Python-Projekt [staircaseblog/uvr16x2logging](https://github.com/staircaseblog/uvr16x2logging))
zeigte: TA-Regler sprechen auf dem CAN-Bus **Standard-CANopen-SDO**, nicht ein rein proprietäres
Format wie in Abschnitt 3.1 angenommen. Zwischenzeitlich **gegen die echte Hardware verifiziert**:

- `candump can1` zeigte bereits laufenden Standard-SDO-Traffic (`0x641`/`0x5C1`, Node 65). Das war
  aber **nicht** die UVR selbst, sondern eine bereits bestehende, separate Verbindung des CMI zu
  seiner eigenen CMI-COE-Bridge nach Home Assistant (die dieses Projekt bewusst nicht nutzt/ersetzt).
  Die **echte, im CMI angezeigte Knotennummer der UVR ist 10** — direkt getestet, liefert exakt
  dieselben Werte, ganz ohne Umweg über die alte CoE-Verbindung.
- Kein TA-Verbindungsaufbau nötig: das Gerät antwortet direkt auf die Standard-COB-IDs
  (`0x600+NodeID`/`0x580+NodeID`, hier `0x60A`/`0x58A` für Node 10), `canopen_test.py --direct`
  (Standard) funktioniert ohne Handshake.
- Die aus `staircaseblog/uvr16x2logging` übernommenen Objektindizes (`0x8272` etc., UVR16x2-Eingang)
  existieren auf diesem Gerät **nicht** (`Object does not exist`, 0x06020000) — vermutlich andere
  Firmware-/Geräte-Variante. **Update:** per `candump` (während CMI eine "CAN-Analogausgang"-Seite
  lud) den *richtigen* Objektindex für dieses Gerät gefunden: **`0x2050`**, Subindex = Ausgangsnummer
  minus 1. Die Dekodierformel aus dem Referenzprojekt (`decode_uvr16x2_value()`) war die ganze Zeit
  korrekt — nur die Objektbasis war falsch. Bestätigt gegen 3 Werte, die exakt mit dem
  Datensatz-Decode (`decode_datensatz()`, Objekt `0x4FF4:04`) übereinstimmen. Anders als
  `0x4FF4` läuft dieser Zugriff über eine **segmentierte** (nicht Block-)SDO-Antwort.
- Stattdessen liest ein bereits vorhandener zweiter Master (vermutlich CMI) laufend **Objekt
  `0x4FF4:04`** per SDO-Block-Transfer — ein **98-Byte-Datensatz**. Mit `scripts/sdo_sniffer.py`
  passiv mitgeschnitten und gegen einen am UVR-Display abgelesenen Wert verifiziert:

  ```
  Byte 0-5:   Tag, Monat, Jahr (2-stellig), Sekunde, Minute, Stunde
  Byte 6-89:  21 Werte, je 4 Byte signed Little-Endian, /10 skaliert
  Byte 90-91: 2 Nullbyte
  Byte 92-95: 4-Byte Prüfsumme (Algorithmus noch nicht verifiziert)
  Byte 96-97: 0x0D 0x0A (CRLF-Ende)
  ```

  Slot 1 = 29,1 wurde gegen die reale Anzeige "Analogausgang 1: Vorlauftemperatur" bestätigt; Slot 19/20
  (53,1 / 54,0) passen zu den bekannten Vitotronic-Werten Kesseltemperatur-Ist/Warmwassertemperatur
  (die laut Architektur per CAN an die UVR durchgereicht werden). Welche UVR-Kanäle genau hinter den
  übrigen Slots stecken, ist noch nicht für jeden Slot einzeln bestätigt.

Testen:

```bash
sudo ip link set can1 up type can bitrate 50000
venv/bin/python scripts/canopen_test.py --read-record --uvr-node-id 10
```

Gibt bei Erfolg Datum/Uhrzeit + alle 21 Slots aus. Implementiert in `scripts/ta_canopen.py`
(`decode_datensatz()`) und nutzt intern `node.sdo.upload()` der `canopen`-Bibliothek, die den
Block-Transfer transparent handhabt.

**Wichtige Einschränkung, bestätigt an echter Hardware:** SDO-COB-IDs hängen nur vom *Server*
(UVR, Node 10) ab, nicht vom Client — `0x60A`/`0x58A` sind fix für jeden, der mit Node 10 spricht.
Falls das CMI selbst (oder die alte CoE-Verbindung) parallel denselben Kanal nutzt, können eigene
aktive Anfragen kollidieren (Timeouts/`Object does not exist` möglich, je nach Timing — direkt
gegen Node 10 bisher aber durchgehend erfolgreich). **Für reines Lesen ist `scripts/sdo_sniffer.py`
(rein passiv, keine eigene Anfrage) trotzdem die kollisionsfreie Alternative**, falls aktive Reads
doch mal unzuverlässig werden.

**Als eigener CANopen-Knoten anmelden** (z.B. damit der Pi im TA-CMI sauber auftaucht, nicht als
"Einbahnstraße"/Fehler): `--heartbeat` meldet den Pi per `ta_canopen.create_own_node()` mit Bootup,
`OPERATIONAL`-Status und laufendem Heartbeat an — zusätzlich beantwortet ein minimaler SDO-Server
(`scripts/ta_own_node.eds`) die CANopen-Pflichtobjekte (Device Type, Error Register, Identity),
ohne die ein Master zwar den Heartbeat sieht, aber auf eigene Anfragen keine Antwort bekommt:

```bash
venv/bin/python scripts/canopen_test.py --read-record --uvr-node-id 10 --own-node-id 60 --heartbeat
```

**Schreib-Pfad (Pi → UVR, Netzwerkeingang): bestätigt funktionsfähig.** Auf der UVR als
"CAN-Analogeingang" mit Knotennummer + Ausgangsnummer des Senders konfiguriert (z.B. Knotennummer
60, Ausgangsnummer 1) — die tatsächliche CAN-ID rechnet TA intern aus. COB-ID-Schema, aus zwei
unabhängigen Community-Quellen bestätigt und gegen echte Hardware verifiziert
([HA-Community-Guide](https://community.home-assistant.io/t/uvr16x2-via-canable-candlelight-home-assistant-no-c-m-i-full-guide/1011270),
FHEM-CanOverEthernet-Modul):

```
0x180 + Knotennummer: Digital-Ausgänge 1-16
0x200 + Knotennummer: Analog-Ausgänge 1-4
0x280 + Knotennummer: Analog-Ausgänge 5-8
0x300 + Knotennummer: Analog-Ausgänge 9-12
0x380 + Knotennummer: Analog-Ausgänge 13-16
```

Je 8-Byte-Frame = 4x signed int16 Little-Endian, ×10 skaliert (Ausgang N an Position (N-1)%4 im
Frame seines Blocks). Getestet mit `scripts/send_network_output_test.py`:

```bash
venv/bin/python scripts/send_network_output_test.py --own-node-id 60 --output 1 --value 12.3
```

Wert kam korrekt als `12,3 °C` am UVR-Analogeingang an — **Voraussetzung:** das Feld "Messgröße"
des CAN-Analogeingangs auf der UVR muss auf den passenden Typ (z.B. "Temperatur") gestellt werden,
sonst zeigt die UVR den rohen Ganzzahlwert unskaliert an (`123` statt `12,3`).

**Produktiv nutzbar über `can_node.py`:** `config/can_mapping.json`s `ta_network_outputs` legt fest,
welche vito.xml-Variablen als welcher Netzwerkausgang gesendet werden (`analog`/`digital`, je 16
Slots = Ausgang 1-16) — am einfachsten über die Web-UI unter **CAN-Einstellungen** → "TA-Netzwerk­
ausgänge" editierbar, kein manuelles JSON-Bearbeiten nötig. `own_node_number` (dieselbe Einstellung
wie für den Rest der CAN-Seite) bestimmt die eigene Knoten-Nummer für die COB-ID-Berechnung. Auf der
UVR muss pro gewünschtem Wert ein "CAN-Analogeingang"/"CAN-Digitaleingang" mit passender
Knotennummer + Ausgangsnummer angelegt werden (Messgröße auf einen konkreten Typ stellen, siehe
oben). `ta_can_protocol.py`s alte Blockkodierung aus Abschnitt 3.1 bleibt parallel bestehen
(`tx_analog_blocks`/`tx_digital_blocks`), falls das bestätigte Schema für einen Anwendungsfall
nicht passt.

**Lese-Pfad (UVR → Pi), produktiv über `can_node.py`: `config/can_mapping.json`s `sdo_record`
integriert das oben beschriebene, bestätigte Datensatz-Auslesen (`0x4FF4:04`) direkt in den
`can-node`-Dienst, statt es nur manuell per `canopen_test.py --read-record` zu testen:

```json
"sdo_record": {
  "uvr_node_id": 10,
  "poll_interval_seconds": 30,
  "slots": {"1": "uvr_vorlauftemperatur", "19": "TempKist", "20": "TempWWist"}
}
```

`slots` bildet Slot-Nummer (1-21, siehe Byte-Layout oben) auf einen Kanalnamen ab; `can_node.py`
fragt den Datensatz alle `poll_interval_seconds` Sekunden per SDO ab (eigener Hintergrund-Thread,
nutzt denselben zweiten CAN-Socket wie der Heartbeat) und published jeden gemappten Slot unter
`uvr/<name>` (retained) -- inklusive automatischer Home-Assistant-Discovery, genau wie
`rx_analog_blocks`/`rx_digital_blocks`. **Vorteil gegenüber `rx_analog_blocks`/`rx_digital_blocks`:**
braucht keine "CAN-Netzwerkausgang"-Konfiguration auf der UVR-Seite und keine per Sniffer ermittelten
CAN-IDs -- nur die UVR-Node-ID (siehe Abschnitt 3.3) und die gewünschten Slot-Nummern. Welcher Slot
welchem UVR-Kanal entspricht, per `canopen_test.py --read-record --uvr-node-id <N>` oder
`sdo_sniffer.py` ermitteln (Slot 1 = Analogausgang 1 ist für dieses Gerät bereits bestätigt).

**Noch offen:** Prüfsummen-Algorithmus des `0x4FF4`-Datensatzes (nicht sicherheitskritisch für
reines Auslesen), genaue Kanalzuordnung der restlichen Datensatz-Slots, und die Digital-Ausgang-
Kodierung (`0x180+Node`, Bitmaske) ist noch nicht gegen echte Hardware getestet (nur analog
bestätigt).

## 4. Home Assistant einbinden

**Automatisch per MQTT-Discovery (Standard):** `orchestrator.py` published beim Start automatisch
Discovery-Konfigurationen für alle Datenpunkte aus `read_cycles.json` (als Sensoren) und alle
Set-fähigen Einträge aus `command_map.json`, die einen `"discovery"`-Block haben (als Number/Select-
Entities, siehe `config/command_map.json.example`). Home Assistant legt die Entities dann von selbst
an, gruppiert unter einem gemeinsamen Gerät "Vitogas 100 (vcontrold)" — kein manuelles Editieren von
`configuration.yaml` nötig. Voraussetzung: MQTT-Discovery ist in der Home-Assistant-MQTT-Integration
aktiviert (Standardeinstellung) und `MQTT_DISCOVERY_PREFIX` in `config/mqtt.env` stimmt mit dem dort
konfigurierten Präfix überein (`homeassistant` bei beiden ist der Standard). Deaktivieren:
`MQTT_DISCOVERY_ENABLED=false` in `config/mqtt.env`.

Um einem weiteren Datenpunkt eine Number/Select-Entity zu geben, in `config/command_map.json` einen
`"discovery"`-Block ergänzen (`{"component": "number", "unit": "...", "min": ..., "max": ..., "step": ...}`
oder `{"component": "select", "options": [...]}`) und `orchestrator` neu starten.

**CAN-Empfangswerte (UVR → Pi):** Diese haben keine Entsprechung in `vito.xml` und werden deshalb
separat von `can_node.py` discovered — jeder in `config/can_mapping.json` unter `rx_analog_blocks`/
`rx_digital_blocks` konfigurierte Kanal bekommt automatisch eine Sensor-Entity unter einem eigenen
Gerät "UVR16x2 (CAN)" in Home Assistant, sobald `can-node` (neu) startet. Gilt für jeden Kanalnamen,
egal ob er zufällig mit einer vito.xml-Variable übereinstimmt oder komplett frei erfunden ist.

**Alternativ manuell:** `homeassistant/configuration_snippet.yaml` enthält dieselben Entities als
statische YAML-Konfiguration, falls du kein Discovery nutzen möchtest.

## 5. Web-UI (Konsole, Config-Import, MQTT-Einstellungen, Diagnose, CAN-Sniffer)

Im Ordner `ui/` liegt eine kleine Flask-App zum Testen und Verwalten:

- **Konsole**: Getter/Setter aus deiner Geräte-XML per Dropdown auswählen oder frei eingeben, direkt per `vclient` ausführen. Set-Befehle erfordern eine Bestätigung.
- **Config**: `vcontrold.xml` und `vito.xml` je in einem ein-/ausklappbaren Bereich — pro Datei entweder als Ganzes importieren (Upload) oder direkt als Text bearbeiten. Validiert XML vor dem Speichern, legt automatisch ein Backup an, startet `vcontrold` neu. Editor über die volle Bildbreite.
- **MQTT-Einstellungen**: `config/mqtt.env` (Broker-Host, Port, Zugangsdaten, Topic-Präfixe) direkt im Browser bearbeiten und die Verbindung testen. Beim Speichern werden bereits laufende Dienste (`orchestrator`, `can-node`) automatisch neu gestartet — kein manuelles Editieren per SSH mehr nötig.
- **CAN-Einstellungen**: `config/can_mapping.json` im Browser bearbeiten — Bitrate, eigene Knoten-Nummer, und je Spalte (Senden/Empfangen × Analog/Digital) 16 Slots, jeder per Dropdown mit einer vito.xml-Variable belegbar. Speichern startet `can-node` (falls aktiv) automatisch neu.
- **Variablen**: die zentrale Seite — zeigt alle aus `vito.xml` extrahierten Getter/Setter als Tabelle. Pro Variable per Dropdown einem Zyklus zuordnen (ersetzt manuelles Editieren von `config/read_cycles.json`) und per Checkbox festlegen, ob sie per MQTT/CAN setzbar sein soll, inkl. Home-Assistant-Discovery-Metadaten (Einheit, Min/Max/Step oder Auswahloptionen). Ersetzt manuelles Editieren von `config/command_map.json`. Speichern startet `orchestrator` (falls aktiv) automatisch neu.
- **Diagnose**: Status aller Dienste (vcontrold, orchestrator, can-node, can1-up), Live-Logs, MQTT-Verbindungstest, CAN-Interface-Status.
- **CAN-Sniffer**: zeichnet für N Sekunden rohe CAN-Frames auf — der zentrale Baustein, um die CAN-IDs für `config/can_mapping.json` empirisch zu ermitteln (siehe Abschnitt 3).

`install.sh` legt `ui/ui.env` aus der Vorlage an und startet den Dienst bereits automatisch. Danach unbedingt:

```bash
nano ui/ui.env   # UI_USERNAME/UI_PASSWORD ändern! DEVICE_XML_PATH auf deine Geräte-XML setzen
sudo systemctl restart vcontrold-ui
```

Erreichbar unter `http://<pi-ip>:5000` (läuft **als root**, da Config-Import nach `/etc/` schreibt und `systemctl restart` ausführt).

**Sicherheitshinweis:** Die UI kann Sollwerte an die echte Heizung senden. Nur im vertrauenswürdigen LAN betreiben (nicht ins Internet weiterleiten), starkes Passwort in `ui.env` setzen. Basic-Auth über HTTP ist unverschlüsselt — bei Bedarf zusätzlich per Reverse-Proxy (z.B. Caddy/nginx) mit HTTPS absichern.

## Offene Punkte, die nur du klären kannst

1. ~~Protokoll der Vitotronic~~ — **erledigt:** V200KW1, Device-ID `2094`, KW-Protokoll (siehe `config/device-vitogas100-v200kw1/`).
2. **CAN-IDs der UVR-Netzwerk-Ein-/Ausgänge** — bytegenau nicht öffentlich dokumentiert, muss per CAN-Sniffer empirisch ermittelt und in `config/can_mapping.json` eingetragen werden (siehe Abschnitt 3).
3. ~~CAN-HAT-Modell~~ — **erledigt:** Waveshare 2-CH CAN HAT+ (MCP2515 über SPI1, siehe Abschnitt 3).
4. **MQTT-Zugangsdaten** des Home-Assistant-Mosquitto-Brokers (Host/User/Passwort) in `config/mqtt.env`.

## Troubleshooting: vcontrold.service startet nicht

Falls `sudo systemctl status vcontrold` `failed` zeigt, in dieser Reihenfolge prüfen:

1. **Rate-Limit von systemd:** Nach mehreren Fehlversuchen kurz hintereinander blockiert systemd
   weitere Startversuche ("Start request repeated too quickly"), auch nach `systemctl restart`.
   Erst zurücksetzen: `sudo systemctl reset-failed vcontrold`, dann erneut `restart`.
2. **Echten Fehler finden:** Die normale `journalctl`-Ausgabe zeigt meist nur systemd-Rahmenmeldungen.
   Die eigentliche Fehlerursache steht in den Zeilen von vcontrold selbst:
   ```bash
   sudo journalctl -u vcontrold.service --no-pager -n 50 | grep 'vcontrold\['
   ```
3. **`failed to load external entity "/etc/vcontrold.xml"`:** Die installierte systemd-Unit zeigt noch
   auf den alten, falschen Pfad. `sudo cp systemd/vcontrold.service /etc/systemd/system/vcontrold.service`,
   `sudo systemctl daemon-reload`, dann neu starten (siehe Abschnitt 1).
4. **`Could not open /tmp/vcontrold.log: Permission denied`:** vcontrold startet als root, legt die Logdatei
   root-only an und gibt danach intern Rechte ab (auf `nobody`) — beim nächsten Start darf es die eigene
   Logdatei dann nicht mehr öffnen. Die aktuelle `systemd/vcontrold.service` räumt das per `ExecStartPre`
   automatisch auf; bei einer älteren installierten Unit hilft `sudo rm -f /tmp/vcontrold.log` vor dem Neustart.
5. **`SRV ERR: command unknown` bei `vclient`:** `/etc/vcontrold/vito.xml` enthält nicht die erwarteten
   Kommandos (z.B. weil dort noch die generische Upstream-Config statt `config/device-vitogas100-v200kw1/`
   liegt). Prüfen mit `cat /etc/vcontrold/vcontrold.xml | grep device` (sollte `ID="2094"` zeigen) und ggf.
   manuell überschreiben (Befehle siehe Abschnitt 1).
6. **`Error communicating with the server` bei `vclient`:** vcontrold läuft, kann aber nicht mit der Heizung
   sprechen — meist weil der Optolink-USB-Adapter nicht eingesteckt ist oder `<tty>` in `vcontrold.xml` nicht
   auf das richtige Gerät zeigt (`ls -l /dev/ttyUSB0` bzw. `/dev/optolink` prüfen).

## Troubleshooting: orchestrator.service verbindet sich nicht mit MQTT

`ConnectionRefusedError` in `journalctl -u orchestrator.service` bedeutet: der Host ist erreichbar,
aber nichts hört auf dem angegebenen Port. Häufigste Ursache: **`homeassistant.local` (mDNS) löst
im eigenen Netzwerk auf die falsche IP auf.** Prüfen:

```bash
getent hosts homeassistant.local
nc -zv homeassistant.local 1883   # falls "Connection refused": IP stimmt nicht
nc -zv <echte-ip-von-home-assistant> 1883   # zum Vergleich
```

Falls die mDNS-Auflösung falsch liegt: in `config/mqtt.env` statt des Hostnamens direkt die
**feste IP-Adresse** deines Home-Assistant-Rechners eintragen, dann `sudo systemctl restart orchestrator`.
