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
# Merkt sich, welche Namen beim letzten Aufruf von prune_removed_vito_variables() noch als
# vito.xml-Variable bekannt waren -- damit ein aus vito.xml gelöschter Name automatisch aus
# mqtt_variables.json entfernt wird, statt als (falsch klassifizierte) CAN-Custom-Variable
# weiterzuleben (ein Name ohne diese Historie sieht identisch aus wie eine absichtlich
# angelegte CAN-Variable, siehe prune_removed_vito_variables()). Lokale Laufzeit-Datei.
_VITO_STATE_PATH = pathlib.Path(__file__).resolve().parent.parent / "config" / ".mqtt_variables_vito_state.json"

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


def prune_removed_vito_variables(current_vito_names: set) -> set:
    """Entfernt Einträge aus mqtt_variables.json, deren Name beim letzten Aufruf noch als
    vito.xml-Variable bekannt war, jetzt aber nicht mehr in current_vito_names steht -- z.B. weil
    die Variable aus vito.xml gelöscht wurde. Ohne das würde ein solcher Eintrag unverändert in
    mqtt_variables.json stehen bleiben und beim nächsten Discovery-Lauf fälschlich als CAN-Custom-
    Variable behandelt (siehe Modul-Docstring: "nicht in vito.xml" = CAN-only-Kriterium). Von
    orchestrator.py bei jedem Start/Reconnect aufgerufen (dieselbe Stelle wie die bestehende
    Aufräumlogik für retained heizung/<var>-Werte, siehe README "Verwaiste Entities werden
    automatisch entfernt"). Gibt die entfernten Namen zurück, damit der Aufrufer das loggen kann."""
    previous = set()
    if _VITO_STATE_PATH.exists():
        try:
            previous = set(json.loads(_VITO_STATE_PATH.read_text()))
        except (json.JSONDecodeError, OSError):
            previous = set()

    stale = previous - current_vito_names
    variables = load()
    removed = stale & variables.keys()
    if removed:
        for name in removed:
            del variables[name]
        save(variables)

    _VITO_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _VITO_STATE_PATH.write_text(json.dumps(sorted(current_vito_names), ensure_ascii=False))
    return removed
