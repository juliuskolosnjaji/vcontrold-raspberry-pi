#!/usr/bin/env bash
#
# Installiert vcontrold (https://github.com/openv/vcontrold) auf Raspberry Pi OS.
# Ausführen als: sudo bash install_vcontrold.sh
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Bitte mit sudo ausführen: sudo bash install_vcontrold.sh" >&2
  exit 1
fi

SRC_DIR="/usr/local/src/vcontrold"

echo "==> Installiere Build-Abhängigkeiten"
apt-get update
apt-get install -y \
  git \
  cmake \
  build-essential \
  libxml2-dev \
  python3-docutils

echo "==> Klone vcontrold nach ${SRC_DIR}"
if [[ -d "${SRC_DIR}" ]]; then
  echo "Verzeichnis existiert bereits, führe git pull aus"
  git -C "${SRC_DIR}" pull
else
  git clone https://github.com/openv/vcontrold "${SRC_DIR}"
fi

echo "==> Baue vcontrold"
mkdir -p "${SRC_DIR}/build"
cd "${SRC_DIR}/build"
cmake ..
make -j"$(nproc)"

echo "==> Installiere vcontrold"
make install
ldconfig

# CMakeLists.txt von vcontrold setzt CMAKE_INSTALL_PREFIX per Default auf /usr,
# d.h. der Daemon landet unter /usr/sbin/vcontrold, vclient unter /usr/bin/vclient.
if [[ ! -x /usr/sbin/vcontrold ]]; then
  echo "WARNUNG: /usr/sbin/vcontrold nicht gefunden — Installation ggf. in anderes Prefix erfolgt." >&2
fi

echo "==> Lege udev-Regel für Optolink-USB-Adapter an"
cat > /etc/udev/rules.d/99-optolink.rules <<'EOF'
# Optolink-USB-Adapter stabil unter /dev/optolink erreichbar machen.
# ACHTUNG: idVendor/idProduct ggf. anpassen (mit "udevadm info -a -n /dev/ttyUSBx" ermitteln).
SUBSYSTEM=="tty", ATTRS{idVendor}=="0403", ATTRS{idProduct}=="6001", SYMLINK+="optolink"
EOF
udevadm control --reload-rules
udevadm trigger

# ---------------------------------------------------------------------------
# Haupt-Config anlegen: vcontrold erwartet /etc/vcontrold/vcontrold.xml
# (per XInclude eingebunden: /etc/vcontrold/vito.xml).
#
# Standardmäßig wird die bereits getestete Config für die Vitogas 100 mit
# Vitotronic V200KW1 (Device-ID 2094, KW-Protokoll) aus
# config/device-vitogas100-v200kw1/ verwendet (tty=/dev/ttyUSB0). Für andere
# Regler die generischen Vorlagen aus dem vcontrold-Repo nutzen:
#   xml/kw/  = KW2-Protokoll, xml/300/ = P300-Protokoll (siehe README.md Abschnitt 1).
# ---------------------------------------------------------------------------
mkdir -p /etc/vcontrold
CONFIG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/config/device-vitogas100-v200kw1"

if [[ ! -f /etc/vcontrold/vcontrold.xml ]]; then
  if [[ -f "${CONFIG_DIR}/vcontrold.xml" ]]; then
    echo "==> Lege Config für Vitogas 100 / V200KW1 (Device-ID 2094, KW-Protokoll) an"
    cp "${CONFIG_DIR}/vcontrold.xml" /etc/vcontrold/vcontrold.xml
    cp "${CONFIG_DIR}/vito.xml" /etc/vcontrold/vito.xml
  else
    echo "==> Lege generische Standard-Config an (Protokoll: KW2)"
    cp "${SRC_DIR}/xml/kw/vcontrold.xml" /etc/vcontrold/vcontrold.xml
    cp "${SRC_DIR}/xml/kw/vito.xml" /etc/vcontrold/vito.xml
    # Default-Config zeigt auf eine Netzwerkadresse (ser2net-Setup) -- auf lokalen
    # USB-Optolink-Adapter umstellen.
    sed -i 's#<tty>.*</tty>#<tty>/dev/optolink</tty>#' /etc/vcontrold/vcontrold.xml
  fi
else
  echo "==> /etc/vcontrold/vcontrold.xml existiert bereits, wird nicht überschrieben"
  if [[ -f "${CONFIG_DIR}/vcontrold.xml" ]] && ! grep -q 'device ID="2094"' /etc/vcontrold/vcontrold.xml; then
    echo "    WARNUNG: Die bestehende Config referenziert nicht Device-ID 2094 (V200KW1)."
    echo "    Falls das nicht deine tatsächliche Regelung ist, überschreibe sie manuell mit:"
    echo "      sudo cp ${CONFIG_DIR}/vcontrold.xml /etc/vcontrold/vcontrold.xml"
    echo "      sudo cp ${CONFIG_DIR}/vito.xml /etc/vcontrold/vito.xml"
    echo "      sudo systemctl restart vcontrold"
  fi
fi

echo "==> Fertig."
if [[ -f "${CONFIG_DIR}/vcontrold.xml" ]]; then
  echo "Config für Vitogas 100 / V200KW1 (Device-ID 2094, KW-Protokoll) wurde installiert."
  echo "Nächste Schritte:"
  echo "  1. Optolink-USB-Adapter einstecken, prüfen mit: ls -l /dev/ttyUSB0"
  echo "     (bzw. den in /etc/vcontrold/vcontrold.xml eingetragenen <tty>-Pfad)"
  echo "  2. vcontrold als systemd-Dienst einrichten (siehe README.md)"
else
  echo "Nächste Schritte:"
  echo "  1. Optolink-Kabel einstecken, prüfen mit: ls -l /dev/optolink"
  echo "  2. WICHTIG: Protokoll deiner Vitotronic-Regelung im OpenV-Wiki verifizieren"
  echo "     (https://github.com/openv/openv/wiki/) — falls P300 statt KW2 benötigt wird:"
  echo "       sudo cp ${SRC_DIR}/xml/300/vcontrold.xml /etc/vcontrold/vcontrold.xml"
  echo "       sudo cp ${SRC_DIR}/xml/300/vito.xml /etc/vcontrold/vito.xml"
  echo "       sudo sed -i 's#<tty>.*</tty>#<tty>/dev/optolink</tty>#' /etc/vcontrold/vcontrold.xml"
  echo "       sudo systemctl restart vcontrold"
  echo "  3. In /etc/vcontrold/vcontrold.xml das richtige <device ID=\"...\"/> für deinen"
  echo "     Vitotronic-Typ eintragen (Liste am Anfang von /etc/vcontrold/vito.xml)"
  echo "  4. vcontrold als systemd-Dienst einrichten (siehe README.md)"
fi
