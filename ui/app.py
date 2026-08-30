#!/usr/bin/env python3
"""
Web-UI für vcontrold: Kommando-Konsole, Config-Importer, Diagnose, CAN-Sniffer.

Start (Entwicklung): python3 app.py
Produktiv: siehe systemd/vcontrold-ui.service im Projekt-Root.

Sicherheitshinweis: Diese UI kann Schreibbefehle an die Heizung senden. Nicht ohne
Basic-Auth (siehe ui.env) und nicht ungeschützt im Internet exponieren.
"""
import datetime
import pathlib
import shutil
import sys
import xml.etree.ElementTree as ET

from flask import Flask, jsonify, redirect, render_template, request, url_for
from werkzeug.utils import secure_filename

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))

import can_sniffer
import diagnostics
import vclient_wrapper
import xml_parser

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
UI_ENV_PATH = pathlib.Path(__file__).resolve().parent / "ui.env"
MQTT_ENV_PATH = PROJECT_ROOT / "config" / "mqtt.env"

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024  # 2 MB reicht für Geräte-XML


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
        "vcontrold_main_xml": env.get("VCONTROLD_MAIN_XML_PATH", "/etc/vcontrold.xml"),
        "device_xml": env.get("DEVICE_XML_PATH", ""),
        "vclient_host": env.get("VCLIENT_HOST", "localhost"),
        "vclient_port": env.get("VCLIENT_PORT", "3002"),
        "can_interface": env.get("CAN_INTERFACE", "can0"),
        "services": [
            s.strip()
            for s in env.get(
                "MONITORED_SERVICES",
                "vcontrold,can-to-mqtt,mqtt-to-can,mqtt-command-listener,can0-up",
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


@app.route("/config-import", methods=["GET", "POST"])
def config_import():
    cfg = get_ui_config()
    message = None
    message_ok = None

    if request.method == "POST":
        uploaded = request.files.get("xml_file")
        if not uploaded or not uploaded.filename:
            message, message_ok = "Keine Datei ausgewählt.", False
        else:
            filename = secure_filename(uploaded.filename)
            tmp_path = pathlib.Path("/tmp") / filename
            uploaded.save(tmp_path)

            try:
                ET.parse(tmp_path)
            except ET.ParseError as exc:
                message, message_ok = f"Ungültiges XML: {exc}", False
            else:
                target = pathlib.Path(cfg["vcontrold_main_xml"])
                if target.exists():
                    backup = target.with_suffix(
                        target.suffix + f".bak.{datetime.datetime.now():%Y%m%d%H%M%S}"
                    )
                    shutil.copy2(target, backup)
                shutil.copy2(tmp_path, target)

                import subprocess

                restart = subprocess.run(
                    ["systemctl", "restart", "vcontrold"], capture_output=True, text=True, timeout=15
                )
                status = diagnostics.service_status("vcontrold")
                if restart.returncode == 0 and status["state"] == "active":
                    message, message_ok = f"Config importiert nach {target}, vcontrold läuft.", True
                else:
                    message, message_ok = (
                        f"Config importiert, aber vcontrold-Neustart fehlgeschlagen: "
                        f"{restart.stderr or status['state']}",
                        False,
                    )
            finally:
                tmp_path.unlink(missing_ok=True)

    return render_template("config_import.html", cfg=cfg, message=message, message_ok=message_ok)


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
