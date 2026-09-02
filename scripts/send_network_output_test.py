#!/usr/bin/env python3
"""
Sendet einen TA-"Netzwerkausgang" (analog) an die UVR (siehe README Abschnitt 3.1/3.3).

BESTÄTIGT gegen echte Hardware: Wert kommt korrekt am konfigurierten UVR-CAN-Analogeingang an.
Voraussetzung dort: Feld "Messgröße" auf einen konkreten Typ (z.B. "Temperatur") stellen, sonst
zeigt die UVR den rohen Ganzzahlwert unskaliert an ("Automatisch" reicht nicht).

Nutzt dieselbe Kodierung wie can_node.py in Produktivbetrieb (ta_canopen.encode_analog_outputs/
ANALOG_OUTPUT_COB_ID_BASES), damit dieses Testskript nie von der echten Sendelogik abweicht.

Kein systemd-Dienst, manuell ausführen:
  venv/bin/python scripts/send_network_output_test.py --own-node-id 60 --output 1 --value 12.3
"""
import argparse
import sys
import time

import can

import ta_canopen as ta


def build_frame(output: int, value: float, existing: list) -> tuple[int, bytes]:
    """output: 1-16. Gibt (cob_id_base, 8-byte-payload) zurück. `existing` sind die drei anderen
    Werte desselben TPDO-Frames (unverändert lassen, nur den Zielwert setzen)."""
    if not 1 <= output <= 16:
        raise ValueError("output muss 1-16 sein")
    block_index = (output - 1) // 4
    slot = (output - 1) % 4
    values = list(existing)
    values[slot] = value
    payload = ta.encode_analog_outputs(values)
    return ta.ANALOG_OUTPUT_COB_ID_BASES[block_index], payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interface", default="can1", help="CAN-Interface (Standard: can1)")
    parser.add_argument("--own-node-id", type=int, required=True, help="Eigene Knoten-Nummer (muss zur UVR-Konfiguration passen)")
    parser.add_argument("--output", type=int, required=True, help="Ausgangsnummer 1-16 (muss zur UVR-Konfiguration passen)")
    parser.add_argument("--value", type=float, required=True, help="Testwert, z.B. 12.3")
    parser.add_argument("--count", type=int, default=10, help="Anzahl Sendungen (Standard: 10)")
    parser.add_argument("--delay", type=float, default=1.0, help="Sekunden zwischen den Sendungen (Standard: 1.0)")
    args = parser.parse_args()

    cob_id_base, payload = build_frame(args.output, args.value, [0.0, 0.0, 0.0, 0.0])
    cob_id = cob_id_base | args.own_node_id
    print(f"Sende Ausgang {args.output} = {args.value} als Node {args.own_node_id} "
          f"auf COB-ID 0x{cob_id:x}, Payload {payload.hex()}")

    bus = can.interface.Bus(channel=args.interface, interface="socketcan")
    try:
        for i in range(args.count):
            msg = can.Message(arbitration_id=cob_id, data=payload, is_extended_id=False)
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
