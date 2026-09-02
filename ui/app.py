#!/usr/bin/env python3
"""
Web-UI für vcontrold: Kommando-Konsole, Config-Importer, Diagnose, CAN-Sniffer.

Start (Entwicklung): python3 app.py
Produktiv: siehe systemd/vcontrold-ui.service im Projekt-Root.

Sicherheitshinweis: Diese UI kann Schreibbefehle an die Heizung senden. Nicht ohne
Login (Session-Cookie, Credentials siehe ui.env) und nicht ungeschützt im Internet
exponieren.
"""
import datetime
import json
import os
import pathlib
import shutil
import sys
import xml.etree.ElementTree as ET

import hmac

from flask import Flask, flash, jsonify, redirect, render_template, request, session, url_for
from werkzeug.utils import secure_filename

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))

import can_sniffer
import diagnostics
import mqtt_variables as mqtt_vars
import ta_can_protocol as proto
import vclient_wrapper
import vito_variables
import xml_parser

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
UI_ENV_PATH = pathlib.Path(__file__).resolve().parent / "ui.env"
MQTT_ENV_PATH = PROJECT_ROOT / "config" / "mqtt.env"
CAN_MAPPING_PATH = PROJECT_ROOT / "config" / "can_mapping.json"

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024  # 2 MB reicht für Geräte-XML
app.config["TEMPLATES_AUTO_RELOAD"] = True  # Templates immer frisch von der Platte lesen
# Signiert die Session-Cookie (Login-Status + flash()-Nachrichten). Ein bei jedem Start neu
# generierter Key ist hier unproblematisch: er invalidiert nach einem Neustart höchstens die
# aktive Anmeldung und offene Flash-Nachrichten, kein Sicherheitsrisiko.
app.secret_key = os.urandom(24)


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
    if request.endpoint in ("login", "logout", "static"):
        return None
    if not session.get("user"):
        return redirect(url_for("login", next=request.path))
    return None


def check_credentials(cfg: dict, username: str, password: str) -> bool:
    return hmac.compare_digest(username, cfg["username"]) and hmac.compare_digest(
        password, cfg["password"]
    )


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    cfg = get_ui_config()
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if check_credentials(cfg, username, password):
            session["user"] = username
            # Nur lokale Pfade akzeptieren (sonst Open-Redirect via ?next=)
            next_page = request.args.get("next") or url_for("dashboard")
            if not next_page.startswith("/"):
                next_page = url_for("dashboard")
            return redirect(next_page)
        error = "Benutzername oder Passwort falsch."
    elif session.get("user"):
        return redirect(url_for("dashboard"))
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


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

        if request.form.get("return_to") == "vcontrold":
            if result:
                category = "message-ok" if result["ok"] else "message-error"
                flash(f"Konsole ({executed_command}): {result['output']}", category)
            return redirect(url_for("vcontrold_page") + "#konsole")

    return render_template(
        "console.html",
        cfg=cfg,
        commands=commands,
        result=result,
        executed_command=executed_command,
        post_action=url_for("console"),
    )


