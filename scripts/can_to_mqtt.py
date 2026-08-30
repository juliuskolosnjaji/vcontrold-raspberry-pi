#!/usr/bin/env python3
"""
Liest CAN-Frames von der Technische-Alternative-UVR (can0) und published sie per MQTT.

Läuft dauerhaft als systemd-Dienst (siehe systemd/can-to-mqtt.service).

TODO: FRAME_MAP an das tatsächliche CAN-Protokoll deiner UVR anpassen. Die CAN-IDs
und Byte-Layouts sind proprietär (Technische Alternative) und müssen anhand der
UVR-Konfiguration bzw. Community-Dokumentation ermittelt werden. Der Code unten
zeigt nur das Grundgerüst: rohe Frames loggen, dann Mapping ergänzen.
"""
import struct
import sys

import can

from mqtt_common import make_client

CAN_INTERFACE = "can0"

# Beispiel-Mapping CAN-ID -> (MQTT-Subtopic, Decoder-Funktion für data-bytes)
# Platzhalter — mit echten IDs/Skalierungen deiner UVR befüllen.
FRAME_MAP = {
    # 0x100: ("kollektortemperatur", lambda data: struct.unpack(">h", data[0:2])[0] / 10.0),
    # 0x101: ("speichertemperatur", lambda data: struct.unpack(">h", data[0:2])[0] / 10.0),
}


def main() -> None:
    client, env = make_client("can-to-mqtt")
    topic_prefix = env.get("MQTT_TOPIC_UVR", "uvr")

    bus = can.interface.Bus(channel=CAN_INTERFACE, interface="socketcan")
    print(f"Höre auf {CAN_INTERFACE} ...")

    try:
        for msg in bus:
            mapping = FRAME_MAP.get(msg.arbitration_id)
            if mapping is None:
                # Unbekannte ID: zum Ermitteln des Protokolls mitloggen
                print(f"Unbekannter Frame: id=0x{msg.arbitration_id:x} data={msg.data.hex()}", file=sys.stderr)
                continue
            subtopic, decode = mapping
            try:
                value = decode(msg.data)
            except Exception as exc:  # Decoder-Fehler nicht den ganzen Dienst abschießen lassen
                print(f"Decoder-Fehler für 0x{msg.arbitration_id:x}: {exc}", file=sys.stderr)
                continue
            client.publish(f"{topic_prefix}/{subtopic}", value, retain=True)
    finally:
        bus.shutdown()
        client.disconnect()


if __name__ == "__main__":
    main()
