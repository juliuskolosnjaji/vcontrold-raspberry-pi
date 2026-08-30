"""
Kodierung/Dekodierung für Technische-Alternative-CAN-Netzwerk-Ein-/Ausgänge.

Bestätigte Fakten aus TA's "Zusatzanleitung CMI CoE" (Vers. 1.09, ta.co.at/download/datei/826):
  - Analoge Netzwerkausgänge werden in Blöcken zu je 4 Werten übertragen
    (2 Byte pro Wert = 8 Byte Payload, genau ein CAN-Frame).
  - Digitale Netzwerkausgänge werden in Blöcken zu je 16 Werten übertragen
    (1 Bit pro Wert = 2 Byte Payload).

NICHT öffentlich dokumentiert (weder in der CMI-JSON-API noch in der CoE-Anleitung,
und auch sonst nirgendwo auffindbar) ist das exakte CAN-ID-Schema sowie die genaue
Byte-Reihenfolge/Skalierung. Diese Werte MÜSSEN empirisch per CAN-Sniffer ermittelt
werden (UI: "CAN-Sniffer"-Seite, siehe README Abschnitt 3) und dann in
config/can_mapping.json eingetragen werden -- an dieser Stelle im Code sind nur
plausible Annahmen (analog zu vcontrold-Konventionen: Big-Endian, signed, /10
skaliert) als Ausgangspunkt hinterlegt.
"""
import struct

ANALOG_VALUES_PER_BLOCK = 4
DIGITAL_VALUES_PER_BLOCK = 16
DEFAULT_SCALE = 10  # Rohwert / DEFAULT_SCALE = echter Wert (TA-Konvention wie in vcontrold "Temperatur"-Unit)


def encode_analog_block(values: list[float | None], scale: float = DEFAULT_SCALE) -> bytes:
    """4 Werte -> 8 Byte (je 2 Byte signed, Big-Endian, TODO: Endianness per Sniffer verifizieren)."""
    if len(values) != ANALOG_VALUES_PER_BLOCK:
        raise ValueError(f"Erwarte genau {ANALOG_VALUES_PER_BLOCK} Werte pro Block")
    raw = [0 if v is None else round(v * scale) for v in values]
    return struct.pack(">4h", *raw)


def decode_analog_block(data: bytes, scale: float = DEFAULT_SCALE) -> list[float]:
    if len(data) != 8:
        raise ValueError("Analog-Block muss 8 Byte lang sein")
    raw = struct.unpack(">4h", data)
    return [v / scale for v in raw]


def encode_digital_block(values: list[bool | None]) -> bytes:
    """16 Werte -> 2 Byte Bitmaske (TODO: Bit-Reihenfolge per Sniffer verifizieren)."""
    if len(values) != DIGITAL_VALUES_PER_BLOCK:
        raise ValueError(f"Erwarte genau {DIGITAL_VALUES_PER_BLOCK} Werte pro Block")
    bitmask = 0
    for i, v in enumerate(values):
        if v:
            bitmask |= 1 << i
    return struct.pack(">H", bitmask)


def decode_digital_block(data: bytes) -> list[bool]:
    if len(data) != 2:
        raise ValueError("Digital-Block muss 2 Byte lang sein")
    (bitmask,) = struct.unpack(">H", data)
    return [bool(bitmask & (1 << i)) for i in range(DIGITAL_VALUES_PER_BLOCK)]