def finish_vcontrold_restart(action_desc: str, also_restart_orchestrator: bool = False) -> tuple[str, bool]:
    restart = diagnostics.restart_service("vcontrold")
    status = diagnostics.service_status("vcontrold")
    if not (restart["ok"] and status["state"] == "active"):
        return (
            f"{action_desc}, aber vcontrold-Neustart fehlgeschlagen: {restart['detail'] or status['state']}",
            False,
        )
    message, ok = f"{action_desc}, vcontrold läuft.", True

    if also_restart_orchestrator:
        # vito.xml bestimmt, welche Getter/Setter der Orchestrator kennt -- ohne Neustart
        # würde er mit der alten Variablenliste weiterlaufen und z.B. entfernte Variablen
        # weiterhin als vorhanden behandeln (siehe README "MQTT-Architektur").
        orch_status = diagnostics.service_status("orchestrator")
        if orch_status["state"] == "active":
            orch_restart = diagnostics.restart_service("orchestrator")
            if orch_restart["ok"]:
                message += " orchestrator neu gestartet (übernimmt die neue Variablenliste)."
            else:
                message += f" ACHTUNG: orchestrator-Neustart fehlgeschlagen ({orch_restart['detail']}) -- läuft mit alter vito.xml weiter."
                ok = False

    return message, ok


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
                    message, message_ok = finish_vcontrold_restart(
                        f"{path.name} importiert", also_restart_orchestrator=(target == "device")
                    )
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
                    message, message_ok = finish_vcontrold_restart(
                        f"{path.name} gespeichert", also_restart_orchestrator=(target == "device")
                    )

        if request.form.get("return_to") == "vcontrold":
            if message:
                flash(message, "message-ok" if message_ok else "message-error")
            anchor = "vcontrold-xml" if target == "main" else "vito-xml"
            return redirect(url_for("vcontrold_page") + f"#{anchor}")

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
        post_action=url_for("config_page"),
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


def load_can_mapping() -> dict:
    if not CAN_MAPPING_PATH.exists():
        return {"bitrate": proto.DEFAULT_BITRATE, "own_node_number": 1}
    return json.loads(CAN_MAPPING_PATH.read_text())


TA_NETWORK_OUTPUT_SLOTS = 16  # TA-Netzwerkausgänge (bestätigtes Schema, siehe ta_canopen.py)
TA_RX_OUTPUT_SLOTS = 16  # rx_ta_analog_outputs/rx_ta_digital_outputs: Ausgang 1-16 (siehe README 3.4/3.5)


def parse_ta_rx_output_rows(key: str) -> dict:
    """Baut das 'outputs'-Dict für rx_ta_analog_outputs/rx_ta_digital_outputs aus den
    Formularfeldern '{key}_slot_topic_<i>'/'{key}_slot_forward_<i>' (i=0..15, Ausgang i+1)."""
    outputs = {}
    for i in range(TA_RX_OUTPUT_SLOTS):
        topic = request.form.get(f"{key}_slot_topic_{i}", "").strip()
        if not topic:
            continue
        forward = request.form.get(f"{key}_slot_forward_{i}", "").strip()
        outputs[str(i + 1)] = {"topic": topic, "forward_as_set": forward} if forward else topic
    return outputs


def build_ta_rx_output_rows(config: dict) -> list:
    """Gegenstück zu parse_ta_rx_output_rows: 'outputs'-Dict -> flache Liste für's Template."""
    outputs = (config or {}).get("outputs", {})
    rows = []
    for i in range(TA_RX_OUTPUT_SLOTS):
        c = outputs.get(str(i + 1))
        if isinstance(c, dict):
            rows.append({"topic": c.get("topic", ""), "forward": c.get("forward_as_set", "")})
        else:
            rows.append({"topic": c or "", "forward": ""})
    return rows


def parse_discovery_fields(prefix: str, suffix, error_label: str, errors: list) -> dict:
    """Baut ein 'discovery'-Dict (component/unit/min/max/step/options) aus Formularfeldern
    '{prefix}_component_{suffix}'/'_unit_'/'_min_'/'_max_'/'_step_'/'_options_' -- gemeinsame
    Logik für can_settings() (custom CAN-Variablen, suffix=Zeilenindex) und variables_page()
    (settable vito.xml-Variablen, suffix=Variablenname). Ungültige Zahlenwerte werden mit
    error_label als Präfix an `errors` angehängt."""
    component = request.form.get(f"{prefix}_component_{suffix}", "number")
    discovery = {"component": component}
    if component == "number":
        unit = request.form.get(f"{prefix}_unit_{suffix}", "").strip()
        if unit:
            discovery["unit"] = unit
        for field in ("min", "max", "step"):
            raw = request.form.get(f"{prefix}_{field}_{suffix}", "").strip()
            if raw:
                try:
                    discovery[field] = float(raw) if "." in raw else int(raw)
                except ValueError:
                    errors.append(f"{error_label}: ungültiger Wert für {field}")
    elif component == "select":
        options_raw = request.form.get(f"{prefix}_options_{suffix}", "").strip()
        discovery["options"] = [o.strip() for o in options_raw.split(",") if o.strip()]
    return discovery


