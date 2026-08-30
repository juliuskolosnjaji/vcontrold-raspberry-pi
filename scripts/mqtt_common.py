"""Gemeinsame Hilfsfunktionen: liest config/mqtt.env und baut einen paho-mqtt Client."""
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
