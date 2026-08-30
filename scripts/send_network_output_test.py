#!/usr/bin/env python3
"""
Testet, ob die UVR einen per Standard-CANopen-TPDO gesendeten "Netzwerkausgang"
annimmt (siehe README Abschnitt 3.1/3.3). Hintergrund: die UVR16x2-Bedienungsanleitung
bestätigt, dass CAN-Analogeingänge über Knotennummer + Ausgangsnummer des Senders
konfiguriert werden (nicht über eine rohe CAN-ID) -- TA rechnet die tatsächliche
CAN-ID intern aus. Hypothese, basierend auf allem bisher an echter Hardware
beobachteten (Standard-CANopen-TPDO-COB-ID-Schema, 4 Werte pro Frame, Little-Endian,
/10 skaliert): Ausgänge 1-4 -> TPDO1 (COB-ID 0x180+NodeID), 5-8 -> TPDO2 (0x280+...),
9-12 -> TPDO3 (0x380+...), 13-16 -> TPDO4 (0x480+...), jeweils 4x int16 LE im
8-Byte-Frame, Ausgang N an Position (N-1)%4.

Noch NICHT gegen echte Hardware verifiziert -- genau dafür ist dieses Skript da.

Kein systemd-Dienst, manuell ausführen:
  venv/bin/python scripts/send_network_output_test.py --own-node-id 60 --output 1 --value 12.3
"""
import argparse
import struct
import sys
import time

import can

TPDO_COB_ID_BASE = (0x180, 0x280, 0x380, 0x480)  # TPDO1..TPDO4


def build_frame(output: int, value: float, existing: list[float]) -> tuple[int, bytes]:
    """output: 1-16. Gibt (cob_id, 8-byte-payload) zurück. `existing` sind die drei
    anderen Werte desselben TPDO-Frames (unverändert lassen, nur den Zielwert setzen)."""
    if not 1 <= output <= 16:
        raise ValueError("output muss 1-16 sein")
    pdo_index = (output - 1) // 4
    slot = (output - 1) % 4
    values = list(existing)
    values[slot] = value
    raw = [round(v * 10) for v in values]
    payload = struct.pack("<4h", *raw)
    return TPDO_COB_ID_BASE[pdo_index], payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interface", default="can1", help="CAN-Interface (Standard: can1)")
    parser.add_argument("--own-node-id", type=int, required=True, help="Eigene Knoten-Nummer (muss zur UVR-Konfiguration passen)")
    parser.add_argument("--output", type=int, required=True, help="Ausgangsnummer 1-16 (muss zur UVR-Konfiguration passen)")
    parser.add_argument("--value", type=float, required=True, help="Testwert, z.B. 12.3")
    parser.add_argument("--count", type=int, default=10, help="Anzahl Sendungen (Standard: 10)")
    parser.add_argument("--delay", type=float, default=1.0, help="Sekunden zwischen den Sendungen (Standard: 1.0)")
    args = parser.parse_args()

    cob_id, payload = build_frame(args.output, args.value, [0.0, 0.0, 0.0, 0.0])
    print(f"Sende Ausgang {args.output} = {args.value} als Node {args.own_node_id} "
          f"auf COB-ID 0x{cob_id | args.own_node_id:x}, Payload {payload.hex()}")

    bus = can.interface.Bus(channel=args.interface, interface="socketcan")
    try:
        for i in range(args.count):
            msg = can.Message(arbitration_id=cob_id | args.own_node_id, data=payload, is_extended_id=False)
            bus.send(msg)
            print(f"  Durchlauf {i + 1}/{args.count} gesendet")
            time.sleep(args.delay)
    except Exception as exc:
        print(f"Fehler beim Senden: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        bus.shutdown()

    print("Fertig. Jetzt am UVR-Display / im CMI (CAN-Analogeingänge) prüfen, ob der Wert angekommen ist.")


if __name__ == "__main__":
    main()