@app.route("/can-settings", methods=["GET", "POST"])
def can_settings():
    message = None
    message_ok = None
    cfg = get_ui_config()
    mapping = load_can_mapping()
    mqtt_variables = mqtt_vars.load()
    vito_vars = vito_variables.try_load_variables(cfg["device_xml"])  # {name: {"get":..., "set":...}}

    if request.method == "POST":
        new_mapping = {
            "bitrate": int(request.form.get("bitrate", proto.DEFAULT_BITRATE) or proto.DEFAULT_BITRATE),
            "own_node_number": int(request.form.get("own_node_number", 1) or 1),
        }
        # sdo_record hat (noch) keine eigenen Formularfelder auf dieser Seite -- unverändert
        # übernehmen, sonst würde ein Speichern hier eine manuell/per JSON angelegte
        # sdo_record-Konfiguration stillschweigend löschen.
        if "sdo_record" in mapping:
            new_mapping["sdo_record"] = mapping["sdo_record"]
        errors = []

        for key in ("rx_ta_analog_outputs", "rx_ta_digital_outputs"):
            can_id_raw = request.form.get(f"{key}_can_id", "").strip()
            outputs = parse_ta_rx_output_rows(key)
            if not can_id_raw:
                if outputs:
                    errors.append(f"{key}: Ausgänge belegt, aber keine CAN-ID gesetzt")
                continue
            try:
                can_id_int = int(can_id_raw, 0)
            except ValueError:
                errors.append(f"{key}: ungültige CAN-ID '{can_id_raw}'")
                continue
            new_mapping[key] = {"can_id": hex(can_id_int), "outputs": outputs}

        ta_net_analog = [
            request.form.get(f"ta_net_analog_{i}", "").strip() or None for i in range(TA_NETWORK_OUTPUT_SLOTS)
        ]
        ta_net_digital = [
            request.form.get(f"ta_net_digital_{i}", "").strip() or None for i in range(TA_NETWORK_OUTPUT_SLOTS)
        ]
        if any(ta_net_analog) or any(ta_net_digital):
            new_mapping["ta_network_outputs"] = {"analog": ta_net_analog, "digital": ta_net_digital}

        if errors:
            message, message_ok = " / ".join(errors), False
            mapping = new_mapping  # editierte (fehlerhafte) Werte im Formular zeigen
        else:
            CAN_MAPPING_PATH.parent.mkdir(parents=True, exist_ok=True)
            CAN_MAPPING_PATH.write_text(json.dumps(new_mapping, indent=2, ensure_ascii=False) + "\n")
            mapping = new_mapping

            status = diagnostics.service_status("can-node")
            if status["state"] == "active":
                restart = diagnostics.restart_service("can-node")
                if restart["ok"]:
                    message, message_ok = "Gespeichert, can-node neu gestartet.", True
                else:
                    message, message_ok = f"Gespeichert, aber Neustart fehlgeschlagen: {restart['detail']}", False
            else:
                message, message_ok = "Gespeichert. can-node läuft nicht, wurde nicht neu gestartet.", True

    # Vorschlagsliste für Senden-Felder (TA-Netzwerkausgänge): alle vito.xml-Variablen, nicht nur
    # die bereits einem Zyklus zugeordneten -- eine Variable OHNE Zyklus hat aber (noch) keinen
    # aktuellen Wert auf heizung/<name> und sendet daher nichts, bis sie auf der
    # Vcontrold-Seite (Abschnitt "MQTT-Konfiguration") einem Zyklus zugeordnet wird.
    available_subtopics = set(vito_vars.keys())
    for cycle in load_read_cycles().values():
        available_subtopics.update(cycle.get("variables", []))

    # Weiterleitungsziele ("Weiterleitung"-Dropdown bei den Empfangs-Tabellen): nur Vcontrold-
    # settable Variablen (Name existiert in vito.xml UND hat einen schreibbaren discovery-Eintrag
    # in config/mqtt_variables.json) -- siehe orchestrator.py/handle_set_request().
    available_set_keys = sorted(
        k for k, v in mqtt_variables.items() if k in vito_vars and mqtt_vars.is_writable(v)
    )

    ta_net_outputs = mapping.get("ta_network_outputs", {})
    ta_net_analog = (ta_net_outputs.get("analog", []) + [None] * TA_NETWORK_OUTPUT_SLOTS)[:TA_NETWORK_OUTPUT_SLOTS]
    ta_net_digital = (ta_net_outputs.get("digital", []) + [None] * TA_NETWORK_OUTPUT_SLOTS)[:TA_NETWORK_OUTPUT_SLOTS]

    # Vorschlagsliste für Empfangs-Kanalnamen ("existierende Variable" statt neuer Name): bereits
    # verwendete Kanäle aus allen rx-Wegen + alle CAN-only-Variablen aus der MQTT-Variablen-Seite
    # (Name nicht in vito.xml). Freies Eintippen bleibt möglich (Datalist erzwingt nichts), das
    # deckt "neu anzulegende Variable" ab.
    existing_uvr_topics = set()
    for key in ("rx_ta_analog_outputs", "rx_ta_digital_outputs"):
        for c in mapping.get(key, {}).get("outputs", {}).values():
            existing_uvr_topics.add(c["topic"] if isinstance(c, dict) else c)
    for c in mapping.get("sdo_record", {}).get("slots", {}).values():
        existing_uvr_topics.add(c["topic"] if isinstance(c, dict) else c)
    existing_uvr_topics.update(k for k in mqtt_variables if k not in vito_vars)

    return render_template(
        "can_settings.html",
        bitrate=mapping.get("bitrate", proto.DEFAULT_BITRATE),
        rx_ta_analog_can_id=mapping.get("rx_ta_analog_outputs", {}).get("can_id", ""),
        rx_ta_analog_rows=build_ta_rx_output_rows(mapping.get("rx_ta_analog_outputs")),
        rx_ta_digital_can_id=mapping.get("rx_ta_digital_outputs", {}).get("can_id", ""),
        rx_ta_digital_rows=build_ta_rx_output_rows(mapping.get("rx_ta_digital_outputs")),
        ta_rx_output_slots=TA_RX_OUTPUT_SLOTS,
        uvr_topics=sorted(existing_uvr_topics),
        own_node_number=mapping.get("own_node_number", 1),
        total_slots=TA_NETWORK_OUTPUT_SLOTS,
        available_subtopics=sorted(available_subtopics),
        available_set_keys=available_set_keys,
        ta_net_analog=ta_net_analog,
        ta_net_digital=ta_net_digital,
        message=message,
        message_ok=message_ok,
    )


