#!/usr/bin/env python3
"""
Orchestrator: zentraler Daemon für Vcontrold-Zyklen, Set-Befehle und Verifikation.

Ersetzt die vorherigen Einzelskripte vcontrold_to_mqtt.py (Cronjob) und
mqtt_command_listener.py durch einen dauerhaft laufenden systemd-Dienst, der:

  - mehrere Read-Zyklen mit unterschiedlichen Intervallen fährt (config/read_cycles.json)
    und die Werte per MQTT an Home Assistant UND per internem Topic an can_node.py
    weiterreicht (damit die UVR über CAN denselben aktuellen Stand sieht),
  - On-demand Set-Befehle entgegennimmt -- sowohl von Home Assistant
    (MQTT_TOPIC_CMD_HEIZUNG) als auch von der UVR (can_node.py leitet
    CAN-seitige Set-Anfragen über ein internes Topic weiter),
  - nach jedem Set-Befehl den zugehörigen Get-Befehl nachschiebt, um die
    tatsächlich übernommene Vitotronic-Antwort zu verifizieren, statt dem
    Set blind zu vertrauen.

Warum ein Daemon statt eines echten Cronjobs: Cronjobs können zwischen zwei
Läufen keine offene MQTT-Verbindung halten und daher nicht "on demand" auf
eingehende Set-Befehle reagieren. Siehe README Abschnitt 2.

CAN-Dekodierung/-Encodierung ist bewusst NICHT hier, sondern in can_node.py --
ein Fehler dort soll nicht die Vcontrold-Zyklen und HA-Befehlsverarbeitung
mit runterreißen (siehe README Abschnitt 3).
"""
import json
import pathlib
import re
import subprocess
import sys
import time

import ha_discovery
import vito_variables
from mqtt_common import make_client

VCLIENT_HOST = "localhost"
VCLIENT_PORT = "3002"

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
READ_CYCLES_PATH = PROJECT_ROOT / "config" / "read_cycles.json"
COMMAND_MAP_PATH = PROJECT_ROOT / "config" / "command_map.json"

TOPIC_TX_VALUE = "internal/can/tx"          # -> can_node.py: aktueller Wert für CAN-Übertragung
TOPIC_RX_SETREQUEST = "internal/can/rx_set"  # <- can_node.py: UVR fordert Set an

# vclient gibt bei numerischen Werten Zahl + Einheitstext zurück (z.B. "44.099998 Grad
# Celsius", "127.500000 %") -- die Einheit ist bereits separat in ha_discovery.py's
# SENSOR_METADATA hinterlegt (unit_of_measurement), daher hier nur die Zahl behalten.
# Home Assistant erwartet bei deklarierter Einheit/device_class einen reinen Zahlenwert,
# sonst bleibt der Sensor "Unbekannt". Enum-artige Antworten (z.B. "WW", "AUS") haben
# keine führende Zahl und bleiben unverändert.
_NUMERIC_PREFIX = re.compile(r"^(-?\d+(?:\.\d+)?)(?:\s|$)")


def extract_numeric_value(raw: str) -> str:
    match = _NUMERIC_PREFIX.match(raw)
    return match.group(1) if match else raw


def load_json(path: pathlib.Path, hint: str) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"{path} fehlt. {hint}")
    return json.loads(path.read_text())


def run_vclient(command: str) -> str | None:
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
    lines = [l.strip() for l in result.stdout.strip().splitlines() if l.strip()]
    return lines[-1] if lines else None


