"""
CANopen/SDO-Zugriff auf Technische-Alternative-Regler (UVR1611, UVR16x2).

BESTÄTIGT gegen echte Hardware (siehe README Abschnitt 3.3): dieses Gerät antwortet
direkt auf Standard-CANopen-SDO-COB-IDs (0x600+NodeID / 0x580+NodeID), OHNE den
TA-Verbindungsaufbau unten -- der ist daher vermutlich für dieses Gerät gar nicht
nötig (siehe canopen_test.py --direct). Ein bereits vorhandener zweiter Master auf
dem Bus liest Objekt 0x4FF4:04 per SDO-Block-Transfer -- ein 98-Byte-Datensatz mit
Datum/Zeit + 21 Werten (siehe decode_datensatz()), passiv per sdo_sniffer.py
mitgeschnitten und gegen einen am UVR-Display abgelesenen Wert verifiziert
(Slot 1 = 29.1 = "Analogausgang 1: Vorlauftemperatur").

Die UVR16X2_OBJ_*/UVR1611_OBJ_*-Konstanten und decode_uvr16x2_value()/
decode_uvr1611_value() unten sind dagegen NUR aus dem Referenzprojekt
github.com/staircaseblog/uvr16x2logging übernommene Vermutungen -- gegen dieses
Gerät getestet, Ergebnis "Object does not exist" (0x06020000). Vermutlich andere
Firmware-/Geräte-Variante. Als Fallback/Ausgangspunkt für andere Geräte belassen,
für DIESES Projekt ist decode_datensatz() der bestätigte Weg.

Verbindungsaufbau (TA-Eigenheit, für dieses Gerät nicht nötig, siehe oben):
  Manche TA-Regler vergeben laut Community offenbar keine feste SDO-COB-ID nach der
  üblichen Formel. Stattdessen müsste zuerst auf COB-ID (0x400 | eigene_node_id)
  eine Verbindungsanfrage gesendet werden; der Regler antwortet auf derselben
  COB-ID mit einer temporären COB-ID, die dann als SDO-Client-COB-ID für den
  eigentlichen canopen-Node verwendet wird.
"""
import pathlib
import struct
import threading

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
DIGITAL_OUTPUT_COB_ID_BASE = 0x180  # Ausgänge 1-16 als Bitmaske -- NICHT verifiziert (nur analog getestet)


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
# bei diesem Gerät der Fall, siehe canopen_test.py --read-record).
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


