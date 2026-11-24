#!/bin/sh
# Pin the wired link to the robot so it comes up by itself and stays up.
#
# The Pi talks to the robot over a single cable with nothing else on it. There is
# no DHCP server on that link and no switch to keep the carrier alive, which is
# exactly the case NetworkManager handles worst by default. Three things go wrong:
#
#   1. Two profiles for the same interface. The image ships an auto-created
#      "Wired connection 1" (DHCP). A static profile added next to it does not
#      replace it - whichever activates first wins, and when the DHCP one wins it
#      waits for a lease that never comes.
#   2. Carrier loss deactivates the profile for good. Every time the robot
#      controller reboots or is switched off the link drops; after four failed
#      retries (the default) NetworkManager stops trying and the interface stays
#      dead until someone re-saves the settings by hand.
#   3. Energy Efficient Ethernet. The Pi 5 PHY has EEE on by default and
#      renegotiates the link periodically, which shows up as real up/down cycles
#      in dmesg - usually through a patch panel into a 100 Mbit controller port.
#
# This script fixes all three and is safe to re-run: it updates the profile in
# place instead of stacking new ones.
#
# Usage:
#   sudo ./setup_ur_link.sh              apply the configuration
#   ./setup_ur_link.sh --status          report the current state, change nothing
#   sudo ./setup_ur_link.sh --purge      delete the other eth0 profiles instead of
#                                        only switching their autoconnect off
#   sudo ./setup_ur_link.sh --pin-100    force 100 Mbit/full duplex, no autoneg.
#                                        Only if --status shows link flapping; a
#                                        CB3 controller port is 100 Mbit anyway.
#   sudo ./setup_ur_link.sh --no-watchdog   skip the 30 s recovery timer
#   sudo ./setup_ur_link.sh --force      proceed even when this SSH session runs
#                                        over the interface being reconfigured
#
# The address is not hard-coded. It is taken from the first of these that exists,
# so re-running never moves the Pi off an address that already works:
#   $PI_CIDR  ->  the existing ur-link profile  ->  another static eth0 profile
#   ->  the address currently on the interface  ->  .10 in the robot's /24
#
# ROBOT_IP is read out of settings.json (set on the GUI's Settings page) or else
# robotControl.py - the same value the GUI dials, so the two cannot drift apart. Point ROBOT_CONTROL at the module if this script is
# installed somewhere else:
#   sudo ROBOT_CONTROL=/home/pi/cobot_depowdering_module/robotControl.py ./setup_ur_link.sh
#
# What gets written:
#   /etc/default/ur-link                             the values below, for the
#                                                    dispatcher and the watchdog
#   NetworkManager profile "ur-link"                 static, never-default,
#                                                    retries forever
#   /etc/NetworkManager/conf.d/10-ur-link.conf       ignore-carrier, boot wait
#   /etc/NetworkManager/dispatcher.d/50-ur-link      EEE off on every link-up
#   /etc/systemd/system/ur-link-watchdog.{service,timer}
#
# The profile deliberately has no gateway and sets ipv4.never-default, so the
# robot link cannot become the default route. WiFi keeps carrying SSH and apt.
#
# Undo everything:
#   sudo systemctl disable --now ur-link-watchdog.timer
#   sudo rm -f /etc/NetworkManager/conf.d/10-ur-link.conf \
#              /etc/NetworkManager/dispatcher.d/50-ur-link \
#              /etc/systemd/system/ur-link-watchdog.* \
#              /usr/local/bin/ur-link-watchdog /etc/default/ur-link
#   sudo nmcli con delete ur-link
#   sudo systemctl daemon-reload; sudo systemctl restart NetworkManager

set -eu

IFACE="${IFACE:-eth0}"
CON_NAME="${CON_NAME:-ur-link}"
ROBOT_CONTROL="${ROBOT_CONTROL:-$(dirname "$0")/robotControl.py}"

# Used only when robotControl.py cannot be read. Keep in sync with ROBOT_IP there.
FALLBACK_ROBOT_IP="192.0.2.1"

