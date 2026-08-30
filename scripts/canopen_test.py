#!/usr/bin/env python3
"""
Eigenständiges CLI-Testwerkzeug für CANopen/SDO-Zugriff auf UVR1611/UVR16x2.

Zweck: gegen echte Hardware verifizieren, ob ta_canopen.py's TA-Verbindungsaufbau
und Objektverzeichnis-Indizes für dein konkretes Gerät funktionieren, BEVOR
can_node.py in Produktivbetrieb darauf umgestellt wird (siehe README Abschnitt 3.3).
Kein systemd-Dienst, manuell ausführen.

Beispiel:
  sudo ip link set can0 up type can bitrate 50000
  venv/bin/python scripts/canopen_test.py --device uvr16x2 --uvr-node-id 1
"""
import argparse
import sys
import time

import can
import canopen

import ta_canopen as ta

CAN_INTERFACE = "can0"


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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", choices=["uvr16x2", "uvr1611"], required=True)
    parser.add_argument("--own-node-id", type=int, default=63, help="eigene Knoten-Nummer (Standard: 63)")
    parser.add_argument("--uvr-node-id", type=int, required=True, help="Knoten-Nummer der UVR (Handbuch/Menü prüfen)")
    parser.add_argument("--count", type=int, default=3, help="Anzahl Poll-Durchläufe (Standard: 3)")
    parser.add_argument("--delay", type=float, default=2.0, help="Sekunden zwischen Durchläufen (Standard: 2.0)")
    args = parser.parse_args()

    bus = can.interface.Bus(channel=CAN_INTERFACE, interface="socketcan")
    network = canopen.Network()
    network.bus = bus
    network.notifier = can.Notifier(bus, network.listeners)

    conn = ta.TAConnection(network, args.own_node_id)
    print(f"Verbinde mit Knoten {args.uvr_node_id} (eigene Node-ID {args.own_node_id}) ...")
    try:
        node = conn.connect(args.uvr_node_id)
    except ta.TAConnectionError as exc:
        print(f"Verbindungsaufbau fehlgeschlagen: {exc}", file=sys.stderr)
        print("Prüfen: can0 up + richtige Bitrate (50000)? uvr-node-id korrekt? Kabel/Termination?", file=sys.stderr)
        sys.exit(1)

    print(f"Verbunden, temporäre COB-ID: 0x{node.id:x}")
    try:
        if args.device == "uvr16x2":
            poll_uvr16x2(node, args.count, args.delay)
        else:
            poll_uvr1611(node, args.count, args.delay)
    finally:
        conn.disconnect(args.uvr_node_id, node)
        network.notifier.stop()
        bus.shutdown()


if __name__ == "__main__":
    main()
