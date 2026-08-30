#!/usr/bin/env bash
#
# Master-Installskript für Raspberry Pi OS Lite 64-bit auf Raspberry Pi 3B.
#
# Installiert vcontrold, richtet eine Python-venv mit allen Abhängigkeiten ein,
# aktiviert das CAN-Overlay (Waveshare 2-CH CAN HAT+, MCP2515 über SPI1) und
# installiert alle systemd-Dienste (vcontrold, can0-up und die Web-UI starten
# sofort; orchestrator.service und can-node.service werden installiert, aber
# NICHT automatisch gestartet, da sie erst config/mqtt.env, config/command_map.json,
# config/read_cycles.json und config/can_mapping.json benötigen).
#
# Ausführen als: sudo bash install.sh
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Bitte mit sudo ausführen: sudo bash install.sh" >&2
  exit 1
fi

INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REAL_USER="${SUDO_USER:-pi}"
echo "==> Installationsverzeichnis: ${INSTALL_DIR}"

# ---------------------------------------------------------------------------
# 1. Boot-Config-Pfad ermitteln (Bookworm: /boot/firmware/config.txt, ältere: /boot/config.txt)
# ---------------------------------------------------------------------------
if [[ -f /boot/firmware/config.txt ]]; then
  BOOT_CONFIG="/boot/firmware/config.txt"
elif [[ -f /boot/config.txt ]]; then
  BOOT_CONFIG="/boot/config.txt"
else
  echo "WARNUNG: config.txt nicht gefunden, CAN-Overlay muss manuell eingerichtet werden." >&2
  BOOT_CONFIG=""
fi
echo "==> Boot-Config: ${BOOT_CONFIG:-nicht gefunden}"

# ---------------------------------------------------------------------------
# 2. Systempakete installieren
# ---------------------------------------------------------------------------
echo "==> Installiere Systemabhängigkeiten"
apt-get update
apt-get install -y \
  git \
  cmake \
  build-essential \
  libxml2-dev \
  python3-docutils \
  python3-venv \
  python3-pip \
  can-utils

# ---------------------------------------------------------------------------
# 3. vcontrold bauen und installieren
# ---------------------------------------------------------------------------
echo "==> Baue und installiere vcontrold"
bash "${INSTALL_DIR}/install_vcontrold.sh"

# ---------------------------------------------------------------------------
# 4. Python-venv anlegen und Abhängigkeiten installieren
# ---------------------------------------------------------------------------
echo "==> Richte Python-venv ein (${INSTALL_DIR}/venv)"
python3 -m venv "${INSTALL_DIR}/venv"
"${INSTALL_DIR}/venv/bin/pip" install --upgrade pip
"${INSTALL_DIR}/venv/bin/pip" install -r "${INSTALL_DIR}/requirements.txt"
"${INSTALL_DIR}/venv/bin/pip" install -r "${INSTALL_DIR}/ui/requirements.txt"
chown -R "${REAL_USER}:${REAL_USER}" "${INSTALL_DIR}/venv"

# ---------------------------------------------------------------------------
# 5. CAN-Overlay aktivieren: Waveshare 2-CH CAN HAT+ (2x MCP2515 über SPI1,
#    Default-Verlötung INT_0=GPIO22 fuer CAN0, INT_1=GPIO13 fuer CAN1)
# ---------------------------------------------------------------------------
if [[ -n "${BOOT_CONFIG}" ]] && ! grep -q "mcp2515,spi1-1" "${BOOT_CONFIG}"; then
  echo "==> Aktiviere CAN-Overlay in ${BOOT_CONFIG}"
  {
    echo ""
    echo "# --- vcontrold-raspberry-pi: Waveshare 2-CH CAN HAT+ (MCP2515, SPI1) ---"
    echo "dtparam=spi=on"
    echo "dtoverlay=i2c0"
    echo "dtoverlay=spi1-3cs"
    echo "dtoverlay=mcp2515,spi1-1,oscillator=16000000,interrupt=22"
    echo "dtoverlay=mcp2515,spi1-2,oscillator=16000000,interrupt=13"
  } >> "${BOOT_CONFIG}"
  echo "    Hinweis: spi1-1/INT=22 wird can0, spi1-2/INT=13 wird can1 (Standardverlötung"
  echo "    des Boards). Für die UVR wird nur can0 genutzt. Neustart erforderlich."
  REBOOT_NEEDED=1
