"""
Generisches MQTT-Variablen-Mapping: der Wert einer MQTT-Variable (egal ob aus vito.xml/Vcontrold
oder CAN stammend) wird zusätzlich als Set-Anfrage an eine andere MQTT-Variable weitergegeben --
unabhängig davon, woher Quelle und Ziel kommen. Ersetzt die frühere "Weiterleitung"
(forward_as_set), die nur CAN-Empfangswerte an eine Vcontrold-settable Variable weiterleiten
konnte und an die CAN-Einstellungen-Seite gekoppelt war.

config/mqtt_mapping.json: Liste von {"source": "<Variablenname>", "target": "<Variablenname>"}.
Beide Namen sind kanonische MQTT-Variablennamen (siehe vito_variables.py/mqtt_variables.py) --
ob ein Name auf heizung/<name> oder uvr/<name> liegt bzw. auf heizung/cmd/<name> oder
uvr/cmd/<name> geschrieben wird, ergibt sich automatisch daraus, ob der Name eine vito.xml-
Variable ist (siehe orchestrator.py, das dieses Mapping tatsächlich ausführt).
"""
import json
import pathlib

CONFIG_PATH = pathlib.Path(__file__).resolve().parent.parent / "config" / "mqtt_mapping.json"


def load(path: pathlib.Path = CONFIG_PATH) -> list:
    if not path.exists():
        return []
    return [
        m for m in json.loads(path.read_text())
        if isinstance(m, dict) and m.get("source") and m.get("target")
    ]


def save(mappings: list, path: pathlib.Path = CONFIG_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(mappings, indent=2, ensure_ascii=False) + "\n")