class Orchestrator:
    def __init__(self):
        self.read_cycles = load_json(
            READ_CYCLES_PATH, "Kopiere config/read_cycles.json.example dorthin und passe die Zyklen an."
        )
        self.command_map = load_json(
            COMMAND_MAP_PATH, "Kopiere config/command_map.json.example dorthin und lege settable Variablen fest."
        )
        self.variables = vito_variables.load_variables()
        self.client, env = make_client("orchestrator")
        self.topic_heizung = env.get("MQTT_TOPIC_HEIZUNG", "heizung")
        self.topic_cmd_heizung = env.get("MQTT_TOPIC_CMD_HEIZUNG", "heizung/cmd")
        self.discovery_enabled = env.get("MQTT_DISCOVERY_ENABLED", "true").lower() not in ("false", "0", "no")
        self.discovery_prefix = env.get("MQTT_DISCOVERY_PREFIX", "homeassistant")
        self.next_due = {name: 0.0 for name in self.read_cycles}

        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message

    def _on_connect(self, client, userdata, flags, rc):
        client.subscribe(f"{self.topic_cmd_heizung}/#")
        client.subscribe(f"{TOPIC_RX_SETREQUEST}/#")
        print(f"Abonniert: {self.topic_cmd_heizung}/# und {TOPIC_RX_SETREQUEST}/#")
        if self.discovery_enabled:
            ha_discovery.publish_discovery(
                client,
                self.discovery_prefix,
                self.read_cycles,
                self.command_map,
                self.topic_heizung,
                self.topic_cmd_heizung,
            )
            print(f"MQTT-Discovery published (Prefix: {self.discovery_prefix})")

    def _on_message(self, client, userdata, msg):
        key = msg.topic.rsplit("/", 1)[-1]
        payload = msg.payload.decode().strip()
        source = "CAN/UVR" if msg.topic.startswith(TOPIC_RX_SETREQUEST) else "MQTT/HA"
        self.handle_set_request(key, payload, source)

    def handle_set_request(self, key: str, payload: str, source: str) -> None:
        if key not in self.command_map:
            print(f"'{key}' ist nicht als settable freigegeben (fehlt in command_map.json), Quelle: {source}", file=sys.stderr)
            return

        variable = self.variables.get(key)
        if variable is None or not variable.get("set"):
            print(f"Keine Setter-Definition für '{key}' in vito.xml gefunden (Quelle: {source})", file=sys.stderr)
            return

        set_result = run_vclient(f"{variable['set']} {payload}")
        if set_result is None:
            print(f"Set fehlgeschlagen: {key}={payload} (Quelle: {source})", file=sys.stderr)
            return

        # Verifikation: nach dem Set den Ist-Zustand per Get nachfragen, statt
        # dem Set-Rückgabewert blind zu vertrauen.
        get_command = variable.get("get")
        verified_value = run_vclient(get_command) if get_command else set_result
        if verified_value is None:
            print(f"Set '{key}' ausgeführt, Verifikation fehlgeschlagen (Quelle: {source})", file=sys.stderr)
            return

        print(f"Set verifiziert: {key}={verified_value} (angefordert: {payload}, Quelle: {source})")
        self.publish_value(key, verified_value)

    def publish_value(self, key: str, value: str) -> None:
        """Published einen verifizierten/gelesenen Wert an Home Assistant UND an can_node.py."""
        value = extract_numeric_value(value)
        self.client.publish(f"{self.topic_heizung}/{key}", value, retain=True)
        self.client.publish(f"{TOPIC_TX_VALUE}/{key}", value)

    def run_due_cycles(self) -> None:
        now = time.monotonic()
        for name, cycle in self.read_cycles.items():
            if now < self.next_due[name]:
                continue
            self.next_due[name] = now + cycle["interval_seconds"]
            for var_name in cycle["variables"]:
                variable = self.variables.get(var_name)
                if variable is None or not variable.get("get"):
                    print(f"Keine Getter-Definition für '{var_name}' in vito.xml gefunden", file=sys.stderr)
                    continue
                value = run_vclient(variable["get"])
                if value is None:
                    continue
                self.publish_value(var_name, value)

    def run_forever(self) -> None:
        self.client.loop_start()
        try:
            while True:
                self.run_due_cycles()
                time.sleep(1)
        finally:
            self.client.loop_stop()
            self.client.disconnect()


if __name__ == "__main__":
    Orchestrator().run_forever()
