#!/bin/bash
# Einmalige Einrichtung auf dem Raspberry Pi (Raspberry Pi OS Bookworm).
#
#   cd ~/cobot_depowdering_module
#   ./install.sh
#
# Warum nicht "pip3 install -r requirements.txt": Bookworm verweigert pip-
# Installationen ins System-Python ("externally-managed-environment"). Darum
# kommt alles, was es als Debian-Paket gibt, über apt, und nur CustomTkinter
# (nicht in apt) über pip mit --user --break-system-packages.
set -e
cd "$(dirname "$(readlink -f "$0")")"

echo "== apt: Tk, Pillow, OpenCV, GPIO =="
sudo apt-get update
sudo apt-get install -y python3-tk python3-pil python3-pil.imagetk \
    python3-opencv python3-gpiozero python3-lgpio python3-pip

echo "== pip: CustomTkinter =="
pip3 install --user --break-system-packages --upgrade customtkinter

echo "== Python-Abhängigkeiten prüfen =="
if ! python3 -c "import tkinter, customtkinter, PIL, cv2"; then
    echo "Fehlendes Modul. Prüfe, dass python3-tk, python3-pil, python3-opencv" >&2
    echo "über apt installiert wurden und dass CustomTkinter installiert ist." >&2
    exit 1
fi

echo "== Rechte =="
chmod +x start.sh reset_gpio.sh setup_ur_link.sh install.sh create_desktop_launcher.sh

echo "== Selbsttest der Programmdefinitionen =="
python3 programs.py | tail -5

echo
echo "Fertig. Starten mit:  ./start.sh"
