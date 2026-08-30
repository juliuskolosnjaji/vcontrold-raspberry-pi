#!/usr/bin/env python3
"""
Passiver SDO-Sniffer: liest CANopen-SDO-Requests/Responses auf dem Bus mit,
ohne selbst SDO-Transfers auszulösen -- nützlich, wenn (wie bei diesem Projekt
festgestellt) bereits ein anderer Master (z.B. CMI) aktiv SDO-Zugriffe auf die
UVR macht. Dekodiert Objektindex/Subindex/Wert nach CiA-301 (expedited transfer)
und zeigt mehrere Zahlenformat-Kandidaten an, um die reale Bedeutung eines
Objekts durch Abgleich mit einem bekannten Wert (z.B. Außentemperatur am
UVR-Display) zu bestimmen.

Kein systemd-Dienst, manuell ausführen:
  venv/bin/python scripts/sdo_sniffer.py --interface can1
"""
import argparse
import struct
import sys

import can

SDO_TX_BASE = 0x580  # Server -> Client (Antwort)
SDO_RX_BASE = 0x600  # Client -> Server (Anfrage)


def decode_expedited(data: bytes) -> tuple[int, int, int, bytes] | None:
    """Gibt (index, subindex, size, wert_bytes) zurück, oder None falls kein
    erkanntes expedited-Transfer-Kommando (segmentierte Transfers werden nicht
    unterstützt -- für die meisten TA-Werte reicht expedited, da <= 4 Byte)."""
    if len(data) < 4:
        return None
    cmd = data[0]
    index = data[1] | (data[2] << 8)
    subindex = data[3]
    # Anfrage (Upload Request): immer 0x40, kein Wert enthalten
    if cmd == 0x40:
        return index, subindex, 0, b""
    # Antwort (Expedited Upload Response): 0x43/0x47/0x4B/0x4F = 4/3/2/1 Byte gültig
    if cmd in (0x43, 0x47, 0x4B, 0x4F):
        size = 4 - ((cmd - 0x43) // 4)
        return index, subindex, size, data[4:4 + size]
    # SDO-Abort
    if cmd == 0x80:
        (abort_code,) = struct.unpack("<I", data[4:8])
        return index, subindex, -1, abort_code.to_bytes(4, "little")
    return None


def format_candidates(value_bytes: bytes) -> str:
    if not value_bytes:
        return ""
    candidates = []
    padded = value_bytes.ljust(4, b"\x00")
    if len(value_bytes) >= 1:
        candidates.append(f"u8={value_bytes[0]}")
    if len(value_bytes) >= 2:
        (u16,) = struct.unpack("<H", value_bytes[:2])
        (i16,) = struct.unpack("<h", value_bytes[:2])
        candidates.append(f"u16={u16} i16={i16} i16/10={i16 / 10:.1f}")
    if len(value_bytes) >= 4:
        (u32,) = struct.unpack("<I", padded)
        (i32,) = struct.unpack("<i", padded)
        (f32,) = struct.unpack("<f", padded)
        candidates.append(f"u32={u32} i32={i32} i32/10={i32 / 10:.1f} f32={f32:.3f}")
    return " | ".join(candidates)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interface", default="can1", help="CAN-Interface (Standard: can1)")
    parser.add_argument("--node-id", type=int, default=None, help="Nur diese Node-ID anzeigen (Standard: alle)")
    args = parser.parse_args()

    bus = can.interface.Bus(channel=args.interface, interface="socketcan")
    print(f"Höre auf {args.interface} für SDO-Requests/Responses (Strg+C zum Beenden) ...")
    try:
        for msg in bus:
            can_id = msg.arbitration_id
            if SDO_RX_BASE <= can_id < SDO_RX_BASE + 0x80:
                node_id, direction = can_id - SDO_RX_BASE, "-> Anfrage"
            elif SDO_TX_BASE <= can_id < SDO_TX_BASE + 0x80:
                node_id, direction = can_id - SDO_TX_BASE, "<- Antwort"
            else:
                continue
            if args.node_id is not None and node_id != args.node_id:
                continue

            decoded = decode_expedited(msg.data)
            if decoded is None:
                print(f"Node {node_id:3d} {direction}: unbekanntes Format, data={msg.data.hex()}")
                continue
            index, subindex, size, value_bytes = decoded
            if size == 0:
                print(f"Node {node_id:3d} {direction}: Objekt 0x{index:04X}:{subindex:02X}")
            elif size == -1:
                (abort_code,) = struct.unpack("<I", value_bytes)
                print(f"Node {node_id:3d} {direction}: Objekt 0x{index:04X}:{subindex:02X} ABORT 0x{abort_code:08X}")
            else:
                print(
                    f"Node {node_id:3d} {direction}: Objekt 0x{index:04X}:{subindex:02X} "
                    f"({size} Byte) raw={value_bytes.hex()}  {format_candidates(value_bytes)}"
                )
    except KeyboardInterrupt:
        pass
    finally:
        bus.shutdown()


if __name__ == "__main__":
    main()
