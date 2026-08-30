"""
Extrahiert Getter-/Setter-Kommandonamen aus einer vcontrold-Geräte-XML.

Da sich das genaue Schema zwischen Protokollversionen (KW/P300) unterscheiden kann,
wird pragmatisch nach allen Attributwerten und Tag-Namen gesucht, die dem Muster
"get<Name>" oder "set<Name>" entsprechen, statt ein festes Schema vorauszusetzen.
"""
import re
import xml.etree.ElementTree as ET

COMMAND_PATTERN = re.compile(r"^(get|set)[A-Za-z0-9_]+$")


def extract_commands(xml_path: str) -> dict:
    """Gibt {"get": [...], "set": [...]} sortiert zurück."""
    commands = {"get": set(), "set": set()}

    tree = ET.parse(xml_path)
    for elem in tree.iter():
        candidates = [elem.tag]
        candidates.extend(elem.attrib.values())
        if elem.text:
            candidates.append(elem.text.strip())
        for value in candidates:
            if not value:
                continue
            match = COMMAND_PATTERN.match(value.strip())
            if match:
                commands[match.group(1)].add(match.group(0))

    return {
        "get": sorted(commands["get"]),
        "set": sorted(commands["set"]),
    }


def try_extract_commands(xml_path: str) -> dict:
    """Wie extract_commands, gibt aber bei Fehlern leere Listen statt Exception zurück."""
    if not xml_path:
        return {"get": [], "set": []}
    try:
        return extract_commands(xml_path)
    except (ET.ParseError, FileNotFoundError, OSError):
        return {"get": [], "set": []}
