"""Gemeinsame Hilfsfunktionen: liest config/mqtt.env, baut einen paho-mqtt Client, und räumt
verwaiste retained Topics auf (genutzt von ha_discovery.py und orchestrator.py -- gleiches
Muster, unterschiedliche Zustandsdateien)."""
import json
import os
import pathlib
import paho.mqtt.client as mqtt

CONFIG_ENV_PATH = pathlib.Path(__file__).resolve().parent.parent / "config" / "mqtt.env"


def load_env(path: pathlib.Path = CONFIG_ENV_PATH) -> dict:
    values = {}
    if not path.exists():
        raise FileNotFoundError(
            f"{path} fehlt. Kopiere config/mqtt.env.example nach config/mqtt.env und trage deine Zugangsdaten ein."
        )
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def make_client(client_id_suffix: str) -> tuple[mqtt.Client, dict]:
    env = load_env()
    client_id = f"{env.get('MQTT_CLIENT_ID_PREFIX', 'raspi')}-{client_id_suffix}"
    client = mqtt.Client(client_id=client_id)
    username = env.get("MQTT_USERNAME")
    password = env.get("MQTT_PASSWORD")
    if username:
        client.username_pw_set(username, password or None)
    client.connect(env["MQTT_HOST"], int(env.get("MQTT_PORT", 1883)))
    return client, env


def sync_retained_topics(client, state_path: pathlib.Path, namespace: str, current_topics: set) -> set:
    """Löscht (leere retained Nachricht) alle Topics, die beim letzten Lauf unter diesem
    Namespace in state_path gespeichert waren, jetzt aber nicht mehr in current_topics stehen --
    z.B. weil eine Variable aus vito.xml oder ein Kanal aus can_mapping.json entfernt wurde.
    Schreibt current_topics als neuen Stand für den nächsten Vergleich zurück. Gibt die Menge
    der gelöschten (verwaisten) Topics zurück, damit der Aufrufer das loggen kann.

    Eine state_path-Datei kann mehrere Namespaces halten (ein Schlüssel pro Namespace), damit
    z.B. ha_discovery.py "vcontrold"- und "can"-Discovery getrennt in derselben Datei tracken
    kann, ohne dass ein Aufräumlauf des einen Namespace den anderen betrifft."""
    state = {}
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text())
        except (json.JSONDecodeError, OSError):
            state = {}
    previous = set(state.get(namespace, []))
    stale = previous - current_topics
    for topic in stale:
        client.publish(topic, payload=None, retain=True)
    state[namespace] = sorted(current_topics)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2))
    return stale
