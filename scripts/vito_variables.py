"""
Liest die vcontrold-Geräte-XML (vito.xml) und baut eine kanonische Variablenliste:
ein Getter/Setter-Paar wie getTempAist/setTempAist wird zu einer Variable "TempAist"
zusammengefasst.

Diese kanonischen Namen (exakt wie in vito.xml, nur ohne "get"/"set"-Präfix) sind die
EINZIGE Quelle für MQTT-Topics und CAN-Kanalnamen -- es gibt keine separate
Umbenennungs-/Mapping-Ebene mehr. Wer "TempAist" in config/read_cycles.json einträgt,
bekommt automatisch heizung/TempAist als MQTT-Topic und kann denselben Namen 1:1 in
config/can_mapping.json als CAN-Kanal verwenden.
"""
import json
import pathlib
import re
import xml.etree.ElementTree as ET

DEFAULT_VITO_XML_PATH = "/etc/vcontrold/vito.xml"
_COMMAND_NAME_PATTERN = re.compile(r"^(get|set)([A-Za-z0-9_]+)$")

# Kanonischer Variablenname -> benutzerdefinierter Anzeigename für Home Assistant.
# getTempKsoll/setTempKsoll bleiben als vclient-Kommandos unverändert, nur der in HA
# angezeigte Name wird hier überschrieben (Standard ohne Eintrag: friendly_name()).
DISPLAY_NAMES_PATH = pathlib.Path(__file__).resolve().parent.parent / "config" / "display_names.json"


def load_display_names(path: pathlib.Path = DISPLAY_NAMES_PATH) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def load_variables(path: str = DEFAULT_VITO_XML_PATH) -> dict:
    """Gibt {variable_name: {"get": "getXXX"|None, "set": "setXXX"|None}} zurück."""
    variables: dict = {}
    tree = ET.parse(path)
    for elem in tree.iter("command"):
        name = elem.get("name")
        if not name:
            continue
        match = _COMMAND_NAME_PATTERN.match(name)
        if not match:
            continue
        kind, var_name = match.group(1), match.group(2)
        variables.setdefault(var_name, {"get": None, "set": None})[kind] = name
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
