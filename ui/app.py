#!/usr/bin/env python3
"""
Web-UI für vcontrold: Kommando-Konsole, Config-Importer, Diagnose, CAN-Sniffer.

Start (Entwicklung): python3 app.py
Produktiv: siehe systemd/vcontrold-ui.service im Projekt-Root.

Sicherheitshinweis: Diese UI kann Schreibbefehle an die Heizung senden. Nicht ohne
Basic-Auth (siehe ui.env) und nicht ungeschützt im Internet exponieren.
"""
import datetime
import json
import pathlib
import shutil
import sys
import xml.etree.ElementTree as ET

from flask import Flask, jsonify, redirect, render_template, request, url_for
from werkzeug.utils import secure_filename

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))

import can_sniffer
import diagnostics
import ta_can_protocol as proto
import vclient_wrapper
import vito_variables
import xml_parser

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
UI_ENV_PATH = pathlib.Path(__file__).resolve().parent / "ui.env"
MQTT_ENV_PATH = PROJECT_ROOT / "config" / "mqtt.env"
CAN_MAPPING_PATH = PROJECT_ROOT / "config" / "can_mapping.json"
CAN_VARIABLES_PATH = PROJECT_ROOT / "config" / "can_variables.json"

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024  # 2 MB reicht für Geräte-XML
app.config["TEMPLATES_AUTO_RELOAD"] = True  # Templates immer frisch von der Platte lesen


def load_env(path: pathlib.Path) -> dict:
    values = {}
    if not path.exists():
        return values
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def get_ui_config() -> dict:
    env = load_env(UI_ENV_PATH)
    return {
        "username": env.get("UI_USERNAME", "admin"),
        "password": env.get("UI_PASSWORD", "change-me"),
        "vcontrold_main_xml": env.get("VCONTROLD_MAIN_XML_PATH", "/etc/vcontrold/vcontrold.xml"),
        "device_xml": env.get("DEVICE_XML_PATH", ""),
        "vclient_host": env.get("VCLIENT_HOST", "localhost"),
        "vclient_port": env.get("VCLIENT_PORT", "3002"),
        "can_interface": env.get("CAN_INTERFACE", "can1"),
        "services": [
            s.strip()
            for s in env.get(
                "MONITORED_SERVICES",
                "vcontrold,orchestrator,can-node,can1-up",
            ).split(",")
            if s.strip()
        ],
    }


@app.before_request
def require_auth():
    cfg = get_ui_config()
    auth = request.authorization
    if not auth or auth.username != cfg["username"] or auth.password != cfg["password"]:
        return (
            "Anmeldung erforderlich",
            401,
            {"WWW-Authenticate": 'Basic realm="vcontrold-ui"'},
        )


@app.route("/")
def dashboard():
    cfg = get_ui_config()
    mqtt_env = load_env(MQTT_ENV_PATH)
    return render_template("dashboard.html", cfg=cfg, mqtt_configured=bool(mqtt_env))


@app.route("/console", methods=["GET", "POST"])
def console():
    cfg = get_ui_config()
    commands = xml_parser.try_extract_commands(cfg["device_xml"])
    result = None
    executed_command = ""

    if request.method == "POST":
        executed_command = request.form.get("command", "").strip()
        confirmed = request.form.get("confirmed") == "1"
        is_write = executed_command.lower().startswith("set")
        if is_write and not confirmed:
            result = {"ok": False, "output": "Set-Befehl erfordert Bestätigung (Checkbox aktivieren)."}
        elif executed_command:
            result = vclient_wrapper.run_vclient(
                cfg["vclient_host"], cfg["vclient_port"], executed_command
            )

    return render_template(
        "console.html",
        cfg=cfg,
        commands=commands,
        result=result,
        executed_command=executed_command,
    )


