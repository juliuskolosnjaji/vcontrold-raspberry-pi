#!/usr/bin/env python3
"""
Passiver SDO-Sniffer: liest CANopen-SDO-Requests/Responses auf dem Bus mit,
ohne selbst SDO-Transfers auszulösen -- nützlich, wenn (wie bei diesem Projekt
festgestellt) bereits ein anderer Master (z.B. CMI) aktiv SDO-Zugriffe auf die
UVR macht. Dekodiert sowohl einzelne Werte (CiA-301 expedited transfer) als
auch komplette Block-Transfers (mehrere 7-Byte-Segmente zu einem Datensatz
zusammengesetzt -- bei diesem Projekt hat sich gezeigt, dass Objekt 0x4FF4:04
ein 98-Byte-Datensatz ist, der so übertragen wird, vermutlich ein TA-eigenes
"D-LOGG"/BL-NET-artiges Format mit Datum/Zeit + Werte-Array + CRLF-Ende).

Zeigt bei jedem einzelnen Wert mehrere Zahlenformat-Kandidaten an, um die
reale Bedeutung eines Objekts durch Abgleich mit einem bekannten Wert (z.B.
Außentemperatur am UVR-Display) zu bestimmen.

Kein systemd-Dienst, manuell ausführen:
  venv/bin/python scripts/sdo_sniffer.py --interface can1
"""
import argparse
import struct
import sys

import can

from ta_canopen import BlockTransferTracker

SDO_TX_BASE = 0x580  # Server -> Client (Antwort)
SDO_RX_BASE = 0x600  # Client -> Server (Anfrage)

# Empirisch an diesem Gerät beobachtete Block-Transfer-Kommandobytes (CiA-301
# Block-Upload, Client=Master/CMI, Server=UVR). Bit 0x80 in einem Segment-Byte
# markiert das letzte Segment einer Teil-Sequenz.
CMD_BLOCK_INITIATE_REQUEST = 0xA4   # Client -> Server: Block-Upload anfordern
CMD_BLOCK_INITIATE_RESPONSE = 0xC6  # Server -> Client: Index/Subindex/Größe bestätigen
CMD_BLOCK_START = 0xA3              # Client -> Server: Segmente anfordern
CMD_BLOCK_ACK = 0xA2                # Client -> Server: Teil-Sequenz quittieren
CMD_BLOCK_END_RESPONSE = 0xC1       # Server -> Client: Ende + CRC
CMD_BLOCK_END_ACK = 0xA1            # Client -> Server: Transfer abschließen


def decode_expedited(data: bytes) -> tuple[int, int, int, bytes] | None:
    """Gibt (index, subindex, size, wert_bytes) zurück, oder None falls kein
    erkanntes expedited-Transfer-Kommando."""
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
    trackers: dict[int, BlockTransferTracker] = {}
    print(f"Höre auf {args.interface} für SDO-Requests/Responses (Strg+C zum Beenden) ...")
    try:
        for msg in bus:
            can_id = msg.arbitration_id
            if SDO_RX_BASE <= can_id < SDO_RX_BASE + 0x80:
                node_id, direction, from_server = can_id - SDO_RX_BASE, "-> Anfrage", False
            elif SDO_TX_BASE <= can_id < SDO_TX_BASE + 0x80:
                node_id, direction, from_server = can_id - SDO_TX_BASE, "<- Antwort", True
            else:
                continue
            if args.node_id is not None and node_id != args.node_id:
                continue

            data = msg.data
            cmd = data[0] if data else None
            tracker = trackers.setdefault(node_id, BlockTransferTracker())

            if from_server and cmd == CMD_BLOCK_INITIATE_RESPONSE:
                tracker.start(data)
                print(
                    f"Node {node_id:3d} <- Block-Upload-Start: Objekt 0x{tracker.index:04X}:{tracker.subindex:02X} "
                    f"({tracker.declared_size} Byte angekündigt)"
                )
                continue

            if from_server and tracker.active and cmd is not None and 0x01 <= (cmd & 0x7F) <= 0x7F and cmd not in (
                CMD_BLOCK_END_RESPONSE,
            ):
                index, subindex = tracker.index, tracker.subindex
                payload = tracker.add_segment(data)
                if payload is None:
                    continue
                print(
                    f"Node {node_id:3d} <- Block-Upload komplett: Objekt 0x{index:04X}:{subindex:02X} "
                    f"({len(payload)} Byte)\n  raw={payload.hex()}"
                )
                if payload.endswith(b"\r\n"):
                    print(f"  endet mit CRLF (0d0a), letzte 4 Byte vor CRLF vermutlich Prüfsumme: {payload[-6:-2].hex()}")
                continue

            if cmd in (CMD_BLOCK_INITIATE_REQUEST, CMD_BLOCK_START, CMD_BLOCK_ACK, CMD_BLOCK_END_ACK):
                continue  # Steuerbytes des Block-Transfers, kein eigener Informationswert für uns
            if from_server and cmd == CMD_BLOCK_END_RESPONSE:
                continue  # CRC-Bestätigung, im Datensatz selbst schon sichtbar

            decoded = decode_expedited(data)
            if decoded is None:
                print(f"Node {node_id:3d} {direction}: unbekanntes Format, data={data.hex()}")
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
