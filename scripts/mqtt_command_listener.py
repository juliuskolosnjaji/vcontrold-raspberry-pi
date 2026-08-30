#!/usr/bin/env python3
"""
Abonniert MQTT-Kommando-Topics von Home Assistant und ruft passende vclient-Set-Befehle auf.

Läuft dauerhaft als systemd-Dienst (siehe systemd/mqtt-command-listener.service).

Mapping Topic-Subtopic -> vclient-Kommando steht in config/command_map.json
(kopiere command_map.json.example und trage die echten Setter-Namen deiner
Geräte-XML ein, siehe README.md).
"""
import json
import pathlib
import subprocess
import sys

import paho.mqtt.client as mqtt

from mqtt_common import load_env

VCLIENT_HOST = "localhost"
VCLIENT_PORT = "3002"
COMMAND_MAP_PATH = pathlib.Path(__file__).resolve().parent.parent / "config" / "command_map.json"


def load_command_map() -> dict:
    if not COMMAND_MAP_PATH.exists():
        raise FileNotFoundError(
            f"{COMMAND_MAP_PATH} fehlt. Kopiere config/command_map.json.example dorthin und passe es an."
        )
    return json.loads(COMMAND_MAP_PATH.read_text())


def main() -> None:
    env = load_env()
    command_map = load_command_map()
    cmd_topic_prefix = env.get("MQTT_TOPIC_CMD_HEIZUNG", "heizung/cmd")

    def on_connect(client, userdata, flags, rc):
        client.subscribe(f"{cmd_topic_prefix}/#")
        print(f"Abonniert: {cmd_topic_prefix}/#")

    def on_message(client, userdata, msg):
        subtopic = msg.topic.rsplit("/", 1)[-1]
        vclient_command = command_map.get(subtopic)
        if vclient_command is None:
            print(f"Kein Mapping für Topic '{msg.topic}'", file=sys.stderr)
            return
        payload = msg.payload.decode().strip()
        full_command = f"{vclient_command} {payload}"
        try:
            result = subprocess.run(
                ["vclient", "-h", VCLIENT_HOST, "-p", VCLIENT_PORT, "-c", full_command],
                capture_output=True,
                text=True,
                timeout=15,
                check=True,
            )
            print(f"Ausgeführt: {full_command} -> {result.stdout.strip()}")
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            print(f"Fehler bei '{full_command}': {exc}", file=sys.stderr)

    client = mqtt.Client(client_id=f"{env.get('MQTT_CLIENT_ID_PREFIX', 'raspi')}-cmd-listener")
    username = env.get("MQTT_USERNAME")
    if username:
        client.username_pw_set(username, env.get("MQTT_PASSWORD") or None)
    client.on_connect = on_connect
    client.on_message = on_message

    client.connect(env["MQTT_HOST"], int(env.get("MQTT_PORT", 1883)))
    client.loop_forever()


if __name__ == "__main__":
    main()
