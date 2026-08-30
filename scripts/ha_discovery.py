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

import vito_variables

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
    "BetriebSpar": {"icon": "mdi:sprout"},
    "BetriebParty": {"icon": "mdi:music-note"},
    "BrennerStatus": {"device_class": "running"},
    "PumpeStatusZirku": {"device_class": "running"},
    "PumpeStatusHk": {"device_class": "running"},
    "PumpeStatusSp": {"device_class": "running"},
    "BrennerError": {"device_class": "problem"},
    "ErrorAktiv": {"device_class": "problem"},
    "State1": {"device_class": "running"},
    "State2": {"device_class": "running"},
}


def _friendly_name(key: str) -> str:
    return vito_variables.friendly_name(key)


def _unique_id(key: str, id_prefix: str = "vcontrold") -> str:
    return f"{id_prefix}_{key}"


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


def build_writable_config(
    key: str, state_topic: str, command_topic: str, discovery_opts: dict, device: dict = None, id_prefix: str = "vcontrold"
) -> tuple[str, dict]:
    """Gibt (component, config) zurück, component ist 'number', 'select' oder 'switch'."""
    component = discovery_opts.get("component", "number")
    config = {
        "name": _friendly_name(key),
        "unique_id": _unique_id(key, id_prefix),
        "state_topic": state_topic,
        "command_topic": command_topic,
        "device": device or DEVICE_INFO,
    }
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
    # Alle Variablen aus den Read-Zyklen sammeln (Kandidaten für reine Sensor-Entities).
    all_subtopics = set()
    for cycle in read_cycles.values():
        all_subtopics.update(cycle.get("variables", []))

    published = set()

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


def _rx_channel_names(blocks: list) -> set:
    names = set()
    for block in blocks:
        for channel in block.get("channels", []):
            if channel is None:
                continue
            names.add(channel["topic"] if isinstance(channel, dict) else channel)
    return names


def publish_can_discovery(
    client, discovery_prefix: str, can_mapping: dict, topic_uvr: str, topic_cmd_uvr: str, can_variables: dict = None
) -> None:
    """
    Published Discovery für die CAN-Seite, unter einem eigenen Gerät "UVR16x2 (CAN)",
    getrennt vom Vitogas/vcontrold-Gerät:

      - Custom CAN-Variablen aus config/can_variables.json: komplett unabhängig von
        vito.xml, als Number/Select-Entity mit command_topic -> Home Assistant kann sie
        direkt lesen UND schreiben, der Wert geht per CAN direkt an die UVR.
      - Alle übrigen CAN-Empfangs-Kanäle (rx_analog_blocks/rx_digital_blocks) als reine
        Sensoren -- diese haben keine Entsprechung in vito.xml und würden sonst nie
        automatisch als Home-Assistant-Entity auftauchen.
    """
    can_variables = can_variables or {}
    published = set()

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

    names = _rx_channel_names(can_mapping.get("rx_analog_blocks", []))
    names |= _rx_channel_names(can_mapping.get("rx_digital_blocks", []))

    for name in names - published:
        config = build_sensor_config(
            name, state_topic=f"{topic_uvr}/{name}", device=CAN_DEVICE_INFO, id_prefix="vcontrold_uvr"
        )
        topic = f"{discovery_prefix}/sensor/{_unique_id(name, 'vcontrold_uvr')}/config"
        client.publish(topic, json.dumps(config), retain=True)
