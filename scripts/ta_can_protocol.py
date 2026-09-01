"""
Kodierung/Dekodierung für Technische-Alternative-CAN-Netzwerkausgänge.

Enthielt früher zusätzlich ein spekulatives Blockschema (je 4 Analogwerte/16 Digitalwerte pro
CAN-Frame, aus TA's "Zusatzanleitung CMI CoE" hergeleitet, aber nie an echter Hardware bestätigt)
-- entfernt, nachdem sich das untenstehende, tatsächlich per candump verifizierte Format als der
funktionierende Weg herausstellte (siehe README Abschnitt 3.4).
"""
import struct

DEFAULT_BITRATE = 50000  # UVR16x2-Standard laut Handbuch

# Bestätigtes Format für "CAN-Analogausgang"-Broadcasts der UVR (siehe README Abschnitt 3.4),
# per candump gegen echte Hardware verifiziert. Die UVR sendet NUR bei Wertänderung >
# "Sendebedingung"-Schwelle bzw. spätestens nach der "Intervallzeit" (auf der UVR pro Ausgang
# konfigurierbar), nicht periodisch fix. Alle Ausgänge teilen sich EINE CAN-ID, unterschieden per
# Byte 1 (Ausgangsnummer-1). Vier unabhängige Testwerte (inkl. negativ) bestätigten die Formel
# exakt: Byte0=0x02 markiert diesen Frame-Typ (ein zweiter, hier irrelevanter Frame-Typ mit
# Byte0=0x01 wurde ebenfalls beobachtet, vermutlich Status/Digital), Byte1=Ausgangsnummer-1
# (0-basiert), Byte2=0x01 (bei allen Beobachtungen konstant, vermutlich Mess-/Einheitentyp, z.B.
# 1="Temperatur"), Byte3=0x00 (reserviert/ungenutzt), Byte4-7=Wert als 4-Byte signed Little-Endian,
# /10 skaliert (dieselbe Formel wie im 0x4FF4-Datensatz).
TA_ANALOG_OUTPUT_FRAME_TYPE = 0x02


def decode_ta_analog_output_frame(data: bytes) -> tuple[int, float] | None:
    """Dekodiert einen CAN-Analogausgang-Broadcast der UVR. Gibt (ausgangsnummer, wert) zurück
    (ausgangsnummer 1-basiert), oder None falls data kein Werte-Telegramm dieses Typs ist
    (z.B. der andere, hier ignorierte Frame-Typ mit Byte0 != 0x02)."""
    if len(data) != 8 or data[0] != TA_ANALOG_OUTPUT_FRAME_TYPE:
        return None
    ausgang = data[1] + 1
    (raw,) = struct.unpack("<i", data[4:8])
    return ausgang, raw / 10.0
