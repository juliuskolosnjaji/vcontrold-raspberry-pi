"""
CANopen/SDO-Zugriff auf Technische-Alternative-Regler (UVR1611, UVR16x2).

BESTÄTIGT gegen echte Hardware (siehe README Abschnitt 3.3): dieses Gerät antwortet
direkt auf Standard-CANopen-SDO-COB-IDs (0x600+NodeID / 0x580+NodeID), kein TA-
spezifischer Verbindungsaufbau nötig. Ein bereits vorhandener zweiter Master auf
dem Bus liest Objekt 0x4FF4:04 per SDO-Block-Transfer -- ein 98-Byte-Datensatz mit
Datum/Zeit + 21 Werten (siehe decode_datensatz()), passiv per sdo_sniffer.py
mitgeschnitten und gegen einen am UVR-Display abgelesenen Wert verifiziert
(Slot 1 = 29.1 = "Analogausgang 1: Vorlauftemperatur").
"""
import pathlib
import struct

import canopen

# Eigenes, minimales Objektverzeichnis für den Pi als CANopen-Knoten: nur die
# CANopen-Pflichtobjekte (Device Type, Error Register, Identity). Ohne das erkennt
# ein Master (z.B. TA-CMI) den Knoten zwar am Heartbeat, kann aber keine SDO-Antwort
# von ihm bekommen -- CMI markiert das mit einem "Einbahnstraße"-Symbol als Fehler
# (bestätigt an echter Hardware, siehe README Abschnitt 3.3).
OWN_NODE_EDS_PATH = str(pathlib.Path(__file__).resolve().parent / "ta_own_node.eds")
DEFAULT_HEARTBEAT_MS = 1000

# BESTÄTIGT gegen echte Hardware (siehe README Abschnitt 3.3): TA-Netzwerkausgänge, aus
# eigener Node-ID berechnete COB-ID (nicht die generische CANopen-TPDO-Formel!). Quelle:
# zwei unabhängige Community-Implementierungen (HA-Community-Guide "UVR16x2 via CANable/
# candlelight, no CMI", FHEM-CanOverEthernet-Modul), per scripts/send_network_output_test.py
# gegen die echte UVR verifiziert (Wert kam korrekt am konfigurierten CAN-Analogeingang an).
ANALOG_OUTPUT_COB_ID_BASES = (0x200, 0x280, 0x300, 0x380)  # Ausgänge 1-4, 5-8, 9-12, 13-16
# Ausgänge 1-16 als Bitmaske. Ursprünglich nur für die Senderichtung (Pi -> UVR) angenommen,
# inzwischen auch in EMPFANGSRICHTUNG bestätigt (siehe README Abschnitt 3.5): die UVR selbst
# sendet ihre CAN-Digitalausgänge unter 0x180 + eigene Node-ID (bei diesem Gerät 0x18A = 0x180+10,
# die UVR hat laut CMI-Geräteübersicht Node-ID 10) -- zwei unabhängige Testtoggles (Ausgang 4,
# Ausgang 6) bestätigten Bit(N-1) = Ausgang N exakt.
DIGITAL_OUTPUT_COB_ID_BASE = 0x180


def encode_analog_outputs(values: list) -> bytes:
    """4 Werte (ein Ausgangsblock) -> 8 Byte, signed int16 Little-Endian, x10 skaliert.
    None -> 0 (unbenutzter Slot)."""
    if len(values) != 4:
        raise ValueError("Erwarte genau 4 Werte pro Analog-Ausgangsblock")
    raw = [0 if v is None else round(v * 10) for v in values]
    return struct.pack("<4h", *raw)


def encode_digital_outputs(values: list) -> bytes:
    """16 Werte -> 2 Byte Bitmaske, Little-Endian. UNVERIFIZIERT (nur die Analog-Kodierung
    wurde gegen echte Hardware bestätigt, siehe Modul-Docstring)."""
    if len(values) != 16:
        raise ValueError("Erwarte genau 16 Werte für einen Digital-Ausgangsblock")
    bitmask = 0
    for i, v in enumerate(values):
        if v:
            bitmask |= 1 << i
    return struct.pack("<H", bitmask)