def finish_vcontrold_restart(action_desc: str) -> tuple[str, bool]:
    restart = diagnostics.restart_service("vcontrold")
    status = diagnostics.service_status("vcontrold")
    if restart["ok"] and status["state"] == "active":
        return f"{action_desc}, vcontrold läuft.", True
    return (
        f"{action_desc}, aber vcontrold-Neustart fehlgeschlagen: {restart['detail'] or status['state']}",
        False,
    )


@app.route("/config", methods=["GET", "POST"])
def config_page():
    cfg = get_ui_config()
    main_path = pathlib.Path(cfg["vcontrold_main_xml"])
    device_path = pathlib.Path(cfg["device_xml"]) if cfg["device_xml"] else None

    message = None
    message_ok = None

    if request.method == "POST":
        target = request.form.get("target")
        path = main_path if target == "main" else device_path

        if path is None:
            message, message_ok = "DEVICE_XML_PATH ist nicht konfiguriert.", False
        else:
            uploaded = request.files.get("xml_file")
            if uploaded and uploaded.filename:
                filename = secure_filename(uploaded.filename)
                tmp_path = pathlib.Path("/tmp") / filename
                uploaded.save(tmp_path)
                try:
                    content = tmp_path.read_text()
                    ET.fromstring(content)
                except ET.ParseError as exc:
                    message, message_ok = f"Ungültiges XML: {exc}", False
                else:
                    backup_and_write(path, content)
                    message, message_ok = finish_vcontrold_restart(f"{path.name} importiert")
                finally:
                    tmp_path.unlink(missing_ok=True)
            else:
                content = request.form.get("content", "")
                try:
                    ET.fromstring(content)
                except ET.ParseError as exc:
                    message, message_ok = f"Ungültiges XML: {exc}", False
                else:
                    backup_and_write(path, content)
                    message, message_ok = finish_vcontrold_restart(f"{path.name} gespeichert")

    main_content = main_path.read_text() if main_path.exists() else ""
    device_content = device_path.read_text() if device_path is not None and device_path.exists() else ""

    return render_template(
        "config.html",
        cfg=cfg,
        main_path=str(main_path),
        device_path=str(device_path) if device_path else None,
        main_content=main_content,
        device_content=device_content,
        message=message,
        message_ok=message_ok,
    )


MQTT_FIELDS = [
    ("MQTT_HOST", "Broker-Host", True),
    ("MQTT_PORT", "Broker-Port", True),
    ("MQTT_USERNAME", "Benutzername", False),
    ("MQTT_PASSWORD", "Passwort", False),
    ("MQTT_CLIENT_ID_PREFIX", "Client-ID-Präfix", True),
    ("MQTT_TOPIC_HEIZUNG", "Topic-Präfix Heizung", True),
    ("MQTT_TOPIC_UVR", "Topic-Präfix UVR", True),
    ("MQTT_TOPIC_CMD_HEIZUNG", "Topic-Präfix Heizungs-Commands", True),
    ("MQTT_DISCOVERY_ENABLED", "HA-Discovery aktiv (true/false)", True),
    ("MQTT_DISCOVERY_PREFIX", "HA-Discovery-Präfix", True),
]

# Dienste, die mqtt.env nutzen und nach einer Änderung neu gestartet werden sollten
MQTT_DEPENDENT_SERVICES = [
    "orchestrator",
    "can-node",
]


def write_env(path: pathlib.Path, values: dict) -> None:
    lines = [f"{key}={value}" for key, value in values.items()]
    path.write_text("\n".join(lines) + "\n")


def backup_and_write(target: pathlib.Path, content: str) -> None:
    if target.exists():
        backup = target.with_suffix(
            target.suffix + f".bak.{datetime.datetime.now():%Y%m%d%H%M%S}"
        )
        shutil.copy2(target, backup)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)


