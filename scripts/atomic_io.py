"""
Absturzsicheres Schreiben von Config-Dateien: erst in eine temporäre Datei im selben Verzeichnis
schreiben, dann per os.replace() atomar umbenennen. Ohne das würde ein Stromausfall mitten im
write_text() (RPi/SD-Karte ohne USV, siehe README) die Zieldatei mit halb geschriebenem Inhalt
zurücklassen -- os.replace() ist auf POSIX-Dateisystemen atomar, es gibt keinen Zwischenzustand,
in dem die Datei nur teilweise die neuen Daten enthält.
"""
import json
import os
import pathlib


def write_text(path: pathlib.Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp")
    tmp_path.write_text(content)
    os.replace(tmp_path, path)


def write_json(path: pathlib.Path, data, indent: int = 2) -> None:
    write_text(path, json.dumps(data, indent=indent, ensure_ascii=False) + "\n")


def load_json(path: pathlib.Path, default):
    """Wie json.loads(path.read_text()), aber robust gegen fehlende oder beschädigte Dateien --
    gibt `default` zurück statt eine Exception zu werfen (z.B. nach einem Absturz mitten in einem
    nicht-atomaren Write aus einer älteren Version, oder manueller Fehlbearbeitung der Datei)."""
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return default