def decode_digital_outputs(data: bytes) -> list[bool]:
    """Dekodiert einen von der UVR gesendeten CAN-Digitalausgang-Broadcast (bestätigt an echter
    Hardware, siehe README Abschnitt 3.5 und Modul-Docstring zu DIGITAL_OUTPUT_COB_ID_BASE):
    Byte 0-1 = 16-Bit-Bitmaske Little-Endian (Bit N-1 = Ausgang N, 1=EIN), restliche Bytes des
    8-Byte-Frames ungenutzt. Dieselbe Kodierung wie encode_digital_outputs() oben, nur in
    Empfangsrichtung (UVR ist hier Sender). Gibt 16 Werte zurück (Ausgang 1-16, 1-basiert)."""
    if len(data) < 2:
        raise ValueError(f"Erwarte mindestens 2 Byte, bekam {len(data)}")
    (bitmask,) = struct.unpack("<H", data[:2])
    return [bool(bitmask & (1 << i)) for i in range(16)]


def create_own_node(network: canopen.Network, own_node_id: int, heartbeat_ms: int = DEFAULT_HEARTBEAT_MS) -> canopen.LocalNode:
    """Meldet den Pi als eigenständigen CANopen-Knoten an: canopen.LocalNode beantwortet
    SDO-Anfragen auf die Pflichtobjekte automatisch (network.create_node() verdrahtet
    das selbst, siehe canopen-Quellcode LocalNode.associate_network), und
    node.nmt.start_heartbeat() sendet Bootup + periodischen Heartbeat korrekt nach
    CANopen-Standard (State-Machine inklusive, nicht nur rohe Frames). Zum Beenden:
    node.nmt.stop_heartbeat() dann network.pop(own_node_id)."""
    node = network.create_node(own_node_id, OWN_NODE_EDS_PATH)
    # NmtSlave startet im Zustand INITIALISING (Heartbeat-Byte 0x00) und bleibt dort,
    # bis explizit auf OPERATIONAL gesetzt wird -- ohne das sendet start_heartbeat()
    # dauerhaft 0x00, was vermutlich der Grund für den "Einbahnstraße"/Fehler-Status
    # im CMI war (Node meldet sich nie als betriebsbereit).
    node.nmt.state = "OPERATIONAL"
    node.nmt.start_heartbeat(heartbeat_ms)
    return node

# Bestätigter Datensatz-Zugriff (siehe Modul-Docstring): kompletter Datensatz statt
# einzelner Werte, per SDO-Block-Transfer (canopen-Bibliothek handhabt das transparent
# über node.sdo.upload(), solange der Block-Transfer standardkonform abläuft -- war
# bei diesem Gerät der Fall, siehe canopen_test.py).
UVR_DATENSATZ_OBJ = 0x4FF4
UVR_DATENSATZ_SUBINDEX = 0x04


def decode_datensatz(payload: bytes) -> dict:
    """Dekodiert den 98-Byte-Datensatz von Objekt 0x4FF4:04 (bestätigt, siehe
    Modul-Docstring). Aufbau: 6 Byte Datum/Zeit, dann N x 4-Byte-Werte (signed
    Little-Endian, /10 skaliert), dann 2 Nullbyte + 4-Byte-Prüfsumme (Algorithmus
    noch nicht verifiziert) + 2-Byte CRLF-Ende (0x0D 0x0A)."""
    if len(payload) < 6 + 4 + 2 + 4 + 2 or not payload.endswith(b"\r\n"):
        raise ValueError(f"Unerwartetes Datensatz-Format ({len(payload)} Byte): {payload.hex()}")
    day, month, year, second, minute, hour = payload[0], payload[1], payload[2], payload[3], payload[4], payload[5]
    value_area = payload[6:-8]  # 8 = 2 Nullbyte + 4 Byte Prüfsumme + 2 Byte CRLF
    values = [
        struct.unpack("<i", value_area[i : i + 4])[0] / 10.0
        for i in range(0, len(value_area) - (len(value_area) % 4), 4)
    ]
    checksum = payload[-6:-2]
    return {
        "date": (day, month, 2000 + year),
        "time": (hour, minute, second),
        "values": values,
        "checksum": checksum,
    }


