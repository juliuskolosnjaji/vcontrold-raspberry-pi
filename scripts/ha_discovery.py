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

# Kanonischer vito.xml-Variablenname -> {unit_of_measurement, device_class} für hübschere Sensoren.
SENSOR_METADATA = {
    "TempAist": {"unit_of_measurement": "°C", "device_class": "temperature"},
    "TempKist": {"unit_of_measurement": "°C", "device_class": "temperature"},
    "TempKsoll": {"unit_of_measurement": "°C", "device_class": "temperature"},
    "TempWWist": {"unit_of_measurement": "°C", "device_class": "temperature"},
    "TempVList": {"unit_of_measurement": "°C", "device_class": "temperature"},
    "TempRList": {"unit_of_measurement": "°C", "device_class": "temperature"},
    "BrennerStunden1": {"unit_of_measurement": "h"},
    "BrennerStunden2": {"unit_of_measurement": "h"},
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
    return config


def build_writable_config(key: str, state_topic: str, command_topic: str, discovery_opts: dict) -> tuple[str, dict]:
    """Gibt (component, config) zurück, component ist 'number' oder 'select'."""
    component = discovery_opts.get("component", "number")
    config = {
        "name": _friendly_name(key),
        "unique_id": _unique_id(key),
        "state_topic": state_topic,
        "command_topic": command_topic,
        "device": DEVICE_INFO,
    }
    if component == "number":
        for field in ("min", "max", "step"):
            if field in discovery_opts:
                config[field] = discovery_opts[field]
        if "unit" in discovery_opts:
            config["unit_of_measurement"] = discovery_opts["unit"]
    elif component == "select":
        config["options"] = discovery_opts.get("options", [])
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

    # Alle übrigen Read-Zyklen-Subtopics als reine Sensoren.
    for key in all_subtopics - published:
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


def publish_can_discovery(client, discovery_prefix: str, can_mapping: dict, topic_uvr: str) -> None:
    """
    Published Sensor-Discovery für alle CAN-Empfangs-Kanäle (UVR -> Pi), die in
    config/can_mapping.json unter rx_analog_blocks/rx_digital_blocks konfiguriert sind.
    Diese Kanäle haben keine Entsprechung in vito.xml -- ohne das hier würden sie zwar
    per MQTT ankommen (uvr/<kanal>), aber nie automatisch als Home-Assistant-Entity auftauchen.
    """
    names = _rx_channel_names(can_mapping.get("rx_analog_blocks", []))
    names |= _rx_channel_names(can_mapping.get("rx_digital_blocks", []))

    for name in names:
        config = build_sensor_config(
            name, state_topic=f"{topic_uvr}/{name}", device=CAN_DEVICE_INFO, id_prefix="vcontrold_uvr"
        )
        topic = f"{discovery_prefix}/sensor/{_unique_id(name, 'vcontrold_uvr')}/config"
        client.publish(topic, json.dumps(config), retain=True)
