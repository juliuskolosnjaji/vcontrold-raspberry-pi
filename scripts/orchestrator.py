#!/usr/bin/env python3
"""
Orchestrator: zentraler Daemon für Vcontrold-Zyklen, Set-Befehle und Verifikation.

Ersetzt die vorherigen Einzelskripte vcontrold_to_mqtt.py (Cronjob) und
mqtt_command_listener.py durch einen dauerhaft laufenden systemd-Dienst, der:

  - mehrere Read-Zyklen mit unterschiedlichen Intervallen fährt (config/read_cycles.json)
    und die Werte per MQTT auf MQTT_TOPIC_HEIZUNG published (retained) -- Home Assistant UND
    can_node.py (für die Weiterleitung an die UVR über CAN) abonnieren denselben Topic, kein
    separater interner Kanal mehr (siehe README "MQTT-Architektur"),
  - On-demand Set-Befehle auf MQTT_TOPIC_CMD_HEIZUNG entgegennimmt -- sowohl von Home
    Assistant (Payload = roher Wert) als auch von der UVR (can_node.py published dort
    ebenfalls, aber als JSON {"value": ..., "source": "can"}, damit die Quelle im Log noch
    erkennbar bleibt -- MQTT selbst verrät den Absender nicht),
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
import tempfile
import time

import ha_discovery
import mqtt_variables as mqtt_vars
import vito_variables
from mqtt_common import make_client, sync_retained_topics

VCLIENT_HOST = "localhost"
VCLIENT_PORT = "3002"

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
READ_CYCLES_PATH = PROJECT_ROOT / "config" / "read_cycles.json"
# Merkt sich, welche vito.xml-Variablen beim letzten Start bekannt waren -- damit eine seither
# entfernte Variable nicht für immer ihren letzten (jetzt veralteten) Wert als retained MQTT-
# Nachricht behält. Ohne das würde can_node.py diesen eingefrorenen Wert unbegrenzt weiter als
# TA-Netzwerkausgang an die UVR senden (tx_values wird nie explizit geleert, siehe README
# "Verwaiste Entities werden automatisch entfernt"). Lokale Laufzeit-Datei, kein Config-Template.
VARIABLE_STATE_PATH = PROJECT_ROOT / "config" / ".orchestrator_state.json"

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


# Trennzeichen für Batch-Antworten: ein Steuerzeichen, das in keiner realen
# Vitotronic-Antwort ("44.099998 Grad Celsius", "WW", ...) vorkommen kann.
_BATCH_DELIMITER = "\x1f"


def make_batch_template(cycle_name: str, count: int) -> str:
    """vclient -t braucht eine Template-DATEI (kein Inline-String, siehe README/Chat-
    Historie); $R1..$Rn sind die Rohtext-Rückgabewerte in Reihenfolge der -c-Kommandoliste.
    Deterministischer Pfad pro Zyklus statt tempfile.NamedTemporaryFile, damit bei jedem
    Neustart dieselbe Datei überschrieben wird statt sich in /tmp anzusammeln."""
    safe_name = re.sub(r"[^A-Za-z0-9_-]", "_", cycle_name)
    path = pathlib.Path(tempfile.gettempdir()) / f"vcontrold-orchestrator-{safe_name}.vclient.tmpl"
    path.write_text(_BATCH_DELIMITER.join(f"$R{i + 1}" for i in range(count)))
    return str(path)


def run_vclient_batch(commands: list[str], template_path: str) -> list[str | None]:
    """Fragt mehrere Get-Kommandos in EINER vclient-Verbindung ab (statt einer TCP-
    Verbindung pro Variable), via vclient -t (Template-Modus, siehe make_batch_template).
    Gibt bei Erfolg genau len(commands) Werte zurück, bei Fehler eine gleich lange Liste
    aus None (damit der Aufrufer jede Variable einzeln als fehlgeschlagen loggen kann)."""
    # Batching spart nur den TCP-Verbindungsaufbau, nicht die eigentliche KW-Protokoll-Zeit
    # pro Kommando (Optolink ist seriell, jedes Get braucht spürbar Zeit) -- Timeout muss
    # daher mit der Anzahl Kommandos wachsen, sonst schlagen größere Zyklen grundlos fehl.
    timeout = max(15, 5 * len(commands))
    try:
        result = subprocess.run(
            ["vclient", "-h", VCLIENT_HOST, "-p", VCLIENT_PORT, "-c", ",".join(commands), "-t", template_path],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=True,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        print(f"Fehler bei Batch-Anfrage ({len(commands)} Kommandos): {exc}", file=sys.stderr)
        return [None] * len(commands)

    values = result.stdout.strip("\n").split(_BATCH_DELIMITER)
    if len(values) != len(commands):
        print(
            f"Batch-Antwort unerwartet ({len(values)} statt {len(commands)} Werte): {result.stdout!r}",
            file=sys.stderr,
        )
        return [None] * len(commands)
    return values


class Orchestrator:
    def __init__(self):
        self.read_cycles = load_json(
            READ_CYCLES_PATH, "Kopiere config/read_cycles.json.example dorthin und passe die Zyklen an."
        )
        self.mqtt_variables = mqtt_vars.load()
        self.variables = vito_variables.load_variables()
        self._log_loaded_cycles()
        self.cycle_batches = self._build_cycle_batches()
        self.client, env = make_client("orchestrator")
        self.topic_heizung = env.get("MQTT_TOPIC_HEIZUNG", "heizung")
        self.topic_cmd_heizung = env.get("MQTT_TOPIC_CMD_HEIZUNG", "heizung/cmd")
        self.discovery_enabled = env.get("MQTT_DISCOVERY_ENABLED", "true").lower() not in ("false", "0", "no")
        self.discovery_prefix = env.get("MQTT_DISCOVERY_PREFIX", "homeassistant")
        self.next_due = {name: 0.0 for name in self.read_cycles}

        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message

    def _log_loaded_cycles(self) -> None:
        """Zeigt beim Start (siehe Diagnose-Seite/journalctl), welche Variablen aus
        config/read_cycles.json geladen wurden, mit welchem Zyklus/Intervall, und ob
        dafür überhaupt ein Getter in vito.xml existiert."""
        print(f"{len(self.read_cycles)} Zyklus/Zyklen aus {READ_CYCLES_PATH} geladen:")
        for name, cycle in self.read_cycles.items():
            interval = cycle.get("interval_seconds", "?")
            var_names = cycle.get("variables", [])
            for var_name in var_names:
                variable = self.variables.get(var_name)
                status = "OK" if variable and variable.get("get") else "FEHLER: kein Getter in vito.xml"
                print(f"  Zyklus '{name}' (alle {interval}s): {var_name} -> {status}")
            if not var_names:
                print(f"  Zyklus '{name}' (alle {interval}s): keine Variablen konfiguriert")

    def _build_cycle_batches(self) -> dict:
        """Bereitet pro Zyklus einmalig (var_names, get_commands, template_path) vor, damit
        run_due_cycles() alle Getter eines Zyklus in EINER vclient-Verbindung statt einer
        pro Variable abfragen kann. Variablen ohne Getter wurden bereits in
        _log_loaded_cycles() als Fehler geloggt und werden hier stillschweigend übersprungen."""
        batches = {}
        for name, cycle in self.read_cycles.items():
            var_names = []
            get_commands = []
            for var_name in cycle.get("variables", []):
                variable = self.variables.get(var_name)
                if variable and variable.get("get"):
                    var_names.append(var_name)
                    get_commands.append(variable["get"])
            template_path = make_batch_template(name, len(get_commands)) if get_commands else None
            batches[name] = (var_names, get_commands, template_path)
        return batches

    def _sync_variable_state(self) -> None:
        """Löscht (leere retained Nachricht) heizung/<var> für jede Variable, die beim letzten
        Start noch bekannt war, jetzt aber nicht mehr in vito.xml existiert. Entfernt außerdem
        automatisch verwaiste Einträge aus config/mqtt_variables.json (siehe
        mqtt_vars.prune_removed_vito_variables()) -- sonst würde ein aus vito.xml gelöschter
        Name dort fälschlich als CAN-Custom-Variable weiterleben."""
        current_topics = {f"{self.topic_heizung}/{name}" for name in self.variables}
        stale = sync_retained_topics(self.client, VARIABLE_STATE_PATH, "variables", current_topics)
        if stale:
            stale_names = sorted(topic.rsplit("/", 1)[-1] for topic in stale)
            print(f"Retained Werte aufgeräumt: {len(stale)} verwaiste Variable(n) ({', '.join(stale_names)})")

        removed = mqtt_vars.prune_removed_vito_variables(set(self.variables.keys()))
        if removed:
            self.mqtt_variables = mqtt_vars.load()
            print(f"MQTT-Variablen aufgeräumt: {len(removed)} verwaiste Einträge entfernt ({', '.join(sorted(removed))})")

    def _on_connect(self, client, userdata, flags, rc):
        client.subscribe(f"{self.topic_cmd_heizung}/#")
        print(f"Abonniert: {self.topic_cmd_heizung}/#")
        self._sync_variable_state()
        if self.discovery_enabled:
            ha_discovery.publish_discovery(
                client,
                self.discovery_prefix,
                self.read_cycles,
                self.mqtt_variables,
                self.topic_heizung,
                self.topic_cmd_heizung,
                known_variables=set(self.variables.keys()),
            )
            print(f"MQTT-Discovery published (Prefix: {self.discovery_prefix})")

    def _on_message(self, client, userdata, msg):
        key = msg.topic.rsplit("/", 1)[-1]
        payload, source = self._parse_cmd_payload(msg.payload.decode().strip())
        self.handle_set_request(key, payload, source)

    @staticmethod
    def _parse_cmd_payload(raw: str) -> tuple[str, str]:
        """Set-Anfragen auf topic_cmd_heizung kommen entweder direkt als roher Wert (von Home
        Assistant) oder als JSON {"value": ..., "source": "can"} (von can_node.py, das UVR-
        seitige Set-Anfragen über denselben Topic weiterleitet, siehe can_node.py/publish_rx_value
        -- MQTT selbst verrät den Absender nicht, daher die Quellenkennung im Payload statt im
        Topic). Gibt (wert_als_string, quelle_fuer_log) zurück."""
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return raw, "MQTT/HA"
        if isinstance(data, dict) and "value" in data:
            source = "CAN/UVR" if data.get("source") == "can" else "MQTT/HA"
            return str(data["value"]), source
        return raw, "MQTT/HA"

    def handle_set_request(self, key: str, payload: str, source: str) -> None:
        entry = self.mqtt_variables.get(key)
        if entry is None or not mqtt_vars.is_writable(entry):
            print(f"'{key}' ist nicht als settable freigegeben (fehlt in config/mqtt_variables.json), Quelle: {source}", file=sys.stderr)
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
        """Published einen verifizierten/gelesenen Wert (retained) -- Home Assistant UND
        can_node.py (für die CAN-Weiterleitung) abonnieren denselben Topic, kein separater
        interner Kanal mehr."""
        value = extract_numeric_value(value)
        self.client.publish(f"{self.topic_heizung}/{key}", value, retain=True)

    def run_due_cycles(self) -> None:
        now = time.monotonic()
        for name, cycle in self.read_cycles.items():
            if now < self.next_due[name]:
                continue
            self.next_due[name] = now + cycle["interval_seconds"]

            var_names, get_commands, template_path = self.cycle_batches[name]
            if not get_commands:
                continue
            values = run_vclient_batch(get_commands, template_path)
            for var_name, value in zip(var_names, values):
                if value is None:
                    print(f"Zyklus '{name}': '{var_name}' fehlgeschlagen (kein Wert von vclient)", file=sys.stderr)
                    continue
                print(f"Zyklus '{name}': '{var_name}' = {value}")
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