READ_CYCLES_PATH = PROJECT_ROOT / "config" / "read_cycles.json"
CYCLE_COUNT = 4  # feste Anzahl konfigurierbarer Zyklen


def load_read_cycles() -> dict:
    if not READ_CYCLES_PATH.exists():
        return {}
    return json.loads(READ_CYCLES_PATH.read_text())


def build_variables_view_data(cfg: dict, variables: dict, cycles: dict, mqtt_variables: dict) -> dict:
    """Baut cycle_rows/rows fürs Variablen-Template aus den geladenen Rohdaten -- gemeinsam
    genutzt von variables_page() (GET) und vcontrold_page() (eingebettete Ansicht), damit beide
    garantiert dieselbe Aufbereitung zeigen. Reine Zyklus-Zuordnung -- Anzeigename und
    Home-Assistant-Discovery-Konfiguration werden auf der eigenständigen MQTT-Variablen-Seite
    gepflegt (siehe mqtt_variables_page()), nicht hier."""
    cycle_names = list(cycles.keys())
    variable_cycle_index = {}
    for idx, cname in enumerate(cycle_names):
        for var_name in cycles[cname].get("variables", []):
            variable_cycle_index[var_name] = idx

    display_names = mqtt_vars.display_names(mqtt_variables)
    variable_state = vito_variables.load_variable_state()
    rows = []
    for var_name, cmds in variables.items():
        entry = mqtt_variables.get(var_name, {})
        rows.append(
            {
                "name": var_name,
                "friendly_name": display_names.get(var_name) or vito_variables.friendly_name(var_name),
                "get": cmds.get("get"),
                "set": cmds.get("set"),
                "cycle_index": variable_cycle_index.get(var_name),
                "settable": cmds.get("set") is not None and mqtt_vars.is_writable(entry),
                "active": vito_variables.is_active(var_name, variable_state),
            }
        )

    cycle_rows = []
    for i in range(CYCLE_COUNT):
        if i < len(cycle_names):
            cycle_rows.append({"name": cycle_names[i], "interval_seconds": cycles[cycle_names[i]]["interval_seconds"]})
        else:
            cycle_rows.append({"name": "", "interval_seconds": 30})

    return {"cycle_rows": cycle_rows, "num_cycles": range(CYCLE_COUNT), "rows": rows}


