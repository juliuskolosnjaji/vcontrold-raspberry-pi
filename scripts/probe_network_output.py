#!/usr/bin/env python3
"""
Probiert mehrere plausible Kodierungsvarianten für einen TA-"Netzwerkausgang"
nacheinander durch, da ohne Referenz-Frame (kein bereits aktiv sendendes Gerät zum
Mitschneiden verfügbar) nur systematisches Testen bleibt. Während das Skript läuft,
am UVR-Display/CMI (CAN-Analogeingang, siehe README 3.1/3.3) beobachten, ob/wann
sich der angezeigte Wert ändert -- dann in der Konsolenausgabe nachschauen, welche
Variante zu diesem Zeitpunkt gerade lief.

Testwert ist bei jeder Variante deutlich unterscheidbar (kein 0/rundes Zehntel), damit
ein zufälliges Zusammentreffen mit einem echten Sensorwert unwahrscheinlich ist.

Kein systemd-Dienst, manuell ausführen:
  venv/bin/python scripts/probe_network_output.py --own-node-id 60 --output 1
"""
import argparse
import struct
import time

import can

TPDO_COB_ID_BASE = (0x180, 0x280, 0x380, 0x480)
TEST_VALUE = 37.7  # ungewöhnlicher Wert, unwahrscheinlich mit echtem Sensor verwechselbar
TEST_RAW = round(TEST_VALUE * 10)  # 377


def variants(output: int):
    """Erzeugt (beschreibung, cob_id_base, payload)-Tupel für alle Kombinationen aus:
    PDO-Slot (0-3, entspricht Ausgang-Position 1-4/5-8/... innerhalb des Frames),
    Byte-Reihenfolge (LE/BE), Wertbreite (2 oder 4 Byte pro Slot)."""
    slot = (output - 1) % 4
    pdo_index = (output - 1) // 4
    cob_id = TPDO_COB_ID_BASE[pdo_index]

    for byteorder, fmt_prefix in (("little", "<"), ("big", ">")):
        # 4 Werte a 2 Byte (wie bisher angenommen)
        values = [0, 0, 0, 0]
        values[slot] = TEST_RAW
        payload = struct.pack(f"{fmt_prefix}4h", *values)
        yield f"4x int16 {byteorder}-endian, Slot {slot}", cob_id, payload

        # 2 Werte a 4 Byte (falls TA hier wie im Datensatz 0x4FF4 int32 nutzt)
        if slot < 2:
            values4 = [0, 0]
            values4[slot] = TEST_RAW
            payload4 = struct.pack(f"{fmt_prefix}2i", *values4)
            yield f"2x int32 {byteorder}-endian, Slot {slot}", cob_id, payload4

    # Variante wie UVR16x2-SDO-Werte kodiert (siehe ta_canopen.decode_uvr16x2_value):
    # byte0=? byte1=Einheit-Index byte2-3=Wert (12-bit, big-nibble) byte4=? byte5=Vorzeichen
    einheit_temperatur = 1
    raw12 = TEST_RAW & 0x0FFF
    low_byte = raw12 & 0xFF
    high_byte = (raw12 >> 8) & 0x0F
    payload_uvr = bytes([0x00, einheit_temperatur, low_byte, high_byte, 0x00, 0x00, 0x00, 0x00])
    yield "UVR16x2-SDO-Stil (6 Byte, Einheit=Temperatur, Slot ignoriert)", cob_id, payload_uvr


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interface", default="can1", help="CAN-Interface (Standard: can1)")
    parser.add_argument("--own-node-id", type=int, required=True, help="Eigene Knoten-Nummer (muss zur UVR-Konfiguration passen)")
    parser.add_argument("--output", type=int, required=True, help="Ausgangsnummer 1-16 (muss zur UVR-Konfiguration passen)")
    parser.add_argument("--hold", type=float, default=6.0, help="Sekunden pro Variante, mehrfach gesendet (Standard: 6.0)")
    args = parser.parse_args()

    bus = can.interface.Bus(channel=args.interface, interface="socketcan")
    print(f"Testwert: {TEST_VALUE} (roh {TEST_RAW}). Am UVR-Display/CMI beobachten, wann sich der Wert ändert.\n")
    try:
        for description, cob_id_base, payload in variants(args.output):
            cob_id = cob_id_base | args.own_node_id
            print(f"--- {description} (COB-ID 0x{cob_id:x}, Payload {payload.hex()}) ---")
            deadline = time.monotonic() + args.hold
            while time.monotonic() < deadline:
                bus.send(can.Message(arbitration_id=cob_id, data=payload, is_extended_id=False))
                time.sleep(1.0)
    finally:
        bus.shutdown()

    print("\nFertig. Welche Variante lief, als sich der Wert geändert hat?")


if __name__ == "__main__":
    main()
