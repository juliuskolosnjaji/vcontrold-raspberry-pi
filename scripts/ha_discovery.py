"""
Home-Assistant-MQTT-Discovery: erzeugt und published die Config-Nachrichten, mit
denen Home Assistant Entities automatisch anlegt, statt dass man
homeassistant/configuration_snippet.yaml von Hand einträgt.

Format: https://www.home-assistant.io/integrations/mqtt/#mqtt-discovery
Ein Publish nach "<prefix>/<component>/<unique_id>/config" (retained) reicht,
Home Assistant abonniert das automatisch, sofern MQTT-Discovery aktiv ist
(Standard-Einstellung).

Metadaten (Einheit, device_class) sind nur für die bekannten Vitogas-100/V200KW1-
Datenpunkte hinterlegt (siehe config/device-vitogas100-v200kw1/). Unbekannte
Subtopics bekommen trotzdem eine Sensor-Entity, nur ohne Einheit/Icon.
"""
import json
import pathlib

import vito_variables

# Merkt sich pro Namespace ("vcontrold"/"can"), welche MQTT-Topics beim letzten Lauf published
# wurden -- damit publish_discovery()/publish_can_discovery() bei jedem Start automatisch
# verwaiste Entities löschen können (z.B. eine aus vito.xml entfernte Variable), ohne dass man
# das manuell per mosquitto_pub aufräumen muss. Lokale Laufzeit-Datei, kein Config-Template.
_STATE_PATH = pathlib.Path(__file__).resolve().parent.parent / "config" / ".discovery_state.json"

DEVICE_INFO = {
    "identifiers": ["vcontrold_raspberry_pi"],
    "name": "Vitogas 100 (vcontrold)",
    "manufacturer": "Viessmann",
    "model": "Vitotronic V200KW1",
}

CAN_DEVICE_INFO = {
    "identifiers": ["vcontrold_uvr_can"],
    "name": "UVR16x2 (CAN)",
    "manufacturer": "Technische Alternative",
    "model": "UVR16x2",
}

# Kanonischer vito.xml-Variablenname -> {unit_of_measurement, device_class, ...} für
# hübschere Sensoren. Übernommen/erweitert aus einer früher funktionierenden
# statischen configuration.yaml (anderer Pi, siehe Chat-Historie).
SENSOR_METADATA = {
    "TempAist": {"unit_of_measurement": "°C", "device_class": "temperature", "state_class": "measurement"},
    "TempKist": {"unit_of_measurement": "°C", "device_class": "temperature", "state_class": "measurement"},
    "TempKsoll": {"unit_of_measurement": "°C", "device_class": "temperature", "state_class": "measurement"},
    "TempWWist": {"unit_of_measurement": "°C", "device_class": "temperature", "state_class": "measurement", "icon": "mdi:shower"},
    "TempVList": {"unit_of_measurement": "°C", "device_class": "temperature", "state_class": "measurement"},
    "TempRList": {"unit_of_measurement": "°C", "device_class": "temperature", "state_class": "measurement"},
    "TempATist": {"unit_of_measurement": "°C", "device_class": "temperature", "state_class": "measurement"},
    "TempAGist": {"unit_of_measurement": "°C", "device_class": "temperature"},
    "TempSTist": {"unit_of_measurement": "°C", "device_class": "temperature", "state_class": "measurement", "icon": "mdi:heating-coil"},
    "TempST2ist": {"unit_of_measurement": "°C", "device_class": "temperature", "state_class": "measurement"},
    "TempRLTist": {"unit_of_measurement": "°C", "device_class": "temperature", "state_class": "measurement"},
    "TempRLVLTist": {"unit_of_measurement": "°C", "device_class": "temperature", "state_class": "measurement"},
    "TempRaumTist": {"unit_of_measurement": "°C", "device_class": "temperature", "state_class": "measurement"},
    "TempRaumNorSoll": {"unit_of_measurement": "°C", "device_class": "temperature", "state_class": "measurement"},
    "TempRaumRedSoll": {"unit_of_measurement": "°C", "device_class": "temperature", "state_class": "measurement"},
    "TempRaumPartySoll": {"unit_of_measurement": "°C", "device_class": "temperature", "state_class": "measurement"},
    "Neigung": {"icon": "mdi:chart-bell-curve-cumulative"},
    "Niveau": {"icon": "mdi:ray-vertex"},
    "Leistung": {"unit_of_measurement": "%", "icon": "mdi:fire"},
    "Verbrauch": {"unit_of_measurement": "m³", "device_class": "gas", "state_class": "total_increasing"},
    "BrennerStarts": {"unit_of_measurement": "x", "state_class": "total_increasing", "icon": "mdi:counter", "suggested_display_precision": 0},
    "BrennerStunden1": {"unit_of_measurement": "h", "device_class": "duration", "state_class": "total_increasing"},
    "BrennerStunden2": {"unit_of_measurement": "h", "device_class": "duration", "state_class": "total_increasing"},
}