def build_vito_override_data(cfg: dict, variables: dict) -> dict:
    """Berechnet die Grundlage für den Abschnitt 'Get/Set-Zuordnung überschreiben': vito.xml-
    Kommandos, die NICHT der getXXX/setXXX-Namenskonvention folgen (oder aus einem anderen Grund
    nicht automatisch gepaart wurden) und deshalb sonst unsichtbar blieben, plus die aktuell
    gespeicherten manuellen Overrides -- siehe vito_variables.load_variables()."""
    raw_commands = vito_variables.try_list_raw_commands(cfg["device_xml"])
    used_names = {cmd for entry in variables.values() for cmd in (entry.get("get"), entry.get("set")) if cmd}
    unassigned_commands = [c for c in raw_commands if c["name"] not in used_names]
    overrides = vito_variables.load_overrides()
    override_rows = [
        {"name": var_name, "get": entry.get("get") or "", "set": entry.get("set") or ""}
        for var_name, entry in sorted(overrides.items())
    ]
    return {
        "raw_commands": raw_commands,
        "unassigned_commands": unassigned_commands,
        "override_rows": override_rows,
    }


BLANK_OVERRIDE_ROWS = 3  # zusätzliche leere Zeilen für neue Get/Set-Overrides; "+ Zeile" ergänzt bei Bedarf mehr