# How long NetworkManager waits at boot for a carrier before giving up on the
# interface. The default 5 s is shorter than a UR controller takes to come up, so
# a Pi that boots first would find no link at all.
CARRIER_WAIT_MS="30000"

CONF_FILE="/etc/NetworkManager/conf.d/10-ur-link.conf"
DISPATCH_FILE="/etc/NetworkManager/dispatcher.d/50-ur-link"
DEFAULTS_FILE="/etc/default/ur-link"
WATCHDOG_BIN="/usr/local/bin/ur-link-watchdog"
WATCHDOG_SERVICE="/etc/systemd/system/ur-link-watchdog.service"
WATCHDOG_TIMER="/etc/systemd/system/ur-link-watchdog.timer"

PURGE=0
PIN_100=no
WATCHDOG=1
FORCE=0
STATUS_ONLY=0

while [ $# -gt 0 ]; do
    case "$1" in
        --status)       STATUS_ONLY=1 ;;
        --purge)        PURGE=1 ;;
        --pin-100)      PIN_100=yes ;;
        --no-watchdog)  WATCHDOG=0 ;;
        --force)        FORCE=1 ;;
        -h|--help)      sed -n '2,/^set -eu$/p' "$0" | sed 's/^#\{1,\} \{0,1\}//' ; exit 0 ;;
        *)              echo "usage: $0 [--status] [--purge] [--pin-100] [--no-watchdog] [--force]" >&2
                        exit 2 ;;
    esac
    shift
done

say() { echo "setup_ur_link: $*"; }
die() { echo "setup_ur_link: $*" >&2; exit 1; }

# ---------------------------------------------------------------- discovery ---