# Kanonischer Name -> {device_class, ...}: als binary_sensor statt nacktem 0/1-Sensor
# published. Rohwert von vclient ist bereits "0"/"1", passt direkt auf payload_off/on.
BINARY_SENSOR_METADATA = {
    "BrennerStatus": {"device_class": "running"},
    "PumpeStatusZirku": {"device_class": "running"},
    "PumpeStatusHk": {"device_class": "running"},
    "PumpeStatusSp": {"device_class": "running"},
    "BrennerError": {"device_class": "problem"},
    "ErrorAktiv": {"device_class": "problem"},
    "State1": {"device_class": "running"},
    "State2": {"device_class": "running"},
}


_display_names_cache: dict = {}


def reload_display_names() -> None:
    """Config/display_names.json neu einlesen -- vor jedem publish_discovery/
    publish_can_discovery aufgerufen, damit UI-Änderungen nach einem Neustart
    des Diensts wirksam werden (ohne Neustart würde der alte Stand weiterlaufen,
    genau wie bei read_cycles.json/command_map.json/can_mapping.json)."""
    global _display_names_cache
    _display_names_cache = vito_variables.load_display_names()


def _friendly_name(key: str) -> str:
    return _display_names_cache.get(key) or vito_variables.friendly_name(key)


def _unique_id(key: str, id_prefix: str = "vcontrold") -> str:
    return f"{id_prefix}_{key}"