@app.route("/vito-overrides", methods=["POST"])
def vito_overrides_page():
    """Speichert manuelle Get/Set-Zuordnungen (config/vito_command_overrides.json) für vito.xml-
    Kommandos, die nicht der getXXX/setXXX-Namenskonvention folgen -- siehe
    vito_variables.load_variables()/build_vito_override_data(). Eingebettet in variables.html/
    vcontrold.html, kein eigener GET-Seitenaufruf nötig."""
    cfg = get_ui_config()
    raw_command_names = {c["name"] for c in vito_variables.try_list_raw_commands(cfg["device_xml"])}
    errors = []

    indices = sorted(
        int(key[len("override_name_"):])
        for key in request.form
        if key.startswith("override_name_") and key[len("override_name_"):].isdigit()
    )
    new_overrides = {}
    for i in indices:
        var_name = request.form.get(f"override_name_{i}", "").strip()
        if not var_name:
            continue
        get_cmd = request.form.get(f"override_get_{i}", "").strip()
        set_cmd = request.form.get(f"override_set_{i}", "").strip()
        if not get_cmd and not set_cmd:
            errors.append(f"'{var_name}': weder Get- noch Set-Kommando gewählt")
            continue
        if get_cmd and get_cmd not in raw_command_names:
            errors.append(f"'{var_name}': Get-Kommando '{get_cmd}' existiert nicht in vito.xml")
            continue
        if set_cmd and set_cmd not in raw_command_names:
            errors.append(f"'{var_name}': Set-Kommando '{set_cmd}' existiert nicht in vito.xml")
            continue
        new_overrides[var_name] = {"get": get_cmd or None, "set": set_cmd or None}

    if errors:
        message, message_ok = " / ".join(errors), False
    else:
        vito_variables.save_overrides(new_overrides)
        restarted = []
        status = diagnostics.service_status("orchestrator")
        if status["state"] == "active":
            result = diagnostics.restart_service("orchestrator")
            if result["ok"]:
                restarted.append("orchestrator")
        message = "Gespeichert." + (f" Neu gestartet: {', '.join(restarted)}." if restarted else "")
        message_ok = True

    flash(message, "message-ok" if message_ok else "message-error")
    if request.form.get("return_to") == "vcontrold":
        return redirect(url_for("vcontrold_page") + "#variablen")
    return redirect(url_for("variables_page"))


@app.route("/variables", methods=["GET", "POST"])
def variables_page():
    message = None
    message_ok = None
    cfg = get_ui_config()
    variables = vito_variables.try_load_variables(cfg["device_xml"])  # {name: {"get":..., "set":...}}
    cycles = load_read_cycles()
    mqtt_variables = mqtt_vars.load()

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

        new_variable_state = {}
        for var_name in variables:
            active = request.form.get(f"var_active_{var_name}") == "1"
            if not active:
                new_variable_state[var_name] = {"active": False}

            cycle_choice = request.form.get(f"var_cycle_{var_name}", "")
            if cycle_choice and active:
                idx = int(cycle_choice)
                if cycle_defs[idx] is None:
                    errors.append(f"'{var_name}': Zyklus {idx + 1} ist nicht definiert")
                else:
                    cycle_defs[idx]["variables"].append(var_name)

        if errors:
            message, message_ok = " / ".join(errors), False
        else:
            new_cycles = {c["name"]: {"interval_seconds": c["interval_seconds"], "variables": c["variables"]}
                          for c in cycle_defs if c is not None}
            READ_CYCLES_PATH.parent.mkdir(parents=True, exist_ok=True)
            READ_CYCLES_PATH.write_text(json.dumps(new_cycles, indent=2, ensure_ascii=False) + "\n")
            vito_variables.save_variable_state(new_variable_state)
            cycles = new_cycles
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

        if request.form.get("return_to") == "vcontrold":
            if message:
                flash(message, "message-ok" if message_ok else "message-error")
            return redirect(url_for("vcontrold_page") + "#variablen")

    return render_template(
        "variables.html",
        device_xml=cfg["device_xml"],
        message=message,
        message_ok=message_ok,
        post_action=url_for("variables_page"),
        **build_variables_view_data(cfg, variables, cycles, mqtt_variables),
        **build_vito_override_data(cfg, variables),
    )


