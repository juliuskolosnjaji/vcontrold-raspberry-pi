#!/usr/bin/env python3
"""
Eigenständiges CLI-Testwerkzeug für den bestätigten SDO-Datensatz-Zugriff auf die UVR
(Objekt 0x4FF4:04, siehe ta_canopen.py-Modul-Docstring und README Abschnitt 3.3).

Zweck: gegen echte Hardware verifizieren, dass der Datensatz lesbar ist, BEVOR can_node.py
in Produktivbetrieb darauf umgestellt wird. Kein systemd-Dienst, manuell ausführen.

Beispiel (bestätigter Weg, siehe README 3.3):
  sudo ip link set can1 up type can bitrate 50000
  venv/bin/python scripts/canopen_test.py --uvr-node-id 65

Mit --heartbeat meldet sich der Pi zusätzlich per CANopen-Bootup+Heartbeat als eigener
Knoten an (z.B. damit er im TA-CMI unter einer bestimmten Node-Nummer auftaucht):
  venv/bin/python scripts/canopen_test.py --uvr-node-id 65 --own-node-id 60 --heartbeat
"""
import argparse
import sys
import time

import can
import canopen

import ta_canopen as ta


def poll_record(node: canopen.RemoteNode, count: int, delay: float) -> None:
    """Bestätigter Weg für dieses Projekt (siehe ta_canopen.py-Modul-Docstring):
    kompletter Datensatz statt einzelner geratener Objektindizes."""
    for i in range(count):
        try:
            payload = node.sdo.upload(ta.UVR_DATENSATZ_OBJ, ta.UVR_DATENSATZ_SUBINDEX)
            record = ta.decode_datensatz(payload)
            day, month, year = record["date"]
            hour, minute, second = record["time"]
            print(f"--- Durchlauf {i + 1}/{count} ({day:02d}.{month:02d}.{year} {hour:02d}:{minute:02d}:{second:02d}) ---")
            for slot, value in enumerate(record["values"], start=1):
                print(f"  Slot {slot:2d}: {value:8.1f}")
        except Exception as exc:
            print(f"Fehler beim Lesen des Datensatzes: {exc}", file=sys.stderr)
        time.sleep(delay)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--interface", default="can1", help="CAN-Interface (Standard: can1)")
    parser.add_argument("--own-node-id", type=int, default=63, help="eigene Knoten-Nummer (Standard: 63)")
    parser.add_argument("--uvr-node-id", type=int, required=True, help="Knoten-Nummer der UVR (Handbuch/Menü prüfen)")
    parser.add_argument(
        "--heartbeat",
        action="store_true",
        help="Als eigener CANopen-Knoten anmelden (Heartbeat + SDO-Server für Pflichtobjekte), "
        "damit der Pi z.B. im CMI korrekt (nicht als 'Einbahnstraße'/Fehler) auftaucht",
    )
    parser.add_argument("--count", type=int, default=3, help="Anzahl Poll-Durchläufe (Standard: 3)")
    parser.add_argument("--delay", type=float, default=2.0, help="Sekunden zwischen Durchläufen (Standard: 2.0)")
    args = parser.parse_args()

    bus = can.interface.Bus(channel=args.interface, interface="socketcan")
    network = canopen.Network()
    network.bus = bus
    network.notifier = can.Notifier(bus, network.listeners)

    own_node = None
    if args.heartbeat:
        own_node = ta.create_own_node(network, args.own_node_id)
        print(f"Als eigener Knoten angemeldet (Node-ID {args.own_node_id}, Heartbeat + SDO-Server für Pflichtobjekte)")

    print(f"Direkter Zugriff auf Standard-SDO-COB-IDs für Knoten {args.uvr_node_id} "
          f"(0x{0x600 + args.uvr_node_id:x}/0x{0x580 + args.uvr_node_id:x}) ...")
    node = network.add_node(args.uvr_node_id, ta.EDS_PATH)

    try:
        poll_record(node, args.count, args.delay)
    finally:
        if own_node is not None:
            own_node.nmt.stop_heartbeat()
            network.pop(own_node.id)
        network.pop(node.id)
        network.notifier.stop()
        bus.shutdown()


if __name__ == "__main__":
    main()