class _BlockTransferTracker:
    """Setzt CiA-301-Block-Upload-Segmente (Server -> Client) zu einem kompletten Datensatz
    zusammen. Ein Tracker pro beobachtetem Node, siehe process_sdo_frame()."""

    def __init__(self):
        self.active = False
        self.declared_size = None
        self.segments: dict[int, bytes] = {}

    def start(self, data: bytes) -> None:
        (self.declared_size,) = struct.unpack("<I", data[4:8])
        self.segments = {}
        self.active = True

    def reset(self) -> None:
        self.active = False
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
    _BlockTransferTracker], das zwischen Aufrufen erhalten bleiben muss. Gibt (node_id, record)
    zurück, sobald ein vollständiger Datensatz zusammengesetzt und dekodiert wurde (record wie
    von decode_datensatz()), sonst None."""
    if not (SDO_SERVER_TO_CLIENT_BASE <= can_id < SDO_SERVER_TO_CLIENT_BASE + 0x80):
        return None
    node_id = can_id - SDO_SERVER_TO_CLIENT_BASE
    cmd = data[0] if data else None
    tracker = trackers.setdefault(node_id, _BlockTransferTracker())

    if cmd == _CMD_BLOCK_INITIATE_RESPONSE:
        index = data[1] | (data[2] << 8)
        subindex = data[3]
        if index == UVR_DATENSATZ_OBJ and subindex == UVR_DATENSATZ_SUBINDEX:
            tracker.start(data)
        else:
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


# BESTÄTIGT gegen echte Hardware (candump während CMI eine "CAN-Analogausgang"-Detailseite
# lud): Objekt 0x2050, Subindex = Ausgangsnummer - 1 (0-basiert), liefert genau einen
# CAN-Analogausgang als SEGMENTIERTE (nicht expedited, nicht Block-) SDO-Antwort, 6 Byte,
# dekodiert mit derselben Formel wie decode_uvr16x2_value() -- Subindex 0x00 = 28.6 =
# "Ausgang 1" (T.Heizkreis VL), Subindex 0x01 = 26.5 = Ausgang 2, Subindex 0x02 = 28.5 =
# Ausgang 3, jeweils exakt deckungsgleich mit den per decode_datensatz() gelesenen Slots.
# Die Objektbasis 0x8272 aus uvr16x2logging war für dieses Gerät falsch, die dortige
# Dekodierformel (decode_uvr16x2_value) aber korrekt.
UVR16X2_OBJ_AUSGANG_WERT = 0x2050  # Ausgangswert (bestätigt), Subindex 0..15 = Ausgang 1..16

# Objektverzeichnis UVR16x2 (Quelle: uvr16x2logging, UNVERIFIZIERT für dieses Gerät,
# siehe Modul-Docstring)
UVR16X2_OBJ_AUSGAENGE = 0x8400          # Netzwerkausgänge, subindex 1-16
UVR16X2_OBJ_EINGANG_WERT = 0x8272       # Eingangswert, subindex 1-16 (UNVERIFIZIERT, falsch für dieses Gerät -- siehe UVR16X2_OBJ_AUSGANG_WERT)
UVR16X2_OBJ_EINGANG_BEZEICHNUNG = 0x8207  # Eingangsbezeichnung (String), subindex 1-16
UVR16X2_OBJ_UHRZEIT = 0x9367
UVR16X2_OBJ_DATUM = 0x9370

# Objektverzeichnis UVR1611 (Quelle: uvr16x2logging, empirisch ermittelt)
UVR1611_OBJ_AUSGAENGE_AKTIV = 0x20D0
UVR1611_OBJ_AUSGAENGE_BITMASKE = 0x20D1
UVR1611_OBJ_EINGAENGE_1_16_BASE = 0x208D    # + subindex 1..16
UVR1611_OBJ_EINGAENGE_17_32_BASE = 0x220B   # + subindex 0x11..0x20
UVR1611_OBJ_UHRZEIT_MINUTE = 0x2011
UVR1611_OBJ_UHRZEIT_STUNDE = 0x2012
UVR1611_OBJ_DATUM_TAG = 0x2014
UVR1611_OBJ_DATUM_MONAT = 0x2015
UVR1611_OBJ_DATUM_JAHR = 0x2016

EINHEITEN_UVR16X2 = ("", "°C", "W/m²", "l/h", "Sek", "Min", "l/Imp", "K", "%", "kW", "kWh", "MWh", "V")
EINHEITEN_UVR1611 = ("nan", "°C", "W/m²", "l/h", "5", "6", "7", "8", "%")


def decode_uvr16x2_value(data: bytes) -> tuple[float, str]:
    """SDO-Response (>=6 Byte) eines UVR16x2-Eingangswerts -> (Wert, Einheit)."""
    if len(data) < 6:
        raise ValueError(f"Erwarte mindestens 6 Byte, bekam {len(data)}")
    einheit_idx, low_byte, high_byte, sign_byte = data[1], data[2], data[3], data[5]

    if sign_byte == 0:
        raw = (0x0F & high_byte) * 256 + low_byte
        value = raw / 10.0
    elif sign_byte == 255:
        raw = (256 - high_byte) * 256 - low_byte
        value = -raw / 10.0
    else:
        raise ValueError(f"Unbekanntes Vorzeichen-Byte: {sign_byte}")

    einheit = EINHEITEN_UVR16X2[einheit_idx] if einheit_idx < len(EINHEITEN_UVR16X2) else "?"
    return value, einheit


def decode_uvr1611_value(data: bytes) -> tuple[float, str]:
    """SDO-Response (>=6 Byte) eines UVR1611-Eingangswerts -> (Wert, Einheit)."""
    if len(data) < 6:
        raise ValueError(f"Erwarte mindestens 6 Byte, bekam {len(data)}")
    low_byte, high_byte = data[0], data[1]
    raw = (0x0F & high_byte) * 256 + low_byte
    if high_byte & 0x80:
        raw = -((raw ^ 0xFFF) + 1)
    einheit_idx = data[5]
    value = raw / 10.0 if data[4] == ord("A") else float(raw)
    einheit = EINHEITEN_UVR1611[einheit_idx] if einheit_idx < len(EINHEITEN_UVR1611) else "?"
    return value, einheit


class TAConnectionError(RuntimeError):
    pass


class TAConnection:
    """
    Verwaltet den TA-spezifischen Verbindungsaufbau, bevor Standard-CANopen-SDO
    auf einem Zielknoten (UVR) genutzt werden kann.

    NICHT-Standard-Teil, siehe Modul-Docstring. Ein Node muss vor jedem SDO-Zugriff
    per connect() geholt und danach nicht mehr benötigt per disconnect() wieder
    freigegeben werden (die temporäre COB-ID ist laut Referenzcode nicht dauerhaft
    reserviert).
    """

    def __init__(self, network: canopen.Network, own_node_id: int):
        self.network = network
        self.own_node_id = own_node_id
        self._cob_id = None
        self._event = threading.Event()
        self.network.subscribe(0x400 | own_node_id, self._on_response)

    def _on_response(self, can_id, data, timestamp):
        if len(data) < 5:
            return
        self._cob_id = data[4]
        self._event.set()

    def connect(self, target_node_id: int, timeout: float = 3.0) -> canopen.RemoteNode:
        """Fordert temporäre COB-ID für target_node_id an, gibt verbundenen Node zurück."""
        self._event.clear()
        self._cob_id = None
        self._send_cob_request(target_node_id, create=True)

        if not self._event.wait(timeout):
            raise TAConnectionError(f"Keine Antwort von Knoten {target_node_id} (Timeout {timeout}s)")
        if not self._cob_id:
            raise TAConnectionError(f"Knoten {target_node_id} lehnte Verbindung ab (COB-ID 0)")

        node = self.network.add_node(self._cob_id, EDS_PATH)
        return node

    def disconnect(self, target_node_id: int, node: canopen.RemoteNode | None = None) -> None:
        if node is not None:
            self.network.pop(node.id)
        self._send_cob_request(target_node_id, create=False)

    def _send_cob_request(self, target_node_id: int, create: bool) -> None:
        payload = struct.pack(
            "BBBBBBBB",
            0x80 | (target_node_id & 0x7F),
            0x00 if create else 0x01,
            0x1F,
            0x00,
            target_node_id & 0x7F,
            self.own_node_id & 0x7F,
            0x80,
            0x12,
        )
        self.network.send_message(0x400 | self.own_node_id, payload)


# Minimales EDS ("Electronic Data Sheet"): canopen braucht eine Objektverzeichnis-Datei
# zum Erzeugen eines RemoteNode, prüft dessen Inhalt bei SDO-Zugriff aber nicht strikt --
# Referenzcode nutzt hierfür ebenfalls nur ein Platzhalter-EDS ("irgendeins genügt").
EDS_PATH = str(pathlib.Path(__file__).resolve().parent / "ta_dummy.eds")