@app.route("/vcontrold")
def vcontrold_page():
    """Gebündelte Seite: Vcontrold-Konfiguration (vcontrold.xml), Konsole, vito.xml, Variablen
    und ein Live-Log der Vitotronic-Kommunikation, je in einem eigenen aufklappbaren Abschnitt.
    Jeder Abschnitt postet weiterhin an seine eigene, unveränderte Route (/config, /console,
    /variables) -- die erkennen am 'return_to'-Feld, dass sie hierher zurückleiten sollen, statt
    ihre eigene Standalone-Seite zu rendern (die unter /config, /console, /variables weiterhin
    einzeln erreichbar bleiben)."""
    cfg = get_ui_config()

    main_path = pathlib.Path(cfg["vcontrold_main_xml"])
    device_path = pathlib.Path(cfg["device_xml"]) if cfg["device_xml"] else None
    main_content = main_path.read_text() if main_path.exists() else ""
    device_content = device_path.read_text() if device_path is not None and device_path.exists() else ""

    commands = xml_parser.try_extract_commands(cfg["device_xml"])

    variables = vito_variables.try_load_variables(cfg["device_xml"])
    cycles = load_read_cycles()
    mqtt_variables = mqtt_vars.load()

    return render_template(
        "vcontrold.html",
        cfg=cfg,
        main_path=str(main_path),
        device_path=str(device_path) if device_path else None,
        main_content=main_content,
        device_content=device_content,
        commands=commands,
        result=None,
        executed_command="",
        device_xml=cfg["device_xml"],
        post_action_config=url_for("config_page"),
        post_action_console=url_for("console"),
        post_action_variables=url_for("variables_page"),
        post_action_vito_overrides=url_for("vito_overrides_page"),
        **build_variables_view_data(cfg, variables, cycles, mqtt_variables),
        **build_vito_override_data(cfg, variables),
    )


BLANK_CUSTOM_VARIABLE_ROWS = 3  # zusätzliche leere Zeilen für neue Custom-CAN-Variablen; "+ Zeile"
# im Browser fügt bei Bedarf beliebig mehr hinzu (kein fixes Limit, da die Anzahl je nach
# CAN-Ausbaustufe stark variieren kann)


def build_mqtt_variable_rows(entry: dict) -> dict:
    discovery = entry.get("discovery", {})
    return {
        "display_name": entry.get("display_name", ""),
        "writable": bool(discovery),
        "component": discovery.get("component", "number"),
        "unit": discovery.get("unit", ""),
        "min": discovery.get("min", ""),
        "max": discovery.get("max", ""),
        "step": discovery.get("step", ""),
        "options": ", ".join(discovery.get("options", [])),
    }


