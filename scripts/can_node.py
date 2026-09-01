#!/usr/bin/env python3
"""
CAN-Node: alleiniger Besitzer des CAN-Sockets zur Technische-Alternative-UVR.

Aufgaben (siehe README Abschnitt 3):
  - Sendet analoge/digitale Netzwerkausgänge (aktuelle Vcontrold-Werte) blockweise
    an die UVR, sobald der Orchestrator neue Werte über interne MQTT-Topics liefert.
  - Empfängt CAN-Frames von der UVR (deren Netzwerkausgänge = unsere Netzwerkeingänge),
    dekodiert sie und published die Werte sowohl direkt für Home Assistant
    (MQTT_TOPIC_UVR) als auch als "on demand set"-Anfrage für den Orchestrator,
    falls der Kanal als beschreibbar gemappt ist.
  - Custom CAN-Variablen (config/can_variables.json): komplett unabhängig von
    vcontrold/vito.xml. Home Assistant kann sie direkt per MQTT (MQTT_TOPIC_CMD_UVR)
    setzen, der Wert geht direkt per CAN an die UVR -- die Vitotronic ist nicht beteiligt.
  - Meldet den Pi dauerhaft per CANopen-Heartbeat als eigenen Knoten an (own_node_number),
    damit er in einer CAN-Bus-Übersicht (z.B. TA-CMI) als vorhandenes Gerät sichtbar ist
    (siehe README Abschnitt 3.3 -- Geräteerkennung selbst bleibt unvollständig, TA-eigenes
    Protokoll dafür nicht reverse-engineert).

Läuft als eigener systemd-Dienst (can-node.service), getrennt vom Orchestrator,
damit ein Fehler in der CAN-Dekodierung nicht die Vcontrold-Zyklen/MQTT-Befehle
des Orchestrators mit runterreißt.

WICHTIG: config/can_mapping.json muss vor Produktivbetrieb mit echten CAN-IDs
befüllt werden (siehe ta_can_protocol.py und README Abschnitt 3 -- CAN-Sniffer).
"""
import json
import pathlib
import subprocess
import sys

import can
import canopen

import ha_discovery
import ta_can_protocol as proto
import ta_canopen as ta
from mqtt_common import make_client

CAN_INTERFACE = "can1"
CONFIG_DIR = pathlib.Path(__file__).resolve().parent.parent / "config"
CAN_MAPPING_PATH = CONFIG_DIR / "can_mapping.json"
CAN_VARIABLES_PATH = CONFIG_DIR / "can_variables.json"

# Interne MQTT-Topics (Glue zwischen Orchestrator und CAN-Node, gleicher Broker)
TOPIC_TX_VALUE = "internal/can/tx"          # Orchestrator -> CAN-Node: aktueller Wert für einen tx-Kanal
TOPIC_RX_SETREQUEST = "internal/can/rx_set"  # CAN-Node -> Orchestrator: UVR fordert Set an


def _chunk4(items: list) -> list:
    """Teilt eine Liste in Blöcke zu je 4 auf (mit None aufgefüllt), max. 4 Blöcke
    (16 Analog-Ausgänge gesamt, siehe ta_canopen.ANALOG_OUTPUT_COB_ID_BASES)."""
    padded = (list(items) + [None] * 16)[:16]
    return [padded[i : i + 4] for i in range(0, 16, 4)]


def publish_rx_value(client, uvr_topic_prefix: str, channel, value) -> None:
    """
    Published einen von der UVR empfangenen Wert.

    `channel` ist entweder:
      - None: Slot unbelegt, ignorieren.
      - ein String: reiner Anzeigewert für Home Assistant (uvr/<channel>).
      - ein Objekt {"topic": "...", "forward_as_set": "<command_map-Key>"}:
        zusätzlich als "on demand set"-Anfrage an den Orchestrator weiterleiten
        (z.B. wenn die UVR-Programmierung einen Sollwert an die Vitotronic
        durchreichen soll, siehe README Abschnitt 3).
    """
    if channel is None:
        return
    if isinstance(channel, dict):
        topic = channel["topic"]
        client.publish(f"{uvr_topic_prefix}/{topic}", value, retain=True)
        forward_key = channel.get("forward_as_set")
        if forward_key:
            client.publish(f"{TOPIC_RX_SETREQUEST}/{forward_key}", value)
    else:
        client.publish(f"{uvr_topic_prefix}/{channel}", value, retain=True)