@app.route("/settings", methods=["GET", "POST"])
def settings():
    message = None
    message_ok = None
    test_result = None
    current = load_env(MQTT_ENV_PATH)

    if request.method == "POST":
        action = request.form.get("action", "save")

        if action == "save":
            new_values = dict(current)
            for key, _label, required in MQTT_FIELDS:
                value = request.form.get(key, "").strip()
                if required and not value:
                    message, message_ok = f"Feld '{key}' darf nicht leer sein.", False
                    break
                new_values[key] = value
            else:
                MQTT_ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
                write_env(MQTT_ENV_PATH, new_values)
                current = new_values

                restarted, failed = [], []
                for service in MQTT_DEPENDENT_SERVICES:
                    status = diagnostics.service_status(service)
                    if status["state"] not in ("active",):
                        continue  # nur laufende Dienste neu starten, nicht versehentlich welche aktivieren
                    result = diagnostics.restart_service(service)
                    (restarted if result["ok"] else failed).append(service)

                message = "MQTT-Konfiguration gespeichert."
                if restarted:
                    message += f" Neu gestartet: {', '.join(restarted)}."
                if failed:
                    message += f" Fehler beim Neustart von: {', '.join(failed)}."
                message_ok = not failed

        elif action == "test":
            new_values = dict(current)
            for key, _label, _required in MQTT_FIELDS:
                new_values[key] = request.form.get(key, "").strip()
            if new_values.get("MQTT_HOST"):
                test_result = diagnostics.mqtt_connectivity(
                    new_values["MQTT_HOST"],
                    int(new_values.get("MQTT_PORT") or 1883),
                    new_values.get("MQTT_USERNAME") or None,
                    new_values.get("MQTT_PASSWORD") or None,
                )
            else:
                test_result = {"ok": False, "detail": "Kein Broker-Host angegeben."}
            current = new_values

    return render_template(
        "settings.html",
        fields=MQTT_FIELDS,
        current=current,
        message=message,
        message_ok=message_ok,
        test_result=test_result,
    )


TOTAL_SLOTS = 16              # feste Slot-Anzahl je Spalte (analog wie digital)
ANALOG_SLOTS_PER_BLOCK = 4    # CAN-Frame-Limit bei 2 Byte/Wert: 8 Byte / 2 = 4
ANALOG_BLOCK_COUNT = TOTAL_SLOTS // ANALOG_SLOTS_PER_BLOCK  # 4 CAN-IDs für 16 Analog-Slots

# key, label, is_analog, allow_forward
CAN_BLOCK_CATEGORIES = [
    ("tx_analog_blocks", "Senden: Analog", True, False),
    ("tx_digital_blocks", "Senden: Digital", False, False),
    ("rx_analog_blocks", "Empfangen: Analog", True, True),
    ("rx_digital_blocks", "Empfangen: Digital", False, True),
]


def load_can_mapping() -> dict:
    if not CAN_MAPPING_PATH.exists():
        return {"bitrate": proto.DEFAULT_BITRATE, "own_node_number": 1}
    return json.loads(CAN_MAPPING_PATH.read_text())


CAN_VARIABLE_ROWS = 8  # Anzahl editierbarer Zeilen für custom CAN-Variablen
TA_NETWORK_OUTPUT_SLOTS = 16  # TA-Netzwerkausgänge (bestätigtes Schema, siehe ta_canopen.py)


def load_can_variables() -> dict:
    if not CAN_VARIABLES_PATH.exists():
        return {}
    return {k: v for k, v in json.loads(CAN_VARIABLES_PATH.read_text()).items() if isinstance(v, dict)}


def channel_to_slot(c, allow_forward: bool) -> dict:
    if allow_forward:
        if isinstance(c, dict):
            return {"topic": c.get("topic", ""), "forward": c.get("forward_as_set", "")}
        return {"topic": c or "", "forward": ""}
    return {"value": c or ""}