# Passives Mitschneiden statt aktiver SDO-Anfrage (siehe README Abschnitt 3.3): an echter
# Hardware hat sich gezeigt, dass eine aktive Anfrage an die im CMI angezeigte Node-ID der
# UVR mit "Object does not exist" scheitern kann, während ein bereits vorhandener zweiter
# Master (CMI) denselben Datensatz unter einer ANDEREN Node-ID laufend erfolgreich abfragt --
# vermutlich weil die tatsächliche CANopen-Node-ID des antwortenden Geräts von der im
# CMI-Menü angezeigten UVR-Nummer abweicht. Passives Mitlesen des ohnehin laufenden
# Block-Transfers ist daher robuster: funktioniert unabhängig von der korrekten Node-ID und
# kollidiert nie mit der aktiven Abfrage eines anderen Masters (siehe sdo_sniffer.py, aus
# dem diese Logik stammt).
SDO_SERVER_TO_CLIENT_BASE = 0x580  # Server (UVR) -> Client (Master), Node-ID = can_id - Basis
_CMD_BLOCK_INITIATE_RESPONSE = 0xC6
_CMD_BLOCK_END_RESPONSE = 0xC1


class BlockTransferTracker:
    """Setzt CiA-301-Block-Upload-Segmente (Server -> Client) zu einem kompletten Datensatz
    zusammen. Allgemein für jedes SDO-Objekt nutzbar (nicht nur den UVR-Datensatz), ein Tracker
    pro beobachtetem Node. Gemeinsam genutzt von process_sdo_frame() unten (gezielt nur der
    UVR-Datensatz) und scripts/sdo_sniffer.py (passives Mitschneiden beliebiger Objekte)."""

    def __init__(self):
        self.active = False
        self.index = None
        self.subindex = None
        self.declared_size = None
        self.segments: dict[int, bytes] = {}

    def start(self, data: bytes) -> None:
        self.index = data[1] | (data[2] << 8)
        self.subindex = data[3]
        (self.declared_size,) = struct.unpack("<I", data[4:8])
        self.segments = {}
        self.active = True

    def reset(self) -> None:
        self.active = False
        self.index = None
        self.subindex = None
        self.segments = {}
        self.declared_size = None

    def add_segment(self, data: bytes) -> bytes | None:
        seq_byte = data[0]
        is_last = bool(seq_byte & 0x80)
        seq = seq_byte & 0x7F
        self.segments[seq] = data[1:8]
        if not is_last:
            return None
        payload = b"".join(self.segments[s] for s in sorted(self.segments))
        if self.declared_size is not None:
            payload = payload[: self.declared_size]
        self.reset()
        return payload


def process_sdo_frame(can_id: int, data: bytes, trackers: dict) -> tuple[int, dict] | None:
    """Nimmt ein rohes, bereits empfangenes CAN-Frame entgegen (KEINE eigene SDO-Anfrage) und
    versucht, es als Teil eines Block-Transfers für den UVR-Datensatz (Objekt 0x4FF4:04)
    zusammenzusetzen. `trackers` ist ein vom Aufrufer gehaltenes dict[node_id,
    BlockTransferTracker], das zwischen Aufrufen erhalten bleiben muss. Gibt (node_id, record)
    zurück, sobald ein vollständiger Datensatz zusammengesetzt und dekodiert wurde (record wie
    von decode_datensatz()), sonst None."""
    if not (SDO_SERVER_TO_CLIENT_BASE <= can_id < SDO_SERVER_TO_CLIENT_BASE + 0x80):
        return None
    node_id = can_id - SDO_SERVER_TO_CLIENT_BASE
    cmd = data[0] if data else None
    tracker = trackers.setdefault(node_id, BlockTransferTracker())

    if cmd == _CMD_BLOCK_INITIATE_RESPONSE:
        tracker.start(data)
        if tracker.index != UVR_DATENSATZ_OBJ or tracker.subindex != UVR_DATENSATZ_SUBINDEX:
            tracker.reset()  # anderes Objekt auf demselben Node, für uns nicht relevant
        return None

    if tracker.active and cmd is not None and 0x01 <= (cmd & 0x7F) <= 0x7F and cmd != _CMD_BLOCK_END_RESPONSE:
        payload = tracker.add_segment(data)
        if payload is None:
            return None
        try:
            return node_id, decode_datensatz(payload)
        except ValueError:
            return None
    return None


# Minimales EDS ("Electronic Data Sheet"): canopen braucht eine Objektverzeichnis-Datei
# zum Erzeugen eines RemoteNode, prüft dessen Inhalt bei SDO-Zugriff aber nicht strikt --
# Referenzcode nutzt hierfür ebenfalls nur ein Platzhalter-EDS ("irgendeins genügt").
EDS_PATH = str(pathlib.Path(__file__).resolve().parent / "ta_dummy.eds")