def load_sdo_slots(sdo_config: dict | None) -> dict[int, object]:
    """Liest config/can_mapping.json's 'sdo_record.slots' (Slot-Nummer 1-21 -> Kanalname)."""
    if not sdo_config:
        return {}
    slots = {int(slot): channel for slot, channel in sdo_config.get("slots", {}).items()}
    if not slots:
        print("sdo_record.slots ist leer, SDO-Datensatz-Auswertung liefert keine Werte", file=sys.stderr)
    return slots


def handle_sdo_record_frame(
    msg: can.Message, sdo_trackers: dict, sdo_slots: dict, sdo_filter_node: int | None, client, uvr_topic_prefix: str
) -> bool:
    """Passives Mitlesen des UVR-Datensatzes (Objekt 0x4FF4:04) aus dem ohnehin laufenden
    CAN-Empfang, statt einer eigenen aktiven SDO-Anfrage (siehe ta_canopen.process_sdo_frame
    für die Begründung -- an echter Hardware hat sich gezeigt, dass die im CMI angezeigte
    UVR-Node-ID bei aktiver Anfrage "Object does not exist" liefern kann, während ein bereits
    vorhandener zweiter Master denselben Datensatz unter einer anderen Node-ID erfolgreich und
    laufend abfragt -- passives Mitlesen funktioniert unabhängig von der korrekten Node-ID und
    kollidiert nie mit fremden aktiven Anfragen). Gibt True zurück, wenn das Frame als
    SDO-Server-Antwort erkannt wurde (auch wenn es nur ein Zwischen-Segment war), sonst False."""
    result = ta.process_sdo_frame(msg.arbitration_id, msg.data, sdo_trackers)
    if not (ta.SDO_SERVER_TO_CLIENT_BASE <= msg.arbitration_id < ta.SDO_SERVER_TO_CLIENT_BASE + 0x80):
        return False
    if result is None:
        return True
    node_id, record = result
    if sdo_filter_node is not None and node_id != sdo_filter_node:
        return True
    for slot, channel in sdo_slots.items():
        index = slot - 1
        if 0 <= index < len(record["values"]):
            publish_rx_value(client, uvr_topic_prefix, channel, record["values"][index])
        else:
            print(
                f"sdo_record: Slot {slot} außerhalb des Datensatzes ({len(record['values'])} Werte)",
                file=sys.stderr,
            )
    return True


def load_mapping() -> dict:
    if not CAN_MAPPING_PATH.exists():
        raise FileNotFoundError(
            f"{CAN_MAPPING_PATH} fehlt. Kopiere config/can_mapping.json.example dorthin "
            "und trage die per CAN-Sniffer ermittelten CAN-IDs ein."
        )
    return json.loads(CAN_MAPPING_PATH.read_text())


def load_can_variables() -> dict:
    if not CAN_VARIABLES_PATH.exists():
        return {}
    return {k: v for k, v in json.loads(CAN_VARIABLES_PATH.read_text()).items() if isinstance(v, dict)}


def resolve_numeric_value(can_variables: dict, channel: str, payload: str) -> float | None:
    """Wandelt eine eingehende MQTT-Payload in einen numerischen CAN-Wert um. Bei einer
    'select'-Variable wird die Option in ihren Index übersetzt (0, 1, 2, ...), da CAN
    nur numerische Werte kennt."""
    discovery_opts = can_variables.get(channel, {}).get("discovery", {})
    if discovery_opts.get("component") == "select":
        options = discovery_opts.get("options", [])
        if payload in options:
            return float(options.index(payload))
        print(f"Unbekannte Option '{payload}' für '{channel}' (erwartet: {options})", file=sys.stderr)
        return None
    try:
        return float(payload)
    except ValueError:
        print(f"Ungültiger Wert für {channel}: {payload!r}", file=sys.stderr)
        return None