@app.route("/mqtt-variables", methods=["GET", "POST"])
def mqtt_variables_page():
    """Eigenständige Seite (siehe README 'MQTT-Architektur'): definiert Anzeigename +
    Home-Assistant-Discovery-Konfiguration für alle MQTT-Variablen, unabhängig davon, ob der Wert
    aus vito.xml (Vcontrold) oder direkt von CAN stammt -- weder Bestandteil der Vcontrold- noch
    der CAN-Einstellungen-Seite. Zyklus-Zuordnung bleibt bewusst auf der Vcontrold-Seite (siehe
    variables_page()), Konfiguration von CAN-IDs/Sendekanälen bleibt auf der CAN-Einstellungen-
    Seite -- hier geht es nur um die Frage 'wie heißt die Variable und wie zeigt sie sich in
    Home Assistant'."""
    message = None
    message_ok = None
    cfg = get_ui_config()
    vito_vars = vito_variables.try_load_variables(cfg["device_xml"])  # {name: {"get":..., "set":...}}
    mqtt_variables = mqtt_vars.load()

    if request.method == "POST":
        errors = []
        new_variables = {}

        for var_name, cmds in vito_vars.items():
            display_name = request.form.get(f"vitovar_display_{var_name}", "").strip()
            entry = {}
            if display_name:
                entry["display_name"] = display_name
            if cmds.get("set") and request.form.get(f"vitovar_writable_{var_name}") == "1":
                entry["discovery"] = parse_discovery_fields("vitovar", var_name, f"'{var_name}'", errors)
            if entry:
                new_variables[var_name] = entry

        # Zeilenindizes kommen aus den tatsächlich übermittelten Formularfeldern, nicht aus einem
        # festen Bereich -- die Custom-CAN-Variablen-Tabelle kann im Browser per "+ Zeile" beliebig
        # viele zusätzliche Zeilen bekommen (kein serverseitiges Limit).
        customvar_indices = sorted(
            int(key[len("customvar_name_"):])
            for key in request.form
            if key.startswith("customvar_name_") and key[len("customvar_name_"):].isdigit()
        )
        for i in customvar_indices:
            name = request.form.get(f"customvar_name_{i}", "").strip()
            if not name:
                continue
            if name in vito_vars:
                errors.append(f"'{name}' ist bereits eine vito.xml-Variable -- oben editieren, nicht hier")
                continue
            entry = {}
            display_name = request.form.get(f"customvar_display_{i}", "").strip()
            if display_name:
                entry["display_name"] = display_name
            entry["discovery"] = parse_discovery_fields("customvar", i, f"'{name}'", errors)
            new_variables[name] = entry

        if errors:
            message, message_ok = " / ".join(errors), False
            mqtt_variables = new_variables  # editierte (fehlerhafte) Werte im Formular zeigen
        else:
            mqtt_vars.save(new_variables)
            mqtt_variables = new_variables

            restarted, failed = [], []
            for service in ("orchestrator", "can-node"):
                status = diagnostics.service_status(service)
                if status["state"] != "active":
                    continue
                result = diagnostics.restart_service(service)
                (restarted if result["ok"] else failed).append(service)
            message = "Gespeichert."
            if restarted:
                message += f" Neu gestartet: {', '.join(restarted)}."
            if failed:
                message += f" Fehler beim Neustart von: {', '.join(failed)}."
            message_ok = not failed

    vito_rows = []
    for var_name, cmds in sorted(vito_vars.items()):
        entry = mqtt_variables.get(var_name, {})
        row = build_mqtt_variable_rows(entry)
        # Set-Variablen sind direkt nach dem Erkennen aus vito.xml defaultmäßig aktiv (schreibbar)
        # -- nur solange sie noch nie konfiguriert wurden (kein Eintrag in mqtt_variables.json).
        # Sobald einmal gespeichert, gilt der explizit gespeicherte Zustand, auch wenn er "aus" ist.
        if var_name not in mqtt_variables and cmds.get("set"):
            row["writable"] = True
        row["name"] = var_name
        row["friendly_name"] = vito_variables.friendly_name(var_name)
        row["has_setter"] = bool(cmds.get("set"))
        vito_rows.append(row)

    custom_rows = []
    for name, entry in sorted(mqtt_variables.items()):
        if name in vito_vars:
            continue
        row = build_mqtt_variable_rows(entry)
        row["name"] = name
        custom_rows.append(row)
    for _ in range(BLANK_CUSTOM_VARIABLE_ROWS):
        custom_rows.append({"name": "", "display_name": "", "component": "number", "unit": "", "min": "", "max": "", "step": "", "options": ""})

    return render_template(
        "mqtt_variables.html",
        vito_rows=vito_rows,
        custom_rows=custom_rows,
        next_custom_index=len(custom_rows),
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


# vcontrold schreibt die eigentliche Get/Set-Kommunikation (mit -g/--debug in
# systemd/vcontrold.service) nicht nach journalctl, sondern in diese eigene Logdatei -- der
# systemd-Journal-Log (siehe diagnostics_log oben) zeigt für vcontrold nur Start/Stop-Meldungen.
VCONTROLD_DEBUG_LOG_PATH = pathlib.Path("/tmp/vcontrold.log")


def tail_file(path: pathlib.Path, lines: int) -> str:
    if not path.exists():
        return f"{path} existiert nicht -- läuft vcontrold? (Datei wird bei jedem Start neu angelegt)"
    try:
        content = path.read_text(errors="replace")
    except OSError as exc:
        return f"Fehler beim Lesen von {path}: {exc}"
    all_lines = content.splitlines()
    return "\n".join(all_lines[-lines:]) or "(Logdatei ist leer)"


@app.route("/vcontrold/log")
def vcontrold_debug_log():
    return jsonify({"log": tail_file(VCONTROLD_DEBUG_LOG_PATH, 200)})


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
