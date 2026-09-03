"""
Liest die vcontrold-Geräte-XML (vito.xml) und baut eine kanonische Variablenliste:
ein Getter/Setter-Paar wie getTempAist/setTempAist wird zu einer Variable "TempAist"
zusammengefasst.

Diese kanonischen Namen (exakt wie in vito.xml, nur ohne "get"/"set"-Präfix) sind die
EINZIGE Quelle für MQTT-Topics und CAN-Kanalnamen -- es gibt keine separate
Umbenennungs-/Mapping-Ebene mehr. Wer "TempAist" in config/read_cycles.json einträgt,
bekommt automatisch heizung/TempAist als MQTT-Topic und kann denselben Namen 1:1 in
config/can_mapping.json als CAN-Kanal verwenden.

Die automatische Paarung setzt voraus, dass Get/Set-Kommandos exakt der getXXX/setXXX-
Namenskonvention folgen (Standard im OpenV/vcontrold-Ökosystem, aber nicht garantiert --
eine andere Geräte-XML kann z.B. "getKesselTemp"/"SetKesselTempWert" ohne gemeinsamen
Namensrest verwenden). Für solche Fälle siehe load_overrides()/config/vito_command_overrides.json:
eine manuelle {kanonischer_name: {"get": "<Kommandoname>", "set": "<Kommandoname>"}}-Zuordnung,
die die automatische Erkennung für die dort genannten Kommandos ersetzt.
"""
import pathlib
import re
import xml.etree.ElementTree as ET

import atomic_io

DEFAULT_VITO_XML_PATH = "/etc/vcontrold/vito.xml"
_COMMAND_NAME_PATTERN = re.compile(r"^(get|set)([A-Za-z0-9_]+)$")

OVERRIDES_PATH = pathlib.Path(__file__).resolve().parent.parent / "config" / "vito_command_overrides.json"


def load_overrides(path: pathlib.Path = OVERRIDES_PATH) -> dict:
    return atomic_io.load_json(path, {})


def save_overrides(overrides: dict, path: pathlib.Path = OVERRIDES_PATH) -> None:
    atomic_io.write_json(path, overrides)


def list_raw_commands(path: str = DEFAULT_VITO_XML_PATH) -> list:
    """Alle <command>-Elemente aus vito.xml, roh -- ungefiltert nach der get/set-Namenskonvention,
    Grundlage für die manuelle Zuordnung in config/vito_command_overrides.json (sonst wären
    Kommandos, die nicht dem Schema folgen, in der UI gar nicht erst sichtbar)."""
    commands = []
    tree = ET.parse(path)
    for elem in tree.iter("command"):
        name = elem.get("name")
        if not name:
            continue
        desc_elem = elem.find("description")
        description = desc_elem.text.strip() if desc_elem is not None and desc_elem.text else ""
        commands.append({"name": name, "description": description})
    return sorted(commands, key=lambda c: c["name"])


def try_list_raw_commands(path: str = DEFAULT_VITO_XML_PATH) -> list:
    if not path:
        return []
    try:
        return list_raw_commands(path)
    except (ET.ParseError, FileNotFoundError, OSError):
        return []


def load_variables(path: str = DEFAULT_VITO_XML_PATH) -> dict:
    """Gibt {variable_name: {"get": "getXXX"|None, "set": "setXXX"|None}} zurück. Kommandos, die
    in config/vito_command_overrides.json als get/set referenziert werden, nehmen NICHT an der
    automatischen Namenserkennung teil (sonst würden sie zusätzlich unter ihrem auto-abgeleiteten
    Namen als eigene, halbe Variable auftauchen) -- die Override-Einträge werden stattdessen direkt
    als eigene Variablen übernommen, mit auf tatsächlich existierende Kommandos geprüftem get/set."""
    tree = ET.parse(path)
    all_names = {elem.get("name") for elem in tree.iter("command") if elem.get("name")}

    overrides = load_overrides()
    overridden_raw_names = {
        cmd for entry in overrides.values() for cmd in (entry.get("get"), entry.get("set")) if cmd
    }

    variables: dict = {}
    for name in all_names:
        if name in overridden_raw_names:
            continue
        match = _COMMAND_NAME_PATTERN.match(name)
        if not match:
            continue
        kind, var_name = match.group(1), match.group(2)
        variables.setdefault(var_name, {"get": None, "set": None})[kind] = name

    for var_name, entry in overrides.items():
        get_cmd, set_cmd = entry.get("get"), entry.get("set")
        variables[var_name] = {
            "get": get_cmd if get_cmd in all_names else None,
            "set": set_cmd if set_cmd in all_names else None,
        }

    return dict(sorted(variables.items()))


def try_load_variables(path: str = DEFAULT_VITO_XML_PATH) -> dict:
    """Wie load_variables, gibt aber bei Fehlern ein leeres Dict statt Exception zurück."""
    if not path:
        return {}
    try:
        return load_variables(path)
    except (ET.ParseError, FileNotFoundError, OSError):
        return {}


def friendly_name(variable_name: str) -> str:
    """Namen für Anzeigezwecke lesbarer machen: "TempRaumNorSoll" -> "Temp Raum Nor Soll",
    "uvr_kollektortemperatur" -> "Uvr Kollektortemperatur" (funktioniert für CamelCase wie
    für snake_case UVR-Kanalnamen)."""
    spaced = re.sub(r"(?<!^)(?=[A-Z])", " ", variable_name).replace("_", " ").strip()
    return " ".join(word.capitalize() for word in spaced.split())
