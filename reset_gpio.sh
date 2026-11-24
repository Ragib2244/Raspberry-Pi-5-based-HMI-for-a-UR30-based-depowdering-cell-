#!/bin/sh
# Drive the GPIO outputs to their OFF level at boot, so nothing downstream sees
# a random state.
#
# After a reset the Pi leaves its GPIOs as inputs, and whatever they feed reads
# whatever the pin happens to float to. This drives the pins used by the GUI to
# a defined off before anything else runs.
#
# Which level counts as "off" depends on the hardware, so it is not hard-coded
# here: the pin list and ACTIVE_HIGH are read out of io_wrapper.py, the same
# values the GUI drives the pins with.
#
#   ACTIVE_HIGH = True   off is 0 V   -> op dl   (MOSFET/SSR input, the default)
#   ACTIVE_HIGH = False  off is 3.3 V -> op dh   (low-active relay board)
#
# Getting this backwards energises every channel from power-on until the GUI
# first claims the pins, which is why it is derived rather than duplicated.
#
# Usage:
#   sudo ./reset_gpio.sh          reset the I/O pins to their off level (default)
#   sudo ./reset_gpio.sh --all    also reset every other general-purpose pin
#
# --all covers GPIO 2-27 except the pins with a default alternate function
# (I2C, SPI, UART) — driving those low would break the buses. GPIO 0 and 1 are
# never touched: they are the HAT ID EEPROM lines and are read at boot. Those
# extra pins are always driven low: they are not wired to the I/O hardware, so
# ACTIVE_HIGH says nothing about them.
#
# io_wrapper.py is looked for next to this script. If the script is installed
# somewhere else, point IO_WRAPPER at the module:
#   sudo IO_WRAPPER=/home/pi/cobot_depowdering_module/io_wrapper.py ./reset_gpio.sh
# Without it the fallback values at the bottom of this block are used, and the
# script says so — those are a safety net, not the source of truth.
#
# Run at startup (Raspberry Pi OS, systemd):
#   sudo install -m 755 reset_gpio.sh /usr/local/bin/reset_gpio.sh
#   sudo tee /etc/systemd/system/reset-gpio.service >/dev/null <<'EOF'
#   [Unit]
#   Description=Reset GPIO outputs to their off level
#   DefaultDependencies=no
#   After=sysinit.target
#   Before=multi-user.target
#
#   [Service]
#   Type=oneshot
#   Environment=IO_WRAPPER=/home/pi/cobot_depowdering_module/io_wrapper.py
#   ExecStart=/usr/local/bin/reset_gpio.sh
#   RemainAfterExit=yes
#
#   [Install]
#   WantedBy=multi-user.target
#   EOF
#   sudo systemctl enable --now reset-gpio.service
#
# Note this only defines the state while the Pi is powered. It cannot cover the
# moment before the kernel is up, so anything that must never twitch (a valve,
# a relay) still needs a hardware pull resistor holding its input at the off
# level — to ground for active-high hardware, to its own supply for low-active.

set -eu

# Used only when io_wrapper.py cannot be read (see IO_WRAPPER above). Keep in
# sync with IO_PINS / ACTIVE_HIGH there.
FALLBACK_PINS="5 6 13 16 19 12 17 18 20 21 22 23"
FALLBACK_LEVEL="dl"

# Other pins that are free to use as outputs on a standard Pi 5 header.
# Excluded on purpose: 0,1 (HAT ID EEPROM), 2,3 (I2C1), 7-11 (SPI0), 14,15 (UART0).
EXTRA_PINS="4 24 25 26 27"

IO_WRAPPER="${IO_WRAPPER:-$(dirname "$0")/io_wrapper.py}"

PINS=""
OFF_LEVEL=""

# Read the two values from the module. Importing it is harmless: io_wrapper only
# opens the pins from _ensure_setup(), never at import time.
read_io_config() {
    python3 - "$IO_WRAPPER" <<'PY'
import importlib.util
import sys

spec = importlib.util.spec_from_file_location("io_wrapper", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
print("dl" if module.ACTIVE_HIGH else "dh")
print(" ".join(str(pin) for pin in module.IO_PINS.values()))
PY
}

if [ -r "$IO_WRAPPER" ] && command -v python3 >/dev/null 2>&1; then
    if IO_CONFIG=$(read_io_config 2>/dev/null); then
        OFF_LEVEL=$(printf '%s\n' "$IO_CONFIG" | head -n 1)
        PINS=$(printf '%s\n' "$IO_CONFIG" | tail -n 1)
    fi
fi

if [ -z "$PINS" ] || [ -z "$OFF_LEVEL" ]; then
    echo "reset_gpio: could not read $IO_WRAPPER — using the built-in pin list" >&2
    PINS="$FALLBACK_PINS"
    OFF_LEVEL="$FALLBACK_LEVEL"
fi

if [ "${1:-}" = "--all" ]; then
    RESET_EXTRA=1
elif [ $# -gt 0 ]; then
    echo "usage: $0 [--all]" >&2
    exit 2
else
    RESET_EXTRA=0
fi

# pinctrl ships with Raspberry Pi OS Bookworm (raspi-utils) and is the tool that
# works on the Pi 5. raspi-gpio is its predecessor, kept as a fallback for older
# images. Neither leaves a process running: the pad keeps the state we set.
if command -v pinctrl >/dev/null 2>&1; then
    TOOL="pinctrl"
elif command -v raspi-gpio >/dev/null 2>&1; then
    TOOL="raspi-gpio"
else
    echo "reset_gpio: neither pinctrl nor raspi-gpio found" >&2
    echo "reset_gpio: install with 'sudo apt install raspi-utils'" >&2
    exit 1
fi

if [ "$(id -u)" -ne 0 ]; then
    echo "reset_gpio: needs root to drive the pins — re-run with sudo" >&2
    exit 1
fi

# "op dl" = configure as output, drive low; "op dh" drives high. Set one pin at
# a time so a single bad pin number is reported instead of failing the whole
# batch silently.
drive_pins() {
    level="$1"
    shift
    for pin in "$@"; do
        if ! "$TOOL" set "$pin" op "$level"; then
            echo "reset_gpio: could not reset GPIO$pin" >&2
        fi
    done
}

drive_pins "$OFF_LEVEL" $PINS
echo "reset_gpio: drove GPIO $(printf '%s' "$PINS" | tr ' ' ',') $OFF_LEVEL using $TOOL"

if [ "$RESET_EXTRA" -eq 1 ]; then
    drive_pins dl $EXTRA_PINS
    echo "reset_gpio: drove unused GPIO $(printf '%s' "$EXTRA_PINS" | tr ' ' ',') dl using $TOOL"
fi