def build_slots_for_template(blocks: list, is_analog: bool, allow_forward: bool) -> list:
    """Baut eine flache Liste von TOTAL_SLOTS Slots. Bei analog beginnt alle 4 Slots ein neuer
    CAN-ID-Block (can_id_start=True), bei digital gibt es nur einen Block am Anfang."""
    slots_per_block = ANALOG_SLOTS_PER_BLOCK if is_analog else TOTAL_SLOTS
    slots = []
    for slot_index in range(TOTAL_SLOTS):
        block_index = slot_index // slots_per_block
        pos_in_block = slot_index % slots_per_block
        block = blocks[block_index] if block_index < len(blocks) else {}
        channels = block.get("channels", [])
        c = channels[pos_in_block] if pos_in_block < len(channels) else None
        slot = channel_to_slot(c, allow_forward)
        slot["can_id_start"] = pos_in_block == 0
        slot["can_id"] = block.get("can_id", "") if pos_in_block == 0 else None
        slot["block_index"] = block_index
        slots.append(slot)
    return slots


@app.route("/can-settings", methods=["GET", "POST"])
def can_settings():
    message = None
    message_ok = None
    mapping = load_can_mapping()
    can_variables = load_can_variables()

    if request.method == "POST":
        new_mapping = {
            "bitrate": int(request.form.get("bitrate", proto.DEFAULT_BITRATE) or proto.DEFAULT_BITRATE),
            "own_node_number": int(request.form.get("own_node_number", 1) or 1),
        }
        errors = []

        ta_net_analog = [
            request.form.get(f"ta_net_analog_{i}", "").strip() or None for i in range(TA_NETWORK_OUTPUT_SLOTS)
        ]
        ta_net_digital = [
            request.form.get(f"ta_net_digital_{i}", "").strip() or None for i in range(TA_NETWORK_OUTPUT_SLOTS)
        ]
        if any(ta_net_analog) or any(ta_net_digital):
            new_mapping["ta_network_outputs"] = {"analog": ta_net_analog, "digital": ta_net_digital}

        new_can_variables = {}
        for i in range(CAN_VARIABLE_ROWS):
            name = request.form.get(f"canvar_name_{i}", "").strip()
            if not name:
                continue
            component = request.form.get(f"canvar_component_{i}", "number")
            discovery = {"component": component}
            if component == "number":
                unit = request.form.get(f"canvar_unit_{i}", "").strip()
                if unit:
                    discovery["unit"] = unit
                for field in ("min", "max", "step"):
                    raw = request.form.get(f"canvar_{field}_{i}", "").strip()
                    if raw:
                        try:
                            discovery[field] = float(raw) if "." in raw else int(raw)
                        except ValueError:
                            errors.append(f"Custom CAN-Variable '{name}': ungültiger Wert für {field}")
            elif component == "select":
                options_raw = request.form.get(f"canvar_options_{i}", "").strip()
                discovery["options"] = [o.strip() for o in options_raw.split(",") if o.strip()]
            new_can_variables[name] = {"discovery": discovery}

        for key, label, is_analog, allow_forward in CAN_BLOCK_CATEGORIES:
            slots_per_block = ANALOG_SLOTS_PER_BLOCK if is_analog else TOTAL_SLOTS
            block_count = ANALOG_BLOCK_COUNT if is_analog else 1

            blocks = []
            for block_index in range(block_count):
                can_id_raw = request.form.get(f"{key}_can_id_{block_index}", "").strip()
                channels = []
                for pos in range(slots_per_block):
                    slot_index = block_index * slots_per_block + pos
                    if allow_forward:
                        topic = request.form.get(f"{key}_slot_topic_{slot_index}", "").strip()
                        forward = request.form.get(f"{key}_slot_forward_{slot_index}", "").strip()
                        if not topic:
                            channels.append(None)
                        elif forward:
                            channels.append({"topic": topic, "forward_as_set": forward})
                        else:
                            channels.append(topic)
                    else:
                        value = request.form.get(f"{key}_slot_value_{slot_index}", "").strip()
                        channels.append(value or None)

                if not can_id_raw:
                    if any(c is not None for c in channels):
                        errors.append(
                            f"{label}, Slots {block_index * slots_per_block + 1}-"
                            f"{(block_index + 1) * slots_per_block}: Kanäle belegt, aber keine CAN-ID gesetzt"
                        )
                    continue
                try:
                    can_id_int = int(can_id_raw, 0)
                except ValueError:
                    errors.append(f"{label}, Block {block_index + 1}: ungültige CAN-ID '{can_id_raw}'")
                    continue

                block = {"can_id": hex(can_id_int), "channels": channels}
                if is_analog:
                    block["value_bytes"] = 2
                blocks.append(block)
            new_mapping[key] = blocks

        if errors:
            message, message_ok = " / ".join(errors), False
            mapping = new_mapping  # editierte (fehlerhafte) Werte im Formular zeigen
            can_variables = new_can_variables
        else:
            CAN_MAPPING_PATH.parent.mkdir(parents=True, exist_ok=True)
            CAN_MAPPING_PATH.write_text(json.dumps(new_mapping, indent=2, ensure_ascii=False) + "\n")
            CAN_VARIABLES_PATH.write_text(json.dumps(new_can_variables, indent=2, ensure_ascii=False) + "\n")
            mapping = new_mapping
            can_variables = new_can_variables

            status = diagnostics.service_status("can-node")
            if status["state"] == "active":
                restart = diagnostics.restart_service("can-node")
                if restart["ok"]:
                    message, message_ok = "Gespeichert, can-node neu gestartet.", True
                else:
                    message, message_ok = f"Gespeichert, aber Neustart fehlgeschlagen: {restart['detail']}", False
            else:
                message, message_ok = "Gespeichert. can-node läuft nicht, wurde nicht neu gestartet.", True

    columns = []
    for key, label, is_analog, allow_forward in CAN_BLOCK_CATEGORIES:
        columns.append(
            {
                "key": key,
                "label": label,
                "is_analog": is_analog,
                "allow_forward": allow_forward,
                "slots": build_slots_for_template(mapping.get(key, []), is_analog, allow_forward),
            }
        )

    available_subtopics = set()
    for cycle in load_read_cycles().values():
        available_subtopics.update(cycle.get("variables", []))

    command_map_path = PROJECT_ROOT / "config" / "command_map.json"
    available_set_keys = (
        sorted(k for k, v in json.loads(command_map_path.read_text()).items() if isinstance(v, dict))
        if command_map_path.exists()
        else []
    )

    canvar_rows = []
    for name, entry in can_variables.items():
        discovery = entry.get("discovery", {})
        canvar_rows.append(
            {
                "name": name,
                "component": discovery.get("component", "number"),
                "unit": discovery.get("unit", ""),
                "min": discovery.get("min", ""),
                "max": discovery.get("max", ""),
                "step": discovery.get("step", ""),
                "options": ", ".join(discovery.get("options", [])),
            }
        )
    while len(canvar_rows) < CAN_VARIABLE_ROWS:
        canvar_rows.append({"name": "", "component": "number", "unit": "", "min": "", "max": "", "step": "", "options": ""})
    canvar_rows = canvar_rows[:CAN_VARIABLE_ROWS]

    ta_net_outputs = mapping.get("ta_network_outputs", {})
    ta_net_analog = (ta_net_outputs.get("analog", []) + [None] * TA_NETWORK_OUTPUT_SLOTS)[:TA_NETWORK_OUTPUT_SLOTS]
    ta_net_digital = (ta_net_outputs.get("digital", []) + [None] * TA_NETWORK_OUTPUT_SLOTS)[:TA_NETWORK_OUTPUT_SLOTS]

    return render_template(
        "can_settings.html",
        bitrate=mapping.get("bitrate", proto.DEFAULT_BITRATE),
        own_node_number=mapping.get("own_node_number", 1),
        columns=columns,
        total_slots=TOTAL_SLOTS,
        available_subtopics=sorted(available_subtopics),
        available_set_keys=available_set_keys,
        canvar_rows=canvar_rows,
        ta_net_analog=ta_net_analog,
        ta_net_digital=ta_net_digital,
        num_canvar_rows=range(CAN_VARIABLE_ROWS),
        message=message,
        message_ok=message_ok,
    )