else
  echo "==> CAN-Overlay bereits vorhanden oder Boot-Config nicht gefunden, übersprungen"
  REBOOT_NEEDED=0
fi

# ---------------------------------------------------------------------------
# 6. Config-Vorlagen kopieren (nur falls noch nicht vorhanden)
# ---------------------------------------------------------------------------
echo "==> Lege Config-Dateien aus Vorlagen an (falls nicht vorhanden)"
[[ -f "${INSTALL_DIR}/config/mqtt.env" ]] || cp "${INSTALL_DIR}/config/mqtt.env.example" "${INSTALL_DIR}/config/mqtt.env"
[[ -f "${INSTALL_DIR}/config/command_map.json" ]] || cp "${INSTALL_DIR}/config/command_map.json.example" "${INSTALL_DIR}/config/command_map.json"
[[ -f "${INSTALL_DIR}/config/read_cycles.json" ]] || cp "${INSTALL_DIR}/config/read_cycles.json.example" "${INSTALL_DIR}/config/read_cycles.json"
[[ -f "${INSTALL_DIR}/config/can_mapping.json" ]] || cp "${INSTALL_DIR}/config/can_mapping.json.example" "${INSTALL_DIR}/config/can_mapping.json"
[[ -f "${INSTALL_DIR}/ui/ui.env" ]] || cp "${INSTALL_DIR}/ui/ui.env.example" "${INSTALL_DIR}/ui/ui.env"
chown "${REAL_USER}:${REAL_USER}" \
  "${INSTALL_DIR}/config/mqtt.env" \
  "${INSTALL_DIR}/config/command_map.json" \
  "${INSTALL_DIR}/config/read_cycles.json" \
  "${INSTALL_DIR}/config/can_mapping.json" \
  "${INSTALL_DIR}/ui/ui.env"

# ---------------------------------------------------------------------------
# 7. systemd-Units installieren (Platzhalter __INSTALL_DIR__ ersetzen)
# ---------------------------------------------------------------------------
echo "==> Installiere systemd-Units"
shopt -s nullglob
for unit in "${INSTALL_DIR}"/systemd/*.service "${INSTALL_DIR}"/systemd/*.timer; do
  name="$(basename "${unit}")"
  sed "s#__INSTALL_DIR__#${INSTALL_DIR}#g" "${unit}" > "/etc/systemd/system/${name}"
done
shopt -u nullglob
systemctl daemon-reload

# vcontrold, can0-up und die UI können sicher automatisch starten
systemctl enable --now vcontrold
if [[ "${REBOOT_NEEDED}" -eq 0 ]]; then
  systemctl enable --now can0-up || echo "    can0-up konnte nicht gestartet werden (CAN-Interface evtl. noch nicht bereit)"
fi
systemctl enable --now vcontrold-ui

# Diese Dienste erst NACH manueller Konfiguration aktivieren (siehe README):
# orchestrator, can-node
systemctl enable orchestrator can-node 2>/dev/null || true

echo ""
echo "======================================================================"
echo "Installation abgeschlossen."
echo ""
echo "Noch zu erledigen, bevor alles läuft:"
echo "  1. Falls das CAN-Overlay neu hinzugefügt wurde: sudo reboot"
echo "  2. Optolink-USB-Adapter einstecken, prüfen: ls -l /dev/optolink"
echo "  3. Protokoll (KW/P300) für deine Vitotronic in /etc/vcontrold/vcontrold.xml"
echo "     prüfen (für Vitogas 100/V200KW1 bereits automatisch korrekt, siehe README.md Abschnitt 1)"
echo "  4. ${INSTALL_DIR}/config/mqtt.env anpassen (Broker-Host/User/Passwort)"
echo "  5. ${INSTALL_DIR}/ui/ui.env anpassen (Passwort, DEVICE_XML_PATH)"
echo "  6. Danach: sudo systemctl start orchestrator"
echo "     (fährt sofort die Read-Zyklen aus config/read_cycles.json und nimmt"
echo "     Set-Befehle über MQTT entgegen -- kein CAN-Mapping dafür nötig)"
echo "  7. Für die CAN-Anbindung zur UVR: mit dem CAN-Sniffer in der Web-UI die"
echo "     echten CAN-IDs ermitteln, in ${INSTALL_DIR}/config/can_mapping.json eintragen,"
echo "     dann: sudo systemctl start can-node (siehe README.md Abschnitt 3)"
echo ""
echo "Web-UI erreichbar unter: http://<pi-ip>:5000"
echo "======================================================================"
