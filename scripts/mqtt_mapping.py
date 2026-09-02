"""
"Set-Weiterleitung": der Wert einer beliebigen MQTT-Variable wird zusätzlich als Set-Anfrage an
eine andere MQTT-Variable weitergegeben. Ersetzt die frühere "Weiterleitung" (forward_as_set),
die nur CAN-Empfangswerte an eine Vcontrold-settable Variable weiterleiten konnte und an die
CAN-Einstellungen-Seite gekoppelt war.

config/mqtt_mapping.json: Liste von {"source": "<Variablenname>", "target": "<Variablenname>"}.
Beide Namen sind kanonische MQTT-Variablennamen (siehe vito_variables.py/mqtt_variables.py) --
ob die Quelle auf heizung/<name> oder uvr/<name> liegt, ergibt sich automatisch daraus, ob der
Name eine vito.xml-Variable ist (siehe orchestrator.py, das dieses Mapping tatsächlich ausführt).

Das Ziel ist zwar auf Protokollebene genauso generisch (heizung/cmd/<name> oder uvr/cmd/<name>),
die UI (ui/app.py:mqtt_variables_page()) lässt als Ziel aber bewusst NUR schreibbare Vcontrold-
Variablen zu: ein Wert, der bei can_node.py auf uvr/cmd/<name> ankommt, wird nur dann tatsächlich
per CAN gesendet, wenn <name> zusätzlich in der "TA-Netzwerkausgänge senden"-Tabelle
(can_mapping.json/ta_network_outputs) steht -- für "Vcontrold-Wert -> CAN" ist diese Tabelle
bereits der vollständige, alleinige Weg, ein zusätzliches Mapping dorthin wäre wirkungslos.
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