def _load_discovery_state() -> dict:
    if _STATE_PATH.exists():
        try:
            return json.loads(_STATE_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _sync_discovery_state(client, namespace: str, published_topics: set) -> None:
    """Löscht (leere retained Nachricht) alle Topics, die beim letzten Lauf unter diesem
    Namespace published wurden, jetzt aber nicht mehr in published_topics stehen -- z.B. weil
    eine Variable aus vito.xml entfernt oder aus command_map.json/can_mapping.json genommen
    wurde. Läuft automatisch bei jedem Dienst-Start, kein manueller Aufräumschritt nötig."""
    state = _load_discovery_state()
    previous = set(state.get(namespace, []))
    stale = previous - published_topics
    for topic in stale:
        client.publish(topic, payload=None, retain=True)
    if stale:
        print(f"Discovery aufgeräumt ({namespace}): {len(stale)} verwaiste Topic(s) gelöscht")
    state[namespace] = sorted(published_topics)
    _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _STATE_PATH.write_text(json.dumps(state, indent=2))


def build_sensor_config(key: str, state_topic: str, device: dict = None, id_prefix: str = "vcontrold") -> dict:
    config = {
        "name": _friendly_name(key),
        "unique_id": _unique_id(key, id_prefix),
        "state_topic": state_topic,
        "device": device or DEVICE_INFO,
    }
    config.update(SENSOR_METADATA.get(key, {}))
    config.setdefault("suggested_display_precision", 1)
    return config


def build_binary_sensor_config(key: str, state_topic: str, device: dict = None, id_prefix: str = "vcontrold") -> dict:
    config = {
        "name": _friendly_name(key),
        "unique_id": _unique_id(key, id_prefix),
        "state_topic": state_topic,
        "payload_on": "1",
        "payload_off": "0",
        "device": device or DEVICE_INFO,
    }
    config.update(BINARY_SENSOR_METADATA.get(key, {}))
    return config


# Kanonischer Name -> {icon, unit, min, max, step, ...}: Vorgaben für schreibbare
# Variablen (command_map.json), übernommen aus der früher funktionierenden statischen
# configuration.yaml. Werden von einem gleichnamigen Feld in command_map.json's
# "discovery"-Objekt überschrieben, falls dort explizit gesetzt.
WRITABLE_METADATA = {
    "Neigung": {"icon": "mdi:chart-bell-curve"},
    "Niveau": {"icon": "mdi:tune-vertical", "unit": "K"},
    "TempRaumNorSoll": {"icon": "mdi:thermometer-lines"},
    "TempRaumRedSoll": {"icon": "mdi:moon-waning-crescent"},
    "TempRaumPartySoll": {"icon": "mdi:glass-cocktail"},
    "Betriebsart": {"icon": "mdi:valve-closed"},
    "BetriebSpar": {"icon": "mdi:leaf"},
    "BetriebParty": {"icon": "mdi:party-popper"},
}


def build_writable_config(
    key: str, state_topic: str, command_topic: str, discovery_opts: dict, device: dict = None, id_prefix: str = "vcontrold"
) -> tuple[str, dict]:
    """Gibt (component, config) zurück, component ist 'number', 'select' oder 'switch'."""
    discovery_opts = {**WRITABLE_METADATA.get(key, {}), **discovery_opts}
    component = discovery_opts.get("component", "number")
    config = {
        "name": _friendly_name(key),
        "unique_id": _unique_id(key, id_prefix),
        "state_topic": state_topic,
        "command_topic": command_topic,
        "device": device or DEVICE_INFO,
    }
    if "icon" in discovery_opts:
        config["icon"] = discovery_opts["icon"]
    if component == "number":
        for field in ("min", "max", "step"):
            if field in discovery_opts:
                config[field] = discovery_opts[field]
        if "unit" in discovery_opts:
            config["unit_of_measurement"] = discovery_opts["unit"]
    elif component == "select":
        config["options"] = discovery_opts.get("options", [])
    elif component == "switch":
        # Rohwert von vclient/vito.xml ist "0"/"1", passt direkt auf payload_off/on.
        config["payload_on"] = "1"
        config["payload_off"] = "0"
    return component, config


def publish_discovery(
    client,
    discovery_prefix: str,
    read_cycles: dict,
    command_map: dict,
    topic_heizung: str,
    topic_cmd_heizung: str,
) -> None:
    reload_display_names()
    # Alle Variablen aus den Read-Zyklen sammeln (Kandidaten für reine Sensor-Entities).
    all_subtopics = set()
    for cycle in read_cycles.values():
        all_subtopics.update(cycle.get("variables", []))

    published = set()
    published_topics = set()

    # Schreibbare Datenpunkte mit expliziter Discovery-Konfiguration zuerst (number/select),
    # damit sie nicht zusätzlich als reiner Sensor doppelt angelegt werden.
    for key, mapping in command_map.items():
        if not isinstance(mapping, dict):
            continue  # z.B. "_hinweis"-Dokumentationseintrag
        discovery_opts = mapping.get("discovery")
        if not discovery_opts:
            continue
        component, config = build_writable_config(
            key,
            state_topic=f"{topic_heizung}/{key}",
            command_topic=f"{topic_cmd_heizung}/{key}",
            discovery_opts=discovery_opts,
        )
        topic = f"{discovery_prefix}/{component}/{_unique_id(key)}/config"
        client.publish(topic, json.dumps(config), retain=True)
        published.add(key)
        published_topics.add(topic)

    # Alle übrigen Read-Zyklen-Subtopics: bekannte Boolean-Werte als binary_sensor,
    # sonst als reiner Sensor.
    for key in all_subtopics - published:
        if key in BINARY_SENSOR_METADATA:
            config = build_binary_sensor_config(key, state_topic=f"{topic_heizung}/{key}")
            topic = f"{discovery_prefix}/binary_sensor/{_unique_id(key)}/config"
        else:
            config = build_sensor_config(key, state_topic=f"{topic_heizung}/{key}")
            topic = f"{discovery_prefix}/sensor/{_unique_id(key)}/config"
        client.publish(topic, json.dumps(config), retain=True)
        published_topics.add(topic)

    _sync_discovery_state(client, "vcontrold", published_topics)


def publish_can_discovery(
    client, discovery_prefix: str, can_mapping: dict, topic_uvr: str, topic_cmd_uvr: str, can_variables: dict = None
) -> None:
    """
    Published Discovery für die CAN-Seite, unter einem eigenen Gerät "UVR16x2 (CAN)",
    getrennt vom Vitogas/vcontrold-Gerät:

      - Custom CAN-Variablen aus config/can_variables.json: komplett unabhängig von
        vito.xml, als Number/Select-Entity mit command_topic -> Home Assistant kann sie
        direkt lesen UND schreiben, der Wert geht per CAN direkt an die UVR.
      - Alle übrigen CAN-Empfangs-Kanäle (sdo_record, rx_ta_analog_outputs,
        rx_ta_digital_outputs) als reine Sensoren -- diese haben keine Entsprechung in
        vito.xml und würden sonst nie automatisch als Home-Assistant-Entity auftauchen.
    """
    reload_display_names()
    can_variables = can_variables or {}
    published = set()
    published_topics = set()

    for key, entry in can_variables.items():
        if not isinstance(entry, dict):
            continue  # z.B. "_hinweis"-Dokumentationseintrag
        discovery_opts = entry.get("discovery")
        if not discovery_opts:
            continue
        component, config = build_writable_config(
            key,
            state_topic=f"{topic_uvr}/{key}",
            command_topic=f"{topic_cmd_uvr}/{key}",
            discovery_opts=discovery_opts,
            device=CAN_DEVICE_INFO,
            id_prefix="vcontrold_uvr",
        )
        topic = f"{discovery_prefix}/{component}/{_unique_id(key, 'vcontrold_uvr')}/config"
        client.publish(topic, json.dumps(config), retain=True)
        published.add(key)
        published_topics.add(topic)

    # sdo_record.slots/rx_ta_*_outputs.outputs-Werte sind entweder ein reiner Name oder ein
    # {"topic": ..., "forward_as_set": ...}-Objekt (siehe can_node.py/publish_rx_value).
    names = set()
    for channel in can_mapping.get("sdo_record", {}).get("slots", {}).values():
        names.add(channel["topic"] if isinstance(channel, dict) else channel)
    for channel in can_mapping.get("rx_ta_analog_outputs", {}).get("outputs", {}).values():
        names.add(channel["topic"] if isinstance(channel, dict) else channel)
    for channel in can_mapping.get("rx_ta_digital_outputs", {}).get("outputs", {}).values():
        names.add(channel["topic"] if isinstance(channel, dict) else channel)

    for name in names - published:
        config = build_sensor_config(
            name, state_topic=f"{topic_uvr}/{name}", device=CAN_DEVICE_INFO, id_prefix="vcontrold_uvr"
        )
        topic = f"{discovery_prefix}/sensor/{_unique_id(name, 'vcontrold_uvr')}/config"
        client.publish(topic, json.dumps(config), retain=True)
        published_topics.add(topic)

    _sync_discovery_state(client, "can", published_topics)
