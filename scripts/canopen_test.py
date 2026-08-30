#!/usr/bin/env python3
"""
Eigenständiges CLI-Testwerkzeug für CANopen/SDO-Zugriff auf UVR1611/UVR16x2.

Zweck: gegen echte Hardware verifizieren, ob Objektverzeichnis-Indizes und Verbindungsweg
für dein konkretes Gerät funktionieren, BEVOR can_node.py in Produktivbetrieb darauf
umgestellt wird (siehe README Abschnitt 3.3). Kein systemd-Dienst, manuell ausführen.

Zwei Verbindungswege:
  --direct (Standard): normale CANopen-SDO-COB-IDs (0x600+NodeID / 0x580+NodeID),
    kein Verbindungsaufbau -- versuchen, falls per candump bereits Traffic auf genau
    diesen COB-IDs zu sehen war (deutet auf direkte Standard-Unterstützung hin).
  --handshake: TA-spezifischer Verbindungsaufbau aus ta_canopen.py (temporäre COB-ID
    über 0x400|eigene_node_id anfordern) -- falls --direct nicht antwortet.

Beispiel (bestätigter Weg, siehe README 3.3):
  sudo ip link set can1 up type can bitrate 50000
  venv/bin/python scripts/canopen_test.py --read-record --uvr-node-id 65

Mit --heartbeat meldet sich der Pi zusätzlich per CANopen-Bootup+Heartbeat als eigener
Knoten an (z.B. damit er im TA-CMI unter einer bestimmten Node-Nummer auftaucht):
  venv/bin/python scripts/canopen_test.py --read-record --uvr-node-id 65 --own-node-id 60 --heartbeat

Beispiel (unverifizierte Referenzindizes, vermutlich falsch für dieses Gerät):
  venv/bin/python scripts/canopen_test.py --device uvr16x2 --uvr-node-id 65
"""
import argparse
import sys
import time

import can
import canopen

import ta_canopen as ta


def poll_uvr16x2(node: canopen.RemoteNode, count: int, delay: float) -> None:
    for i in range(count):
        print(f"--- Durchlauf {i + 1}/{count} ---")
        for subindex in range(1, 17):
            try:
                data = node.sdo.upload(ta.UVR16X2_OBJ_EINGANG_WERT, subindex)
                value, einheit = ta.decode_uvr16x2_value(data)
                print(f"  Eingang {subindex:2d}: {value:8.1f} {einheit}")
            except Exception as exc:
                print(f"  Eingang {subindex:2d}: Fehler ({exc})", file=sys.stderr)
        time.sleep(delay)


def poll_uvr1611(node: canopen.RemoteNode, count: int, delay: float) -> None:
    for i in range(count):
        print(f"--- Durchlauf {i + 1}/{count} ---")
        for subindex in range(1, 17):
            try:
                data = node.sdo.upload(ta.UVR1611_OBJ_EINGAENGE_1_16_BASE, subindex)
                value, einheit = ta.decode_uvr1611_value(data)
                print(f"  E{subindex:2d}: {value:8.1f} {einheit}")
            except Exception as exc:
                print(f"  E{subindex:2d}: Fehler ({exc})", file=sys.stderr)
        time.sleep(delay)


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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interface", default="can1", help="CAN-Interface (Standard: can1)")
    parser.add_argument(
        "--read-record",
        action="store_true",
        help="Bestätigter Weg (siehe README 3.3): kompletten Datensatz (Objekt 0x4FF4:04) lesen statt --device",
    )
    parser.add_argument("--device", choices=["uvr16x2", "uvr1611"], help="Nur mit den unverifizierten Referenzindizes, siehe ta_canopen.py")
    parser.add_argument("--own-node-id", type=int, default=63, help="eigene Knoten-Nummer (Standard: 63)")
    parser.add_argument("--uvr-node-id", type=int, required=True, help="Knoten-Nummer der UVR (Handbuch/Menü prüfen)")
    parser.add_argument("--handshake", action="store_true", help="TA-Verbindungsaufbau statt Standard-SDO-COB-IDs")
    parser.add_argument(
        "--heartbeat",
        action="store_true",
        help="Bootup + periodischen NMT-Heartbeat senden, damit der Pi z.B. im CMI als eigener Knoten auftaucht",
    )
    parser.add_argument("--count", type=int, default=3, help="Anzahl Poll-Durchläufe (Standard: 3)")
    parser.add_argument("--delay", type=float, default=2.0, help="Sekunden zwischen Durchläufen (Standard: 2.0)")
    args = parser.parse_args()

    if not args.read_record and not args.device:
        parser.error("--read-record oder --device angeben")

    bus = can.interface.Bus(channel=args.interface, interface="socketcan")
    network = canopen.Network()
    network.bus = bus
    network.notifier = can.Notifier(bus, network.listeners)

    heartbeat = None
    if args.heartbeat:
        heartbeat = ta.start_heartbeat(network, args.own_node_id)
        print(f"Heartbeat gestartet (eigene Node-ID {args.own_node_id}, COB-ID 0x{0x700 | args.own_node_id:x})")

    conn = None
    if args.handshake:
        conn = ta.TAConnection(network, args.own_node_id)
        print(f"TA-Verbindungsaufbau zu Knoten {args.uvr_node_id} (eigene Node-ID {args.own_node_id}) ...")
        try:
            node = conn.connect(args.uvr_node_id)
        except ta.TAConnectionError as exc:
            print(f"Verbindungsaufbau fehlgeschlagen: {exc}", file=sys.stderr)
            print(
                f"Prüfen: {args.interface} up + richtige Bitrate (50000)? uvr-node-id korrekt? Kabel/Termination?",
                file=sys.stderr,
            )
            sys.exit(1)
        print(f"Verbunden, temporäre COB-ID: 0x{node.id:x}")
    else:
        print(f"Direkter Zugriff auf Standard-SDO-COB-IDs für Knoten {args.uvr_node_id} "
              f"(0x{0x600 + args.uvr_node_id:x}/0x{0x580 + args.uvr_node_id:x}) ...")
        node = network.add_node(args.uvr_node_id, ta.EDS_PATH)

    try:
        if args.read_record:
            poll_record(node, args.count, args.delay)
        elif args.device == "uvr16x2":
            poll_uvr16x2(node, args.count, args.delay)
        else:
            poll_uvr1611(node, args.count, args.delay)
    finally:
        if heartbeat is not None:
            heartbeat.stop()
        if conn is not None:
            conn.disconnect(args.uvr_node_id, node)
        else:
            network.pop(node.id)
        network.notifier.stop()
        bus.shutdown()


if __name__ == "__main__":
    main()
