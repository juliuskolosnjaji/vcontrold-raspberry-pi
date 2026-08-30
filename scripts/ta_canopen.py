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


# Objektverzeichnis UVR16x2 (Quelle: uvr16x2logging, UNVERIFIZIERT für dieses Gerät,
# siehe Modul-Docstring)
UVR16X2_OBJ_AUSGAENGE = 0x8400          # Netzwerkausgänge, subindex 1-16
UVR16X2_OBJ_EINGANG_WERT = 0x8272       # Eingangswert, subindex 1-16
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