# Parse ROBOT_IP out of the module rather than importing it: parsing cannot have
# a side effect, which matters for something that runs as root.
robot_ip() {
    # The Settings page of the GUI (Developer Mode) stores a changed robot IP in
    # settings.json next to robotControl.py - that one wins, like in the GUI.
    settings_json="$(dirname "$ROBOT_CONTROL")/settings.json"
    if [ -r "$settings_json" ] && command -v python3 >/dev/null 2>&1; then
        ip=$(python3 -c 'import json,sys,ipaddress
v = json.load(open(sys.argv[1], encoding="utf-8")).get("robot_ip")
print(ipaddress.IPv4Address(v))
' "$settings_json" 2>/dev/null) || ip=""
        if [ -n "${ip:-}" ]; then
            printf '%s\n' "$ip"
            return
        fi
    fi
    if [ -r "$ROBOT_CONTROL" ] && command -v python3 >/dev/null 2>&1; then
        ip=$(python3 -c 'import ast,sys
tree = ast.parse(open(sys.argv[1], encoding="utf-8").read())
for node in tree.body:
    if isinstance(node, ast.Assign):
        for target in node.targets:
            if getattr(target, "id", None) == "ROBOT_IP" and isinstance(node.value, ast.Constant):
                print(node.value.value)
                sys.exit(0)
' "$ROBOT_CONTROL" 2>/dev/null) || ip=""
        if [ -n "${ip:-}" ]; then
            printf '%s\n' "$ip"
            return
        fi
    fi
    echo "setup_ur_link: could not read ROBOT_IP from $ROBOT_CONTROL - using $FALLBACK_ROBOT_IP" >&2
    printf '%s\n' "$FALLBACK_ROBOT_IP"
}

# Every ethernet profile that could claim $IFACE: either bound to it by name or
# not bound to any interface at all. NAME is taken as everything before the last
# colon so names containing one survive.
eth_profiles() {
    nmcli -t -f NAME,TYPE con show 2>/dev/null | while IFS= read -r line; do
        [ "${line##*:}" = "802-3-ethernet" ] || continue
        name="${line%:*}"
        bound=$(nmcli -g connection.interface-name con show "$name" 2>/dev/null || true)
        if [ -z "$bound" ] || [ "$bound" = "$IFACE" ]; then
            printf '%s\n' "$name"
        fi
    done
}

profile_exists() { nmcli -t -f NAME con show 2>/dev/null | grep -qxF "$1"; }

profile_active() {
    nmcli -t -f NAME,DEVICE con show --active 2>/dev/null | grep -qxF "$1:$IFACE"
}

# First static address configured on any candidate profile, ur-link first.
address_from_profiles() {
    if profile_exists "$CON_NAME"; then
        set -- "$CON_NAME"
    else
        set --
    fi
    for name in "$@" $(eth_profiles | grep -vxF "$CON_NAME" || true); do
        [ "$(nmcli -g ipv4.method con show "$name" 2>/dev/null || true)" = "manual" ] || continue
        addr=$(nmcli -g ipv4.addresses con show "$name" 2>/dev/null | cut -d, -f1 || true)
        if [ -n "$addr" ]; then
            printf '%s\n' "$addr"
            return
        fi
    done
}

# Whatever is on the interface right now, ignoring the 169.254 link-local that
# turns up precisely when the configuration has failed.
address_from_interface() {
    ip -4 -o addr show "$IFACE" 2>/dev/null \
        | awk '{print $4}' | grep -v '^169\.254\.' | head -n 1 || true
}

nm_version() { nmcli --version 2>/dev/null | sed -n 's/.*version \([0-9][0-9.]*\).*/\1/p'; }
version_ge() { [ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -n 1)" = "$2" ]; }

ROBOT_IP=$(robot_ip)

PI_CIDR="${PI_CIDR:-$(address_from_profiles)}"
[ -n "$PI_CIDR" ] || PI_CIDR=$(address_from_interface)
if [ -z "$PI_CIDR" ]; then
    # Last resort: .10 in the robot's /24. Called out loudly below, because a
    # guessed address is the one thing here that can leave the link unreachable.
    PI_CIDR="$(printf '%s\n' "$ROBOT_IP" | cut -d. -f1-3).10/24"
    GUESSED_ADDR=1
else
    GUESSED_ADDR=0
fi
case "$PI_CIDR" in
    */*) ;;
    *)   PI_CIDR="$PI_CIDR/24" ;;
esac

# ------------------------------------------------------------------- status ---

print_status() {
    echo "setup_ur_link: status"
    echo "  interface        $IFACE"
    echo "  robot            $ROBOT_IP (from $ROBOT_CONTROL)"
    echo "  intended address $PI_CIDR"
    echo "  NetworkManager   $(nm_version || echo 'not found')"
    echo "  carrier          $(cat "/sys/class/net/$IFACE/carrier" 2>/dev/null || echo 'unknown - no such interface?')"
    echo "  live addresses   $(ip -4 -o addr show "$IFACE" 2>/dev/null | awk '{print $4}' | tr '\n' ' ')"
    echo "  eth profiles:"
    eth_profiles | while IFS= read -r name; do
        method=$(nmcli -g ipv4.method con show "$name" 2>/dev/null || true)
        auto=$(nmcli -g connection.autoconnect con show "$name" 2>/dev/null || true)
        retries=$(nmcli -g connection.autoconnect-retries con show "$name" 2>/dev/null || true)
        if profile_active "$name"; then active=yes; else active=no; fi
        echo "    $name  method=$method autoconnect=$auto retries=$retries active=$active"
    done
    if command -v ethtool >/dev/null 2>&1; then
        echo "  link             $(ethtool "$IFACE" 2>/dev/null | sed -n 's/^[[:space:]]*\(Speed\|Duplex\|Link detected\):[[:space:]]*/\1=/p' | tr '\n' ' ')"
        echo "  eee              $(ethtool --show-eee "$IFACE" 2>/dev/null | sed -n 's/^[[:space:]]*EEE status:[[:space:]]*//p')"
    else
        echo "  link             ethtool not installed"
    fi
    echo "  installed files:"
    for f in "$DEFAULTS_FILE" "$CONF_FILE" "$DISPATCH_FILE" "$WATCHDOG_BIN" "$WATCHDOG_SERVICE" "$WATCHDOG_TIMER"; do
        if [ -e "$f" ]; then echo "    present $f"; else echo "    missing $f"; fi
    done
    echo "  watchdog timer   $(systemctl is-active ur-link-watchdog.timer 2>/dev/null || true)"
    echo
    echo "setup_ur_link: recent link events (a flapping cable shows up here)"
    events=$(dmesg 2>/dev/null | grep -iE "$IFACE|Link is (Up|Down)" | tail -n 10 || true)
    if [ -n "$events" ]; then
        printf '%s\n' "$events"
    else
        echo "  (none logged, or dmesg needs root)"
    fi
}

if [ "$STATUS_ONLY" -eq 1 ]; then
    print_status
    exit 0
fi

# ----------------------------------------------------------------- prechecks ---

[ "$(id -u)" -eq 0 ] || die "needs root to change the network configuration - re-run with sudo"
command -v nmcli >/dev/null 2>&1 || die "nmcli not found - this script is for NetworkManager (Raspberry Pi OS Bookworm and newer)"
[ -e "/sys/class/net/$IFACE" ] || die "no interface called $IFACE - check 'ip -br link' and re-run with IFACE=<name>"

NM_VER=$(nm_version)
[ -n "$NM_VER" ] || die "could not determine the NetworkManager version"

# Reconfiguring the interface that carries this session will drop it. The profile
# comes back up a second later, but SSH will not survive the gap.
if [ -n "${SSH_CONNECTION:-}" ]; then
    ssh_local=$(printf '%s\n' "$SSH_CONNECTION" | awk '{print $3}')
    if ip -4 -o addr show "$IFACE" 2>/dev/null | grep -q " ${ssh_local}/"; then
        [ "$FORCE" -eq 1 ] || die "this SSH session runs over $IFACE and would be cut - re-run from the console, or with --force if you can reach the Pi another way"
        say "WARNING: reconfiguring $IFACE while your SSH session uses it (--force)"
    fi
fi

if [ "$GUESSED_ADDR" -eq 1 ]; then
    say "WARNING: no existing static address found, guessing $PI_CIDR from the robot's subnet."
    say "         If that is wrong: sudo PI_CIDR=<addr>/<prefix> $0"
fi

command -v ethtool >/dev/null 2>&1 \
    || say "WARNING: ethtool is not installed, so EEE cannot be switched off - 'sudo apt install ethtool' and re-run"

# --------------------------------------------------------------------- apply ---

say "interface $IFACE, address $PI_CIDR, robot $ROBOT_IP"

{
    echo "# Written by setup_ur_link.sh - do not edit by hand, re-run the script instead."
    echo "# Sourced by $DISPATCH_FILE and $WATCHDOG_BIN."
    echo "IFACE=$IFACE"
    echo "CON_NAME=$CON_NAME"
    echo "PI_CIDR=$PI_CIDR"
    echo "ROBOT_IP=$ROBOT_IP"
    echo "PIN_100=$PIN_100"
} > "$DEFAULTS_FILE"
chmod 644 "$DEFAULTS_FILE"
say "wrote $DEFAULTS_FILE"

# 1. One profile for this interface, and only one.
for name in $(eth_profiles | grep -vxF "$CON_NAME" || true); do
    if [ "$PURGE" -eq 1 ]; then
        if nmcli con delete "$name" >/dev/null 2>&1; then
            say "deleted competing profile '$name'"
        fi
    else
        if nmcli con modify "$name" connection.autoconnect no >/dev/null 2>&1; then
            say "disabled autoconnect on competing profile '$name' (--purge deletes it instead)"
        fi
        if profile_active "$name" && nmcli con down "$name" >/dev/null 2>&1; then
            say "brought '$name' down"
        fi
    fi
done

# 2. The profile itself. autoconnect-retries 0 means retry forever and is the
#    single setting that stops a robot reboot from parking the interface.
if profile_exists "$CON_NAME"; then
    say "updating existing profile '$CON_NAME'"
    nmcli con modify "$CON_NAME" connection.interface-name "$IFACE" >/dev/null
else
    say "creating profile '$CON_NAME'"
    nmcli con add type ethernet ifname "$IFACE" con-name "$CON_NAME" >/dev/null
fi

nmcli con modify "$CON_NAME" \
    ipv4.method manual \
    ipv4.addresses "$PI_CIDR" \
    ipv4.gateway "" \
    ipv4.never-default yes \
    ipv4.may-fail no \
    ipv6.method disabled \
    connection.autoconnect yes \
    connection.autoconnect-retries 0 \
    connection.autoconnect-priority 100 >/dev/null
say "profile set: static, no default route, retries forever"

# Separate call: older nmcli builds reject "none" here, and that must not take the
# settings above down with it. Wake-on-LAN only matters because a woken PHY
# renegotiates the link for no reason on a cable that never carries a magic packet.
nmcli con modify "$CON_NAME" 802-3-ethernet.wake-on-lan none >/dev/null 2>&1 \
    && say "wake-on-lan off" \
    || say "note: could not set wake-on-lan via nmcli (harmless, ethtool clears it on link-up)"

# 3. Keep the address configured while the robot is off, and wait long enough at
#    boot for the controller to bring its side up. The [device-*] form needs
#    NetworkManager 1.20; older builds take a device spec under [main].
if version_ge "$NM_VER" 1.20; then
    {
        echo "# Written by setup_ur_link.sh"
        echo "[main]"
        echo "carrier-wait-timeout=$CARRIER_WAIT_MS"
        echo
        echo "[device-ur-link]"
        echo "match-device=interface-name:$IFACE"
        echo "ignore-carrier=yes"
    } > "$CONF_FILE"
else
    {
        echo "# Written by setup_ur_link.sh (NetworkManager $NM_VER, pre-1.20 syntax)"
        echo "[main]"
        echo "carrier-wait-timeout=$CARRIER_WAIT_MS"
        echo "ignore-carrier=interface-name:$IFACE"
    } > "$CONF_FILE"
fi
chmod 644 "$CONF_FILE"
say "wrote $CONF_FILE (NetworkManager $NM_VER)"

# 4. EEE off on every link-up. It resets with the PHY, so it cannot be set once.
#    Dispatcher scripts are ignored unless root-owned and not group/world writable.
cat > "$DISPATCH_FILE" <<'DISPATCH_EOF'
#!/bin/sh
# Written by setup_ur_link.sh - re-applies the PHY settings that do not survive a
# link reset. Energy Efficient Ethernet is the usual reason a Pi 5 sees periodic
# up/down cycles on a short point-to-point link.
[ -r /etc/default/ur-link ] || exit 0
. /etc/default/ur-link
[ "$1" = "$IFACE" ] || exit 0
[ "$2" = "up" ] || exit 0
command -v ethtool >/dev/null 2>&1 || exit 0
ethtool --set-eee "$IFACE" eee off >/dev/null 2>&1 || true
ethtool -s "$IFACE" wol d >/dev/null 2>&1 || true
if [ "${PIN_100:-no}" = "yes" ]; then
    ethtool -s "$IFACE" speed 100 duplex full autoneg off >/dev/null 2>&1 || true
fi
exit 0
DISPATCH_EOF
chown root:root "$DISPATCH_FILE"
chmod 755 "$DISPATCH_FILE"
say "wrote $DISPATCH_FILE (EEE off, pin-100=$PIN_100)"

# 5. Recovery timer. Everything above is meant to make this unnecessary; it exists
#    so a case nobody predicted costs 30 seconds instead of a trip to the cell.
if [ "$WATCHDOG" -eq 1 ]; then
    cat > "$WATCHDOG_BIN" <<'WATCHDOG_EOF'
#!/bin/sh
# Written by setup_ur_link.sh - bring the robot link back without anyone asking.
#
# Two checks, in order of how sure the fix is:
#   the address is gone   -> the profile is down, bring it up.
#   address and carrier
#   there, robot silent   -> the link returned but the path is stale; reapply.
#                            Three strikes first, so one dropped ping does not
#                            bounce a working interface.
# No carrier means the robot is simply switched off. There is nothing to fix then,
# and the strike counter is cleared so it does not fire on the next power-on.
set -u
[ -r /etc/default/ur-link ] || exit 0
. /etc/default/ur-link
STATE="/run/ur-link-watchdog.strikes"

if ! ip -4 -o addr show "$IFACE" 2>/dev/null | grep -q " ${PI_CIDR%%/*}/"; then
    logger -t ur-link-watchdog "no ${PI_CIDR} on $IFACE - activating $CON_NAME"
    nmcli con up "$CON_NAME" >/dev/null 2>&1 || true
    rm -f "$STATE"
    exit 0
fi

if [ "$(cat "/sys/class/net/$IFACE/carrier" 2>/dev/null || echo 0)" != "1" ]; then
    rm -f "$STATE"
    exit 0
fi

if ping -c 1 -W 1 -I "$IFACE" "$ROBOT_IP" >/dev/null 2>&1; then
    rm -f "$STATE"
    exit 0
fi

strikes=$(( $(cat "$STATE" 2>/dev/null || echo 0) + 1 ))
if [ "$strikes" -ge 3 ]; then
    logger -t ur-link-watchdog "$ROBOT_IP unreachable on $strikes checks with carrier up - reapplying $IFACE"
    nmcli device reapply "$IFACE" >/dev/null 2>&1 || nmcli con up "$CON_NAME" >/dev/null 2>&1 || true
    rm -f "$STATE"
else
    echo "$strikes" > "$STATE"
fi
exit 0
WATCHDOG_EOF
    chmod 755 "$WATCHDOG_BIN"

    {
        echo "# Written by setup_ur_link.sh"
        echo "[Unit]"
        echo "Description=Recover the wired link to the robot"
        echo "After=NetworkManager.service"
        echo "Wants=NetworkManager.service"
        echo
        echo "[Service]"
        echo "Type=oneshot"
        echo "ExecStart=$WATCHDOG_BIN"
    } > "$WATCHDOG_SERVICE"

    {
        echo "# Written by setup_ur_link.sh"
        echo "[Unit]"
        echo "Description=Check the wired link to the robot every 30 s"
        echo
        echo "[Timer]"
        echo "OnBootSec=60"
        echo "OnUnitActiveSec=30"
        echo "AccuracySec=5s"
        echo
        echo "[Install]"
        echo "WantedBy=timers.target"
    } > "$WATCHDOG_TIMER"

    systemctl daemon-reload
    systemctl enable --now ur-link-watchdog.timer >/dev/null 2>&1
    say "installed and started ur-link-watchdog.timer (every 30 s)"
else
    say "skipped the watchdog timer (--no-watchdog)"
fi

# ------------------------------------------------------------------ activate ---

say "restarting NetworkManager"
systemctl restart NetworkManager

# The restart re-reads conf.d and re-activates the profile itself; give it a
# moment before reporting, and nudge it if autoconnect has not landed by then.
i=0
while [ "$i" -lt 15 ]; do
    if profile_active "$CON_NAME"; then break; fi
    i=$((i + 1))
    sleep 1
done
if ! profile_active "$CON_NAME"; then
    nmcli con up "$CON_NAME" >/dev/null 2>&1 \
        || say "WARNING: could not activate '$CON_NAME' - see 'journalctl -u NetworkManager -b'"
fi

echo
print_status
echo
if ping -c 1 -W 2 -I "$IFACE" "$ROBOT_IP" >/dev/null 2>&1; then
    say "robot $ROBOT_IP answers - link is up"
else
    say "robot $ROBOT_IP does not answer. Expected if the controller is off;"
    say "otherwise check the robot's address on the teach pendant and that it is"
    say "in the same subnet as $PI_CIDR."
fi