def configure_interface(interface: str, bitrate: int) -> None:
    """Setzt die konfigurierte Bitrate, unabhängig davon, was can1-up.service schon gesetzt hat."""
    subprocess.run(["ip", "link", "set", interface, "down"], check=False)
    result = subprocess.run(
        ["ip", "link", "set", interface, "up", "type", "can", "bitrate", str(bitrate)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"Konnte {interface} nicht mit Bitrate {bitrate} konfigurieren: {result.stderr}", file=sys.stderr)

    # Standard-Sendequeue (txqueuelen) ist bei CAN-Interfaces oft sehr klein (z.B. 10). Bei
    # mehreren fast gleichzeitigen Sends (jede eingehende internal/can/tx-Nachricht löst einen
    # kompletten Neuversand aller Blöcke aus) plus einem ohnehin stark ausgelasteten Bus
    # (CMI<->UVR-Dauerverkehr) reicht das nicht -- der Kernel meldet dann ENOBUFS ("No buffer
    # space available"), siehe README Abschnitt 3.3. Größere Queue puffert den Burst ab, statt
    # Frames sofort zu verwerfen.
    result = subprocess.run(
        ["ip", "link", "set", interface, "txqueuelen", "128"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"Konnte txqueuelen für {interface} nicht setzen: {result.stderr}", file=sys.stderr)


def main() -> None:
    mapping = load_mapping()
    can_variables = load_can_variables()
    client, env = make_client("can-node")
    uvr_topic_prefix = env.get("MQTT_TOPIC_UVR", "uvr")
    uvr_cmd_topic_prefix = env.get("MQTT_TOPIC_CMD_UVR", "uvr/cmd")
    discovery_enabled = env.get("MQTT_DISCOVERY_ENABLED", "true").lower() not in ("false", "0", "no")
    discovery_prefix = env.get("MQTT_DISCOVERY_PREFIX", "homeassistant")

    configure_interface(CAN_INTERFACE, mapping.get("bitrate", proto.DEFAULT_BITRATE))

    # Aktueller Wertespeicher für tx-Kanäle (vom Orchestrator zuletzt gemeldete Werte)
    tx_values: dict[str, float] = {}

    try:
        bus = can.interface.Bus(channel=CAN_INTERFACE, interface="socketcan")
    except OSError as exc:
        print(f"Konnte {CAN_INTERFACE} nicht öffnen: {exc}", file=sys.stderr)
        sys.exit(1)

    # Eigener, zweiter CAN-Socket nur für den Heartbeat -- getrennt vom Haupt-`bus`, damit sich
    # dessen Notifier-Thread (canopen-Bibliothek) nicht mit der direkten `for msg in bus`-Schleife
    # unten um eingehende Frames streitet (SocketCAN erlaubt beliebig viele Sockets pro Interface,
    # jeder bekommt unabhängig eine Kopie jedes Frames). Meldet den Pi per Bootup+laufendem
    # Heartbeat als CANopen-Knoten an (siehe README Abschnitt 3.3) -- ohne das taucht der Pi in
    # keiner CAN-Bus-Übersicht (z.B. TA-CMI) als vorhandenes Gerät auf.
    own_node_number = mapping.get("own_node_number", 1)
    heartbeat_bus = can.interface.Bus(channel=CAN_INTERFACE, interface="socketcan")
    heartbeat_network = canopen.Network()
    heartbeat_network.bus = heartbeat_bus
    heartbeat_network.notifier = can.Notifier(heartbeat_bus, heartbeat_network.listeners)
    own_node = ta.create_own_node(heartbeat_network, own_node_number)
    print(f"Heartbeat gestartet (eigene Node-ID {own_node_number})")

    sdo_config = mapping.get("sdo_record")
    sdo_slots = load_sdo_slots(sdo_config)
    sdo_filter_node = sdo_config.get("uvr_node_id") if sdo_config else None
    sdo_trackers: dict = {}
    if sdo_slots:
        node_desc = f"nur Node {sdo_filter_node}" if sdo_filter_node is not None else "jede Node-ID"
        print(f"Passives SDO-Datensatz-Parsing aktiv ({node_desc}, {len(sdo_slots)} Slot(s) gemappt)")

    rx_analog_by_id = {
        int(b["can_id"], 0): b for b in mapping.get("rx_analog_blocks", []) if b.get("active", True)
    }
    rx_digital_by_id = {
        int(b["can_id"], 0): b["channels"] for b in mapping.get("rx_digital_blocks", []) if b.get("active", True)
    }

    def safe_send(message: can.Message) -> None:
        """Sendet mit Timeout statt unbegrenzt zu blockieren -- ohne Timeout kann ein
        blockierendes bus.send() (z.B. bei Bus-Off/Error-Passive, kein ACK, TX-Puffer voll)
        den MQTT-Netzwerk-Thread (der on_message synchron ausführt) für immer einfrieren:
        alle künftigen tx-Werte kommen dann nie mehr an, während die separate Lese-Schleife
        (eigener Thread) unbeeinflusst weiterläuft -- genau das durch Logs beobachtete
        Symptom ("Unbekannter Frame" läuft weiter, TA-Netzwerkausgang-Zeilen frieren ein)."""
        try:
            bus.send(message, timeout=1.0)
        except can.CanError as exc:
            print(f"CAN-Sendefehler (id=0x{message.arbitration_id:x}): {exc}", file=sys.stderr)

    def send_tx_analog_blocks():
        for block in mapping.get("tx_analog_blocks", []):
            if not block.get("active", True):
                continue
            values = [tx_values.get(ch) for ch in block["channels"]]
            data = proto.encode_analog_block(values, value_bytes=block.get("value_bytes", 2))
            safe_send(can.Message(arbitration_id=int(block["can_id"], 0), data=data, is_extended_id=False))

    def send_tx_digital_blocks():
        for block in mapping.get("tx_digital_blocks", []):
            if not block.get("active", True):
                continue
            values = [bool(tx_values.get(ch)) if ch else None for ch in block["channels"]]
            data = proto.encode_digital_block(values)
            safe_send(can.Message(arbitration_id=int(block["can_id"], 0), data=data, is_extended_id=False))

    ta_outputs = mapping.get("ta_network_outputs")

    def send_ta_network_outputs():
        """Sendet Werte als TA-Netzwerkausgänge (bestätigtes Schema, siehe ta_canopen.py),
        an eine auf der UVR konfigurierte 'CAN-Analogeingang'/'CAN-Digitaleingang'-Zuordnung
        mit Knotennummer = own_node_number (dieselbe Einstellung wie auf der CAN-Einstellungen-Seite)."""
        if not ta_outputs:
            return
        own_node_id = mapping.get("own_node_number", 1)
        for block_index, channels in enumerate(_chunk4(ta_outputs.get("analog", []))):
            values = [tx_values.get(ch) if ch else None for ch in channels]
            if all(v is None for v in values):
                continue
            data = ta.encode_analog_outputs(values)
            cob_id = ta.ANALOG_OUTPUT_COB_ID_BASES[block_index] | own_node_id
            safe_send(can.Message(arbitration_id=cob_id, data=data, is_extended_id=False))
            print(f"TA-Netzwerkausgang analog Block {block_index + 1}: {list(zip(channels, values))} "
                  f"-> COB-ID 0x{cob_id:x} data={data.hex()}")
        digital_channels = (ta_outputs.get("digital", []) + [None] * 16)[:16]
        if any(ch for ch in digital_channels):
            values = [bool(tx_values.get(ch)) if ch else None for ch in digital_channels]
            data = ta.encode_digital_outputs(values)
            cob_id = ta.DIGITAL_OUTPUT_COB_ID_BASE | own_node_id
            safe_send(can.Message(arbitration_id=cob_id, data=data, is_extended_id=False))
            print(f"TA-Netzwerkausgang digital: {list(zip(digital_channels, values))} "
                  f"-> COB-ID 0x{cob_id:x} data={data.hex()}")

    def on_connect(mqtt_client, userdata, flags, rc):
        mqtt_client.subscribe(f"{TOPIC_TX_VALUE}/#")
        mqtt_client.subscribe(f"{uvr_cmd_topic_prefix}/#")
        print(f"Abonniert: {TOPIC_TX_VALUE}/# und {uvr_cmd_topic_prefix}/#")
        if discovery_enabled:
            ha_discovery.publish_can_discovery(
                mqtt_client, discovery_prefix, mapping, uvr_topic_prefix, uvr_cmd_topic_prefix, can_variables
            )
            print(f"CAN-MQTT-Discovery published (Prefix: {discovery_prefix})")

    def on_message(mqtt_client, userdata, msg):
        channel = msg.topic.rsplit("/", 1)[-1]
        payload = msg.payload.decode()
        value = resolve_numeric_value(can_variables, channel, payload)
        if value is None:
            return
        tx_values[channel] = value
        if msg.topic.startswith(uvr_cmd_topic_prefix):
            # Custom CAN-Variable (uvr/cmd/<name>): sofortiges optimistisches Echo auf den
            # State-Topic, statt auf eine CAN-Antwort der UVR zu warten.
            mqtt_client.publish(f"{uvr_topic_prefix}/{channel}", payload, retain=True)
        send_tx_analog_blocks()
        send_tx_digital_blocks()
        send_ta_network_outputs()

    client.on_connect = on_connect
    client.on_message = on_message
    client.subscribe(f"{TOPIC_TX_VALUE}/#")
    client.subscribe(f"{uvr_cmd_topic_prefix}/#")
    client.loop_start()

    print(f"Höre auf {CAN_INTERFACE} für UVR-Netzwerkausgänge ...")
    try:
        for msg in bus:
            if msg.arbitration_id in rx_analog_by_id:
                block = rx_analog_by_id[msg.arbitration_id]
                channels = block["channels"]
                try:
                    values = proto.decode_analog_block(msg.data, value_bytes=block.get("value_bytes", 2))
                except ValueError as exc:
                    print(f"Decoder-Fehler (analog) für 0x{msg.arbitration_id:x}: {exc}", file=sys.stderr)
                    continue
                for channel, value in zip(channels, values):
                    publish_rx_value(client, uvr_topic_prefix, channel, value)

            elif msg.arbitration_id in rx_digital_by_id:
                channels = rx_digital_by_id[msg.arbitration_id]
                try:
                    values = proto.decode_digital_block(msg.data)
                except ValueError as exc:
                    print(f"Decoder-Fehler (digital) für 0x{msg.arbitration_id:x}: {exc}", file=sys.stderr)
                    continue
                for channel, value in zip(channels, values):
                    publish_rx_value(client, uvr_topic_prefix, channel, "ON" if value else "OFF")

            elif sdo_slots and handle_sdo_record_frame(
                msg, sdo_trackers, sdo_slots, sdo_filter_node, client, uvr_topic_prefix
            ):
                pass  # SDO-Server-Antwort (0x580+Node) erkannt und verarbeitet, kein unbekanntes Frame

            else:
                print(
                    f"Unbekannter Frame: id=0x{msg.arbitration_id:x} data={msg.data.hex()} "
                    "(zum Ermitteln der Zuordnung: CAN-Sniffer in der Web-UI nutzen)",
                    file=sys.stderr,
                )
    finally:
        own_node.nmt.stop_heartbeat()
        heartbeat_network.notifier.stop()
        heartbeat_bus.shutdown()
        bus.shutdown()
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    main()
