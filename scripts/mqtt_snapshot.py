"""
Kurzlebiger MQTT-Snapshot für die "Live-Wert"-Anzeige in der Web-UI: verbindet kurz, sammelt die
aktuellen (retained) Werte für angefragte Topics, trennt wieder -- keine Dauerverbindung, die UI
selbst ist ein zustandsloser Flask-Request/Response-Prozess ohne eigene MQTT-Verbindung.
Retained Nachrichten werden vom Broker sofort bei subscribe() zugestellt, ein kurzes Zeitfenster
reicht deshalb aus.
"""
import time

import paho.mqtt.client as mqtt

from mqtt_common import load_env

CONNECT_TIMEOUT = 1.5
MESSAGE_TIMEOUT = 1.2


def fetch(topics: list, connect_timeout: float = CONNECT_TIMEOUT, message_timeout: float = MESSAGE_TIMEOUT) -> dict:
    """Gibt {topic: payload} zurück für alle Topics aus `topics`, die innerhalb von
    `message_timeout` Sekunden nach erfolgreicher Verbindung eine (retained) Nachricht liefern.
    Bricht früher ab, sobald alle angefragten Topics beantwortet sind. Liefert ein leeres Dict,
    falls kein Broker erreichbar ist (config/mqtt.env fehlt, connect() schlägt fehl oder die
    Verbindung steht nicht innerhalb von `connect_timeout` -- z.B. falsche Zugangsdaten) -- die UI
    zeigt dann einfach keine Live-Werte an, statt einen Fehler zu werfen.

    Abonniert erst NACH erfolgreichem CONNACK (nicht direkt nach dem nicht-blockierenden
    connect()) -- sonst werden subscribe()-Aufrufe vom Client intern gepuffert und erst nach dem
    eigentlichen Verbindungsaufbau rausgeschickt, was unnötig Zeit kostet, bevor überhaupt auf
    Nachrichten gewartet wird."""
    unique_topics = sorted(set(t for t in topics if t))
    if not unique_topics:
        return {}
    try:
        env = load_env()
    except FileNotFoundError:
        return {}

    result: dict = {}
    connected = []

    def on_connect(client, userdata, flags, rc):
        connected.append(rc)

    def on_message(client, userdata, msg):
        result[msg.topic] = msg.payload.decode(errors="replace")

    client = mqtt.Client()
    username = env.get("MQTT_USERNAME")
    if username:
        client.username_pw_set(username, env.get("MQTT_PASSWORD") or None)
    client.on_connect = on_connect
    client.on_message = on_message

    try:
        client.connect(env["MQTT_HOST"], int(env.get("MQTT_PORT", 1883)), keepalive=5)
    except (OSError, KeyError, ValueError):
        return {}

    client.loop_start()
    try:
        connect_deadline = time.monotonic() + connect_timeout
        while time.monotonic() < connect_deadline and not connected:
            time.sleep(0.02)
        if not connected or connected[0] != 0:
            return {}

        for topic in unique_topics:
            client.subscribe(topic)

        message_deadline = time.monotonic() + message_timeout
        while time.monotonic() < message_deadline and len(result) < len(unique_topics):
            time.sleep(0.02)
    finally:
        client.loop_stop()
        client.disconnect()

    return result
