#!/bin/bash
# Startet die Cobot-Control-Oberfläche. Richtet beim ersten Mal alles selbst ein.
#
#   bash start.sh                 einrichten (falls nötig) und starten
#   bash start.sh --autostart     zusätzlich: bei jedem Hochfahren automatisch starten
#   bash start.sh --no-autostart  automatischen Start wieder abschalten
#
# Nach dem Kopieren vom PC fehlen oft die Ausführungsrechte – darum beim ersten
# Mal mit "bash start.sh" aufrufen; danach geht auch "./start.sh".
cd "$(dirname "$(readlink -f "$0")")" || exit 1
APP_DIR="$(pwd)"
AUTOSTART_FILE="$HOME/.config/autostart/cobot-control.desktop"

# Rechte und Zeilenenden reparieren (Kopie von Windows/USB-Stick)
chmod +x ./*.sh 2>/dev/null
sed -i 's/\r$//' ./*.sh 2>/dev/null

case "$1" in
  --autostart)
    mkdir -p "$(dirname "$AUTOSTART_FILE")"
    cat > "$AUTOSTART_FILE" <<EOF
[Desktop Entry]
Type=Application
Name=Cobot Control
Comment=Cobot depowdering HMI
Exec=/bin/bash "$APP_DIR/start.sh"
Terminal=false
X-GNOME-Autostart-enabled=true
EOF
    echo "Autostart eingerichtet: $AUTOSTART_FILE"
    ;;
  --no-autostart)
    rm -f "$AUTOSTART_FILE"
    echo "Autostart entfernt."
    exit 0
    ;;
esac

# Ohne Terminal (Autostart) alles in eine Logdatei schreiben
mkdir -p logs
if [ ! -t 1 ]; then
  exec >>"logs/start.log" 2>&1
  echo "=== $(date) ==="
fi

# Python modules used by the GUI and media playback.
if ! python3 -c "import tkinter, customtkinter, PIL, cv2" 2>/dev/null; then
  echo "Abhängigkeiten fehlen – installiere einmalig (braucht Internet und sudo)…"
  bash ./install.sh || { echo "Installation fehlgeschlagen, siehe Ausgabe oben."; exit 1; }
fi

# Im Autostart kurz warten, bis der Desktop steht
[ ! -t 1 ] && sleep 3

export DISPLAY="${DISPLAY:-:0}"
exec python3 main.py "${@:2}"
