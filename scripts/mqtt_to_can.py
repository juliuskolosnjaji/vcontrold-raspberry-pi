#!/usr/bin/env python3
"""
Abonniert MQTT-Kommando-Topics und sendet daraufhin CAN-Frames an die UVR.

Läuft dauerhaft als systemd-Dienst (siehe systemd/mqtt-to-can.service).

TODO: COMMAND_MAP an das tatsächliche CAN-Protokoll deiner UVR anpassen (CAN-ID,
Byte-Layout, Skalierung). Ohne die korrekte TA-Spezifikation kann die UVR die
Frames nicht interpretieren — dies ist ein Grundgerüst.
"""
import struct
import sys

import can
import paho.mqtt.client as mqtt

from mqtt_common import load_env

CAN_INTERFACE = "can0"

# Beispiel-Mapping: MQTT-Subtopic (unterhalb von MQTT_TOPIC_CMD_UVR) -> (CAN-ID, Encoder-Funktion)
# Platzhalter — mit echten IDs/Skalierungen deiner UVR befüllen.
COMMAND_MAP = {
    # "pumpe_an_aus": (0x200, lambda payload: bytes([1 if payload == "ON" else 0])),
    # "sollwert_speicher": (0x201, lambda payload: struct.pack(">h", int(float(payload) * 10))),
}


def main() -> None:
    env = load_env()
    cmd_topic_prefix = env.get("MQTT_TOPIC_CMD_UVR", "uvr/cmd")

    bus = can.interface.Bus(channel=CAN_INTERFACE, interface="socketcan")

    def on_connect(client, userdata, flags, rc):
        client.subscribe(f"{cmd_topic_prefix}/#")
        print(f"Abonniert: {cmd_topic_prefix}/#")

    def on_message(client, userdata, msg):
        subtopic = msg.topic.rsplit("/", 1)[-1]
        mapping = COMMAND_MAP.get(subtopic)
        if mapping is None:
            print(f"Kein Mapping für Topic '{msg.topic}'", file=sys.stderr)
            return
        can_id, encode = mapping
        try:
            payload = msg.payload.decode()
            data = encode(payload)
            bus.send(can.Message(arbitration_id=can_id, data=data, is_extended_id=False))
            print(f"CAN gesendet: id=0x{can_id:x} data={data.hex()} (von {msg.topic}={payload})")
        except Exception as exc:
            print(f"Fehler beim Senden für '{msg.topic}': {exc}", file=sys.stderr)

    client = mqtt.Client(client_id=f"{env.get('MQTT_CLIENT_ID_PREFIX', 'raspi')}-mqtt-to-can")
    username = env.get("MQTT_USERNAME")
    if username:
        client.username_pw_set(username, env.get("MQTT_PASSWORD") or None)
    client.on_connect = on_connect
    client.on_message = on_message

    client.connect(env["MQTT_HOST"], int(env.get("MQTT_PORT", 1883)))
    try:
        client.loop_forever()
    finally:
        bus.shutdown()


if __name__ == "__main__":
    main()
