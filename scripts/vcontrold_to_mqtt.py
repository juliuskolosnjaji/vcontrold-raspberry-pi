#!/usr/bin/env python3
"""
Fragt eine Liste von vcontrold-Datenpunkten per vclient ab und published sie per MQTT.

Gedacht für den Aufruf per Cronjob (siehe README.md).

TODO: DATAPOINTS an die tatsächlichen Getter-Namen deiner Geräte-XML anpassen
(siehe /usr/local/etc/vcontrold/xml/... bzw. `vclient -c "list"`).
"""
import subprocess
import sys

from mqtt_common import make_client

VCLIENT_HOST = "localhost"
VCLIENT_PORT = "3002"

# Mapping: vclient-Kommando -> MQTT-Subtopic
DATAPOINTS = {
    "getTempAussen": "aussentemperatur",
    "getTempVorlauf": "vorlauftemperatur",
    "getTempKessel": "kesseltemperatur",
    "getBetriebsstunden": "betriebsstunden",
}


def query_vclient(command: str) -> str | None:
    try:
        result = subprocess.run(
            ["vclient", "-h", VCLIENT_HOST, "-p", VCLIENT_PORT, "-c", command],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        print(f"Fehler bei '{command}': {exc}", file=sys.stderr)
        return None
    # vclient gibt i.d.R. "Wert" oder "Wert Einheit" auf der letzten Zeile aus
    lines = [l.strip() for l in result.stdout.strip().splitlines() if l.strip()]
    return lines[-1] if lines else None


def main() -> None:
    client, env = make_client("vcontrold-to-mqtt")
    topic_prefix = env.get("MQTT_TOPIC_HEIZUNG", "heizung")

    for command, subtopic in DATAPOINTS.items():
        value = query_vclient(command)
        if value is None:
            continue
        topic = f"{topic_prefix}/{subtopic}"
        client.publish(topic, value, retain=True)
        print(f"{topic} = {value}")

    client.disconnect()


if __name__ == "__main__":
    main()
