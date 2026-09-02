"""
Zentrale Definition aller MQTT-Variablen: Anzeigename + Home-Assistant-Discovery-Konfiguration
(Komponente, Einheit, Min/Max/Step, Optionen) -- unabhängig davon, ob der Wert aus vito.xml
(Vcontrold) oder direkt von CAN stammt (siehe README "MQTT-Architektur"). Ersetzt die früher
getrennten, sich teils überschneidenden Dateien command_map.json (Vcontrold-Settable),
can_variables.json (CAN-Custom) und display_names.json (Anzeigenamen).

Eine Variable mit einem 'discovery'-Eintrag ist automatisch schreibbar. Ob sie über Vcontrold
(heizung/cmd/<Name>) oder direkt per CAN (uvr/cmd/<Name>) geschrieben wird, ergibt sich rein
daraus, ob der Name auch als vito.xml-Variable mit Setter existiert -- siehe
ha_discovery.publish_discovery()/publish_can_discovery() und orchestrator.py.
"""
import json
import pathlib

CONFIG_PATH = pathlib.Path(__file__).resolve().parent.parent / "config" / "mqtt_variables.json"

WRITABLE_COMPONENTS = ("number", "select", "switch")


def load(path: pathlib.Path = CONFIG_PATH) -> dict:
    if not path.exists():
        return {}
    return {k: v for k, v in json.loads(path.read_text()).items() if isinstance(v, dict)}


def save(variables: dict, path: pathlib.Path = CONFIG_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(variables, indent=2, ensure_ascii=False) + "\n")


def is_writable(entry: dict) -> bool:
    return entry.get("discovery", {}).get("component") in WRITABLE_COMPONENTS


def display_names(variables: dict) -> dict:
    """Nur die Anzeigename-Overrides, im selben Format wie das frühere display_names.json --
    genutzt von ha_discovery._friendly_name()."""
    return {k: v["display_name"] for k, v in variables.items() if v.get("display_name")}
