"""
Format-Umwandlung für die 21 Zeitschaltuhr-Variablen (TimerM1/TimerWW/TimerZirku x
Mo/Di/Mi/Do/Fr/Sa/So): vcontrold gibt beim Lesen einen mehrzeiligen String mit vier
An/Aus-Paaren zurück und erwartet beim Schreiben ein ganz anderes, kompakteres Format --
siehe unit.c (getCycleTime()/setCycleTime()) im vcontrold-Quelltext:

  Lesen (getTimerXxx):
    "1:An:05:00  Aus:23:00\\n2:An:--     Aus:--\\n3:An:--     Aus:--\\n4:An:--     Aus:--"

  Schreiben (setTimerXxx <arg>): 8 leerzeichen-getrennte Token, je zwei pro Slot
  (An-Zeit, Aus-Zeit), "--" für ein leeres Zeitpaar (ein "--"-Token pro Byte, nicht
  eines für das ganze Paar):
    "05:00 23:00 -- -- -- -- -- --"

Für eine editierbare Home-Assistant-Text-Entity (siehe ha_discovery.py) wird stattdessen ein
kompaktes, gut lesbares Zwischenformat mit vier Slots verwendet, das in beide Richtungen
verlustfrei umwandelbar ist:
    "05:00-23:00 -- -- --"

Gegen echte Hardware verifiziert (siehe Chat-Historie): setTimerZirkuMo mit einem echten
Zeitwert gesetzt, per getTimerZirkuMo zurückgelesen, dann wieder auf den ursprünglichen
leeren Zustand zurückgesetzt -- Format bestätigt.
"""
import re

_CIRCUITS = ("M1", "WW", "Zirku")
_DAYS = ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")
TIMER_VARIABLE_NAMES = frozenset(f"Timer{circuit}{day}" for circuit in _CIRCUITS for day in _DAYS)

_GET_SLOT_PATTERN = re.compile(r"An:\s*(--|\d{2}:\d{2})\s*Aus:\s*(--|\d{2}:\d{2})")
_DISPLAY_SLOT_PATTERN = re.compile(r"^(\d{2}:\d{2})-(\d{2}:\d{2})$")


def is_timer_variable(name: str) -> bool:
    return name in TIMER_VARIABLE_NAMES


def to_display(raw: str) -> str:
    """vclient-Rohausgabe (mehrzeilig) -> kompaktes einzeiliges Anzeigeformat. Gibt bei
    unerwartetem Format (z.B. eine Fehlermeldung statt der üblichen 4 Zeilen) `raw`
    unverändert zurück, statt eine Exception zu werfen -- die Anzeige zeigt dann zwar nicht
    das hübsche Format, aber wenigstens den tatsächlichen vclient-Rückgabewert."""
    slots = []
    for on, off in _GET_SLOT_PATTERN.findall(raw):
        slots.append("--" if on == "--" else f"{on}-{off}")
    return " ".join(slots) if slots else raw


def to_vclient_args(display: str) -> str | None:
    """Kompaktes Anzeigeformat -> 8 Token für setTimerXxx. Gibt None zurück, wenn `display`
    nicht dem erwarteten Format entspricht (Aufrufer soll dann NICHT an vclient weiterreichen,
    sonst würde ein Tippfehler als kryptische vclient-Fehlermeldung oder -- schlimmer -- als
    falsch interpretiertes Zeitpaar landen)."""
    tokens = display.split()
    if not tokens or len(tokens) > 4:
        return None
    args = []
    for tok in tokens:
        if tok == "--":
            args.extend(["--", "--"])
            continue
        m = _DISPLAY_SLOT_PATTERN.match(tok)
        if not m:
            return None
        args.extend([m.group(1), m.group(2)])
    args.extend(["--"] * (8 - len(args)))
    return " ".join(args)
