"""
Kodierung/Dekodierung für Technische-Alternative-CAN-Netzwerk-Ein-/Ausgänge.

Bestätigte Fakten aus TA's "Zusatzanleitung CMI CoE" (Vers. 1.09, ta.co.at/download/datei/826)
und dem UVR16x2-Handbuch:
  - Analoge Netzwerkausgänge werden blockweise übertragen: je 4 Werte bei 2 Byte/Wert
    (CoE "V1") oder je 2 Werte bei 4 Byte/Wert (CoE "V2") -- in beiden Fällen genau
    8 Byte Payload, ein CAN-Frame.
  - Digitale Netzwerkausgänge werden in Blöcken zu je 16 Werten übertragen
    (1 Bit pro Wert = 2 Byte Payload).
  - Standard-Bus-Geschwindigkeit der UVR16x2 ist 50 kBit/s.

NICHT öffentlich dokumentiert (weder in der CMI-JSON-API noch in der CoE-Anleitung,
und auch sonst nirgendwo auffindbar) ist das exakte CAN-ID-Schema sowie die genaue
Byte-Reihenfolge/Skalierung. Diese Werte MÜSSEN empirisch per CAN-Sniffer ermittelt
werden (UI: "CAN-Sniffer"-Seite, siehe README Abschnitt 3) und dann über die
"CAN-Einstellungen"-Seite der Web-UI (bzw. direkt in config/can_mapping.json)
eingetragen werden -- an dieser Stelle im Code sind nur plausible Annahmen (Big-Endian,
signed, /10 skaliert) als Ausgangspunkt hinterlegt.
"""
import struct

DIGITAL_VALUES_PER_BLOCK = 16
DEFAULT_SCALE = 10  # Rohwert / DEFAULT_SCALE = echter Wert (TA-Konvention wie in vcontrold "Temperatur"-Unit)
DEFAULT_BITRATE = 50000  # UVR16x2-Standard laut Handbuch
VALID_VALUE_BYTES = (2, 4)

_STRUCT_FORMAT = {2: "h", 4: "i"}  # signed short / signed int, jeweils jeweils Big-Endian


def analog_values_per_block(value_bytes: int = 2) -> int:
    if value_bytes not in VALID_VALUE_BYTES:
        raise ValueError(f"value_bytes muss 2 oder 4 sein, nicht {value_bytes}")
    return 8 // value_bytes


def encode_analog_block(values: list[float | None], value_bytes: int = 2, scale: float = DEFAULT_SCALE) -> bytes:
    """N Werte (4 bei 2 Byte, 2 bei 4 Byte) -> 8 Byte, TODO: Endianness per Sniffer verifizieren."""
    n = analog_values_per_block(value_bytes)
    if len(values) != n:
        raise ValueError(f"Erwarte genau {n} Werte pro Block bei {value_bytes} Byte/Wert")
    raw = [0 if v is None else round(v * scale) for v in values]
    fmt = f">{n}{_STRUCT_FORMAT[value_bytes]}"
    return struct.pack(fmt, *raw)


def decode_analog_block(data: bytes, value_bytes: int = 2, scale: float = DEFAULT_SCALE) -> list[float]:
    if len(data) != 8:
        raise ValueError("Analog-Block muss 8 Byte lang sein")
    n = analog_values_per_block(value_bytes)
    fmt = f">{n}{_STRUCT_FORMAT[value_bytes]}"
    raw = struct.unpack(fmt, data)
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
