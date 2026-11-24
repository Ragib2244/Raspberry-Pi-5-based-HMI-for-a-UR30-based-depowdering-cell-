#!/bin/bash
set -eu

APP_DIR="$(cd -- "$(dirname -- "$(readlink -f -- "$0")")" && pwd)"
START_SCRIPT="$APP_DIR/start.sh"

if [[ ! -f "$START_SCRIPT" ]]; then
    echo "start.sh not found in $APP_DIR" >&2
    exit 1
fi

DESKTOP_DIR="$(xdg-user-dir DESKTOP 2>/dev/null || true)"
if [[ -z "$DESKTOP_DIR" || ! -d "$DESKTOP_DIR" ]]; then
    DESKTOP_DIR="$HOME/Desktop"
fi
mkdir -p "$DESKTOP_DIR"

LAUNCHER="$DESKTOP_DIR/Cobot Control.desktop"

cat > "$LAUNCHER" <<EOF
[Desktop Entry]
Type=Application
Name=Cobot Control
Comment=Start the cobot depowdering HMI
Exec=/bin/bash "$START_SCRIPT"
Path=$APP_DIR
Icon=applications-system
Terminal=true
Categories=Utility;
EOF

chmod +x "$START_SCRIPT" "$LAUNCHER"
echo "Desktop launcher created: $LAUNCHER"