READ_CYCLES_PATH = PROJECT_ROOT / "config" / "read_cycles.json"
COMMAND_MAP_PATH_UI = PROJECT_ROOT / "config" / "command_map.json"
DISPLAY_NAMES_PATH_UI = PROJECT_ROOT / "config" / "display_names.json"
CYCLE_COUNT = 4  # feste Anzahl konfigurierbarer Zyklen


def load_read_cycles() -> dict:
    if not READ_CYCLES_PATH.exists():
        return {}
    return json.loads(READ_CYCLES_PATH.read_text())


def load_command_map_ui() -> dict:
    if not COMMAND_MAP_PATH_UI.exists():
        return {}
    return {k: v for k, v in json.loads(COMMAND_MAP_PATH_UI.read_text()).items() if isinstance(v, dict)}


@app.route("/variables", methods=["GET", "POST"])
def variables_page():
    message = None
    message_ok = None
    cfg = get_ui_config()
    variables = vito_variables.try_load_variables(cfg["device_xml"])  # {name: {"get":..., "set":...}}
    cycles = load_read_cycles()
    command_map = load_command_map_ui()

    # Bestehende Zyklen auf die 4 festen Slots abbilden (Reihenfolge = Einfüge-Reihenfolge in der JSON)
    cycle_names = list(cycles.keys())

    if request.method == "POST":
        errors = []

        cycle_defs = []
        for i in range(CYCLE_COUNT):
            name = request.form.get(f"cycle_name_{i}", "").strip()
            interval_raw = request.form.get(f"cycle_interval_{i}", "").strip()
            if not name:
                cycle_defs.append(None)
                continue
            try:
                interval = int(interval_raw)
                if interval <= 0:
                    raise ValueError
            except ValueError:
                errors.append(f"Zyklus {i + 1} ('{name}'): ungültiges Intervall")
                cycle_defs.append(None)
                continue
            cycle_defs.append({"name": name, "interval_seconds": interval, "variables": []})

        new_command_map = {}
        new_display_names = {}
        for var_name, cmds in variables.items():
            cycle_choice = request.form.get(f"var_cycle_{var_name}", "")
            if cycle_choice:
                idx = int(cycle_choice)
                if cycle_defs[idx] is None:
                    errors.append(f"'{var_name}': Zyklus {idx + 1} ist nicht definiert")
                else:
                    cycle_defs[idx]["variables"].append(var_name)

            display_name = request.form.get(f"var_display_name_{var_name}", "").strip()
            if display_name:
                new_display_names[var_name] = display_name

            if not cmds.get("set"):
                continue  # ohne Setter in vito.xml kann diese Variable nicht settable sein
            if request.form.get(f"var_settable_{var_name}") != "1":
                continue

            entry = {}
            component = request.form.get(f"var_component_{var_name}", "number")
            discovery = {"component": component}
            if component == "number":
                unit = request.form.get(f"var_unit_{var_name}", "").strip()
                if unit:
                    discovery["unit"] = unit
                for field in ("min", "max", "step"):
                    raw = request.form.get(f"var_{field}_{var_name}", "").strip()
                    if raw:
                        try:
                            discovery[field] = float(raw) if "." in raw else int(raw)
                        except ValueError:
                            errors.append(f"'{var_name}': ungültiger Wert für {field}")
            elif component == "select":
                options_raw = request.form.get(f"var_options_{var_name}", "").strip()
                discovery["options"] = [o.strip() for o in options_raw.split(",") if o.strip()]
            entry["discovery"] = discovery
            new_command_map[var_name] = entry

        if errors:
            message, message_ok = " / ".join(errors), False
        else:
            new_cycles = {c["name"]: {"interval_seconds": c["interval_seconds"], "variables": c["variables"]}
                          for c in cycle_defs if c is not None}
            READ_CYCLES_PATH.parent.mkdir(parents=True, exist_ok=True)
            READ_CYCLES_PATH.write_text(json.dumps(new_cycles, indent=2, ensure_ascii=False) + "\n")
            COMMAND_MAP_PATH_UI.write_text(json.dumps(new_command_map, indent=2, ensure_ascii=False) + "\n")
            DISPLAY_NAMES_PATH_UI.write_text(json.dumps(new_display_names, indent=2, ensure_ascii=False) + "\n")
            cycles = new_cycles
            command_map = new_command_map
            cycle_names = list(cycles.keys())

            restarted = []
            for service in ("orchestrator",):
                status = diagnostics.service_status(service)
                if status["state"] == "active":
                    result = diagnostics.restart_service(service)
                    if result["ok"]:
                        restarted.append(service)
            message = "Gespeichert." + (f" Neu gestartet: {', '.join(restarted)}." if restarted else "")
            message_ok = True

    # Zyklus-Zuordnung pro Variable ermitteln (welcher Index in cycle_names, falls überhaupt)
    variable_cycle_index = {}
    for idx, cname in enumerate(cycle_names):
        for var_name in cycles[cname].get("variables", []):
            variable_cycle_index[var_name] = idx

    display_names = vito_variables.load_display_names()
    rows = []
    for var_name, cmds in variables.items():
        entry = command_map.get(var_name, {})
        discovery = entry.get("discovery", {})
        rows.append(
            {
                "name": var_name,
                "friendly_name": vito_variables.friendly_name(var_name),
                "display_name": display_names.get(var_name, ""),
                "get": cmds.get("get"),
                "set": cmds.get("set"),
                "cycle_index": variable_cycle_index.get(var_name),
                "settable": var_name in command_map,
                "component": discovery.get("component", "number"),
                "unit": discovery.get("unit", ""),
                "min": discovery.get("min", ""),
                "max": discovery.get("max", ""),
                "step": discovery.get("step", ""),
                "options": ", ".join(discovery.get("options", [])),
            }
        )

    cycle_rows = []
    for i in range(CYCLE_COUNT):
        if i < len(cycle_names):
            cycle_rows.append({"name": cycle_names[i], "interval_seconds": cycles[cycle_names[i]]["interval_seconds"]})
        else:
            cycle_rows.append({"name": "", "interval_seconds": 30})

    return render_template(
        "variables.html",
        cycle_rows=cycle_rows,
        num_cycles=range(CYCLE_COUNT),
        rows=rows,
        device_xml=cfg["device_xml"],
        message=message,
        message_ok=message_ok,
    )


