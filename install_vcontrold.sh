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

echo "==> Lege udev-Regel für Optolink-USB-Adapter an"
cat > /etc/udev/rules.d/99-optolink.rules <<'EOF'
# Optolink-USB-Adapter stabil unter /dev/optolink erreichbar machen.
# ACHTUNG: idVendor/idProduct ggf. anpassen (mit "udevadm info -a -n /dev/ttyUSBx" ermitteln).
SUBSYSTEM=="tty", ATTRS{idVendor}=="0403", ATTRS{idProduct}=="6001", SYMLINK+="optolink"
EOF
udevadm control --reload-rules
udevadm trigger

echo "==> Fertig."
echo "Nächste Schritte:"
echo "  1. Optolink-Kabel einstecken, prüfen mit: ls -l /dev/optolink"
echo "  2. /etc/vcontrold.xml anlegen/anpassen (siehe config/vcontrold.xml.example in diesem Projekt)"
echo "  3. Protokoll (KW oder P300) für deine Vitotronic-Regelung im OpenV-Wiki verifizieren"
echo "  4. vcontrold als systemd-Dienst einrichten (siehe README.md)"
