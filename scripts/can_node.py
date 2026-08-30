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

import ha_discovery
import ta_can_protocol as proto
from mqtt_common import make_client

CAN_INTERFACE = "can0"
CAN_MAPPING_PATH = pathlib.Path(__file__).resolve().parent.parent / "config" / "can_mapping.json"

# Interne MQTT-Topics (Glue zwischen Orchestrator und CAN-Node, gleicher Broker)
TOPIC_TX_VALUE = "internal/can/tx"          # Orchestrator -> CAN-Node: aktueller Wert für einen tx-Kanal
TOPIC_RX_SETREQUEST = "internal/can/rx_set"  # CAN-Node -> Orchestrator: UVR fordert Set an


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


def load_mapping() -> dict:
    if not CAN_MAPPING_PATH.exists():
        raise FileNotFoundError(
            f"{CAN_MAPPING_PATH} fehlt. Kopiere config/can_mapping.json.example dorthin "
            "und trage die per CAN-Sniffer ermittelten CAN-IDs ein."
        )
    return json.loads(CAN_MAPPING_PATH.read_text())


def configure_interface(interface: str, bitrate: int) -> None:
    """Setzt die konfigurierte Bitrate, unabhängig davon, was can0-up.service schon gesetzt hat."""
    subprocess.run(["ip", "link", "set", interface, "down"], check=False)
    result = subprocess.run(
        ["ip", "link", "set", interface, "up", "type", "can", "bitrate", str(bitrate)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"Konnte {interface} nicht mit Bitrate {bitrate} konfigurieren: {result.stderr}", file=sys.stderr)


def main() -> None:
    mapping = load_mapping()
    client, env = make_client("can-node")
    uvr_topic_prefix = env.get("MQTT_TOPIC_UVR", "uvr")
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

    rx_analog_by_id = {
        int(b["can_id"], 0): b for b in mapping.get("rx_analog_blocks", []) if b.get("active", True)
    }
    rx_digital_by_id = {
        int(b["can_id"], 0): b["channels"] for b in mapping.get("rx_digital_blocks", []) if b.get("active", True)
    }

    def send_tx_analog_blocks():
        for block in mapping.get("tx_analog_blocks", []):
            if not block.get("active", True):
                continue
            values = [tx_values.get(ch) for ch in block["channels"]]
            data = proto.encode_analog_block(values, value_bytes=block.get("value_bytes", 2))
            bus.send(can.Message(arbitration_id=int(block["can_id"], 0), data=data, is_extended_id=False))

    def send_tx_digital_blocks():
        for block in mapping.get("tx_digital_blocks", []):
            if not block.get("active", True):
                continue
            values = [bool(tx_values.get(ch)) if ch else None for ch in block["channels"]]
            data = proto.encode_digital_block(values)
            bus.send(can.Message(arbitration_id=int(block["can_id"], 0), data=data, is_extended_id=False))

    def on_connect(mqtt_client, userdata, flags, rc):
        mqtt_client.subscribe(f"{TOPIC_TX_VALUE}/#")
        print(f"Abonniert: {TOPIC_TX_VALUE}/#")
        if discovery_enabled:
            ha_discovery.publish_can_discovery(mqtt_client, discovery_prefix, mapping, uvr_topic_prefix)
            print(f"CAN-MQTT-Discovery published (Prefix: {discovery_prefix})")

    def on_message(mqtt_client, userdata, msg):
        channel = msg.topic.rsplit("/", 1)[-1]
        try:
            tx_values[channel] = float(msg.payload.decode())
        except ValueError:
            print(f"Ungültiger Wert für {channel}: {msg.payload!r}", file=sys.stderr)
            return
        send_tx_analog_blocks()
        send_tx_digital_blocks()

    client.on_connect = on_connect
    client.on_message = on_message
    client.subscribe(f"{TOPIC_TX_VALUE}/#")
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

            else:
                print(
                    f"Unbekannter Frame: id=0x{msg.arbitration_id:x} data={msg.data.hex()} "
                    "(zum Ermitteln der Zuordnung: CAN-Sniffer in der Web-UI nutzen)",
                    file=sys.stderr,
                )
    finally:
        bus.shutdown()
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    main()