@app.route("/diagnostics")
def diagnostics_page():
    cfg = get_ui_config()
    mqtt_env = load_env(MQTT_ENV_PATH)

    services = [diagnostics.service_status(name) for name in cfg["services"]]
    can_status = diagnostics.can_link_status(cfg["can_interface"])

    mqtt_status = None
    if mqtt_env:
        mqtt_status = diagnostics.mqtt_connectivity(
            mqtt_env["MQTT_HOST"],
            int(mqtt_env.get("MQTT_PORT", 1883)),
            mqtt_env.get("MQTT_USERNAME") or None,
            mqtt_env.get("MQTT_PASSWORD") or None,
        )

    return render_template(
        "diagnostics.html",
        cfg=cfg,
        services=services,
        can_status=can_status,
        mqtt_status=mqtt_status,
    )


@app.route("/diagnostics/log/<service>")
def diagnostics_log(service: str):
    cfg = get_ui_config()
    if service not in cfg["services"]:
        return jsonify({"error": "Unbekannter Dienst"}), 404
    return jsonify({"log": diagnostics.service_log(service)})


@app.route("/can-sniffer", methods=["GET"])
def can_sniffer_page():
    cfg = get_ui_config()
    return render_template("can_sniffer.html", cfg=cfg)


@app.route("/can-sniffer/capture", methods=["POST"])
def can_sniffer_capture():
    cfg = get_ui_config()
    duration = float(request.form.get("duration", 5))
    duration = max(1.0, min(duration, 30.0))
    frames = can_sniffer.capture(cfg["can_interface"], duration_seconds=duration)
    return jsonify({"frames": frames})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
